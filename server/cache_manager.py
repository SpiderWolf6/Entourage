"""CacheManager — LRU cache coordinator between SQLite (hot) and Supabase (cold).

Responsibilities:
1. Load project into SQLite cache when opened/created
2. Sync SQLite → Supabase when project completes or goes idle
3. Evict from SQLite (LRU) when:
   - Project idle > 2 hours
   - SQLite DB > 500 MB
4. Track last_activity per project for idle detection
5. Background loop runs every 30 minutes

Cache states:
  hot  — in SQLite, actively being used
  warm — in SQLite, idle but not yet evicted
  cold — in Supabase only, not in SQLite

The cache manager does NOT manage demo processes (that's demo_launcher.py).
It only manages data persistence.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IDLE_EVICT_HOURS    = 2       # evict from SQLite after 2hr idle
SQLITE_MAX_MB       = 500     # evict when SQLite exceeds this size
EVICTION_LOOP_SECS  = 1800    # check every 30 minutes

# tracks last_activity: project_id -> datetime
_last_activity: dict[str, datetime] = {}


def touch(project_id: str) -> None:
    """Record that a project was accessed right now."""
    _last_activity[project_id] = datetime.now(timezone.utc)


def get_last_activity(project_id: str) -> datetime | None:
    return _last_activity.get(project_id)


def _sqlite_size_mb() -> float:
    """Return the current SQLite DB file size in MB."""
    from server.config import settings
    db_url = settings.database_url
    # extract path from sqlite+aiosqlite:///./path.db
    path = db_url.replace("sqlite+aiosqlite:///", "").lstrip("/")
    if not path.startswith("/"):
        # relative path — resolve from CWD
        path = os.path.join(os.getcwd(), path)
    try:
        return os.path.getsize(path) / 1024 / 1024
    except OSError:
        return 0.0


def _is_idle(project_id: str) -> bool:
    """Return True if project has been idle longer than IDLE_EVICT_HOURS."""
    last = _last_activity.get(project_id)
    if last is None:
        return True
    return datetime.now(timezone.utc) - last > timedelta(hours=IDLE_EVICT_HOURS)


async def load_project_into_cache(project_id: str) -> dict | None:
    """Fetch a project from Supabase and write it into SQLite cache.

    Called when a user opens an existing project that isn't in SQLite yet.
    Returns the project dict or None if not found in Supabase.
    """
    import json
    from server import supabase_store
    from server.database import async_session
    from server.models.project import Project
    from server.models.artifact import Artifact
    from server.models.execution import SprintRun
    from sqlalchemy import select

    # check if already in cache
    async with async_session() as db:
        result = await db.execute(select(Project).where(Project.id == project_id))
        if result.scalar_one_or_none():
            touch(project_id)
            return None  # already cached

    # fetch from Supabase
    sb_project = await supabase_store.get_project(project_id)
    if not sb_project:
        return None

    # write project to SQLite
    async with async_session() as db:
        project = Project(
            id            = sb_project["id"],
            name          = sb_project.get("name", ""),
            user_story    = sb_project.get("user_story", ""),
            status        = sb_project.get("status", "created"),
            workspace_path= sb_project.get("workspace_path", ""),
            config        = json.dumps(sb_project.get("config", {})),
        )
        db.add(project)

        # write artifacts
        sb_artifacts = await supabase_store.get_artifacts(project_id)
        for a in sb_artifacts:
            artifact = Artifact(
                id           = a.get("id"),
                project_id   = project_id,
                agent        = a.get("agent", ""),
                artifact_type= a.get("artifact_type", ""),
                title        = a.get("title", ""),
                content      = a.get("content", ""),
                tokens_used  = a.get("tokens_used", 0),
                cost         = a.get("cost", 0.0),
            )
            db.add(artifact)

        # write sprint runs
        sb_runs = await supabase_store.get_sprint_runs(project_id)
        for r in sb_runs:
            run = SprintRun(
                id            = r.get("id"),
                project_id    = project_id,
                sprint_number = r.get("sprint_number", 0),
                status        = r.get("status", ""),
                notes         = r.get("notes", ""),
                files_written = json.dumps(r.get("files_written", [])),
            )
            db.add(run)

        await db.commit()

    touch(project_id)
    log.info("Cache: loaded project %s from Supabase into SQLite", project_id)
    return sb_project


async def sync_project_to_supabase(project_id: str, user_id: str | None = None) -> None:
    """Sync a project from SQLite cache to Supabase (source of truth)."""
    import json
    from server import supabase_store
    from server.database import async_session
    from server.models.project import Project
    from server.models.artifact import Artifact
    from server.models.execution import SprintRun
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            return

        config = json.loads(project.config) if project.config else {}

        sb_project = {
            "id":             project.id,
            "name":           project.name,
            "user_story":     project.user_story,
            "status":         project.status,
            "workspace_path": project.workspace_path or "",
            "config":         config,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        if user_id:
            sb_project["user_id"] = user_id

        await supabase_store.upsert_project(sb_project)

        # sync artifacts
        result2 = await db.execute(
            select(Artifact).where(Artifact.project_id == project_id)
        )
        artifacts = result2.scalars().all()
        for a in artifacts:
            await supabase_store.save_artifact({
                "project_id":    project_id,
                "agent":         a.agent,
                "artifact_type": a.artifact_type,
                "title":         a.title or "",
                "content":       a.content or "",
                "tokens_used":   a.tokens_used or 0,
                "cost":          a.cost or 0.0,
            })

        # sync sprint runs
        result3 = await db.execute(
            select(SprintRun).where(SprintRun.project_id == project_id)
        )
        runs = result3.scalars().all()
        for r in runs:
            await supabase_store.save_sprint_run({
                "project_id":    project_id,
                "sprint_number": r.sprint_number,
                "status":        r.status,
                "notes":         r.notes or "",
                "files_written": json.loads(r.files_written) if r.files_written else [],
            })

    log.info("Cache: synced project %s to Supabase", project_id)


async def evict_project_from_cache(project_id: str) -> None:
    """Remove a project from SQLite after syncing to Supabase."""
    from server.database import async_session
    from server.models.project import Project
    from server.models.artifact import Artifact
    from server.models.execution import SprintRun
    from server.models.message import Message
    from server.models.agent_memory import AgentMemory
    from sqlalchemy import select, delete as sql_delete

    # don't evict if pipeline is still running
    from execution.sprint_pipeline import get_pipeline
    if get_pipeline(project_id):
        log.debug("Cache: skipping eviction of %s — pipeline still running", project_id)
        return

    async with async_session() as db:
        await db.execute(sql_delete(SprintRun).where(SprintRun.project_id == project_id))
        await db.execute(sql_delete(Artifact).where(Artifact.project_id == project_id))
        await db.execute(sql_delete(AgentMemory).where(AgentMemory.project_id == project_id))
        await db.execute(sql_delete(Message).where(Message.project_id == project_id))
        result = await db.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if project:
            await db.delete(project)
        await db.commit()

    _last_activity.pop(project_id, None)
    log.info("Cache: evicted project %s from SQLite", project_id)


async def run_eviction_loop() -> None:
    """Background loop — runs every 30 minutes to evict idle/stale projects."""
    while True:
        await asyncio.sleep(EVICTION_LOOP_SECS)
        try:
            await _eviction_pass()
        except Exception as e:
            log.error("Cache eviction error: %s", e, exc_info=True)


async def _eviction_pass() -> None:
    """Single pass: find projects to evict and sync them to Supabase."""
    from server.database import async_session
    from server.models.project import Project
    from sqlalchemy import select

    sqlite_mb = _sqlite_size_mb()
    size_pressure = sqlite_mb > SQLITE_MAX_MB
    log.info("Cache eviction pass: SQLite=%.1f MB, pressure=%s", sqlite_mb, size_pressure)

    async with async_session() as db:
        result = await db.execute(select(Project))
        projects = result.scalars().all()

    # sort by last activity (oldest first = LRU)
    def _age(p: Project) -> float:
        last = _last_activity.get(p.id)
        if last is None:
            return float("inf")
        return (datetime.now(timezone.utc) - last).total_seconds()

    candidates = sorted(projects, key=_age, reverse=True)

    for project in candidates:
        should_evict = _is_idle(project.id) or size_pressure
        if not should_evict:
            continue

        # don't evict active pipelines
        from execution.sprint_pipeline import get_pipeline
        if get_pipeline(project.id):
            continue

        # sync then evict
        try:
            # we don't know user_id here — it's already in Supabase from initial upsert
            await sync_project_to_supabase(project.id)
            await evict_project_from_cache(project.id)
        except Exception as e:
            log.warning("Cache: failed to evict %s: %s", project.id, e)

        # re-check size after each eviction
        if _sqlite_size_mb() <= SQLITE_MAX_MB:
            size_pressure = False
            if not any(_is_idle(p.id) for p in candidates):
                break
