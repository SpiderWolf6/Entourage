"""SupabaseStore — long-term persistence layer for projects, artifacts, sprint runs.

supabase-py is a synchronous library. All calls are wrapped in run_in_executor
so they don't block the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from server.supabase_client import get_supabase

log = logging.getLogger(__name__)


async def _sb(fn):
    """Run a synchronous supabase-py call in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn)


# ── Projects ──────────────────────────────────────────────────────────────────

async def upsert_project(project: dict) -> dict:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("projects").upsert(project).execute())
    return result.data[0] if result.data else project


async def get_project(project_id: str) -> dict | None:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("projects").select("*").eq("id", project_id).execute())
    return result.data[0] if result.data else None


async def list_projects_for_user(user_id: str) -> list[dict]:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("projects").select("*").eq("user_id", user_id).order("created_at", desc=True).execute())
    return result.data or []


async def list_all_projects() -> list[dict]:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("projects").select("*").order("created_at", desc=True).execute())
    return result.data or []


async def update_project_status(project_id: str, status: str, extra: dict | None = None) -> None:
    sb = get_supabase()
    payload: dict[str, Any] = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    await _sb(lambda: sb.table("projects").update(payload).eq("id", project_id).execute())


async def delete_project(project_id: str) -> None:
    sb = get_supabase()
    await _sb(lambda: sb.table("projects").delete().eq("id", project_id).execute())


async def delete_all_projects() -> None:
    """Admin nuclear reset — deletes every project (cascades to artifacts + sprint_runs)."""
    sb = get_supabase()
    # delete all rows — use neq on a field that's always set to match everything
    await _sb(lambda: sb.table("sprint_runs").delete().neq("id", 0).execute())
    await _sb(lambda: sb.table("artifacts").delete().neq("id", 0).execute())
    await _sb(lambda: sb.table("projects").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute())


# ── Artifacts ─────────────────────────────────────────────────────────────────

async def save_artifact(artifact: dict) -> dict:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("artifacts").insert(artifact).execute())
    return result.data[0] if result.data else artifact


async def get_artifacts(project_id: str) -> list[dict]:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("artifacts").select("*").eq("project_id", project_id).order("created_at").execute())
    return result.data or []


async def delete_artifacts(project_id: str) -> None:
    sb = get_supabase()
    await _sb(lambda: sb.table("artifacts").delete().eq("project_id", project_id).execute())


# ── Sprint runs ───────────────────────────────────────────────────────────────

async def save_sprint_run(run: dict) -> dict:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("sprint_runs").insert(run).execute())
    return result.data[0] if result.data else run


async def get_sprint_runs(project_id: str) -> list[dict]:
    sb = get_supabase()
    result = await _sb(lambda: sb.table("sprint_runs").select("*").eq("project_id", project_id).order("sprint_number").execute())
    return result.data or []


async def delete_sprint_runs(project_id: str) -> None:
    sb = get_supabase()
    await _sb(lambda: sb.table("sprint_runs").delete().eq("project_id", project_id).execute())


# ── User credentials ──────────────────────────────────────────────────────────

async def get_user_saved_creds(user_id: str) -> dict:
    """Fetch saved API credentials from Supabase auth user_metadata."""
    sb = get_supabase()
    try:
        result = await _sb(lambda: sb.auth.admin.get_user_by_id(user_id))
        if result and result.user:
            return result.user.user_metadata.get("saved_creds", {})
    except Exception as e:
        log.warning("get_user_saved_creds failed: %s", e)
    return {}
