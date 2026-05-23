"""Project CRUD endpoints.

Architecture:
- Supabase = source of truth (long-term storage, per-user isolation)
- SQLite   = hot cache (fast reads during active pipelines)

Create → write to SQLite first (fast), async write to Supabase.
List   → read from Supabase (authoritative, user-scoped).
Get    → check SQLite cache first, fall back to Supabase if not cached.
Delete → delete from both.
"""

import json
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from server.database import get_session
from server.models.project import Project
from server.models.message import Message
from server.models.artifact import Artifact
from server.models.agent_memory import AgentMemory
from server.models.execution import SprintRun

log = logging.getLogger(__name__)
router = APIRouter(tags=["projects"])

ADMIN_EMAIL = "smukherjee39@wisc.edu"


class CreateProjectRequest(BaseModel):
    name: str = ""
    user_story: str
    config: dict = {}


class ProjectResponse(BaseModel):
    id: str
    name: str
    user_story: str
    status: str
    workspace_path: str
    config: dict
    created_at: str
    updated_at: str


@router.post("/projects", response_model=ProjectResponse)
async def create_project(
    req: CreateProjectRequest,
    db: AsyncSession = Depends(get_session),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Create a project in SQLite (cache) and Supabase (source of truth)."""
    now = datetime.now(timezone.utc)
    config = dict(req.config)
    if x_user_id:
        config["user_id"] = x_user_id

    project = Project(
        name=req.name or req.user_story[:80],
        user_story=req.user_story,
        config=json.dumps(config),
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    # touch cache activity
    from server.cache_manager import touch
    touch(project.id)

    # write to Supabase asynchronously (don't block the response)
    if x_user_id:
        try:
            from server import supabase_store
            await supabase_store.upsert_project({
                "id":          project.id,
                "user_id":     x_user_id,
                "name":        project.name,
                "user_story":  project.user_story,
                "status":      project.status,
                "workspace_path": "",
                "config":      config,
                "created_at":  now.isoformat(),
                "updated_at":  now.isoformat(),
            })
        except Exception as e:
            log.warning("Supabase project create failed (non-fatal): %s", e)

    return _to_response(project)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(
    db: AsyncSession = Depends(get_session),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """List projects.

    - Authenticated users: fetch from Supabase (user-scoped).
    - Admin: fetch all from Supabase.
    - No auth: fall back to SQLite (local dev).
    """
    if x_user_id:
        try:
            from server import supabase_store
            is_admin = x_user_email == ADMIN_EMAIL
            if is_admin:
                sb_projects = await supabase_store.list_all_projects()
            else:
                sb_projects = await supabase_store.list_projects_for_user(x_user_id)
            return [_sb_to_response(p) for p in sb_projects]
        except Exception as e:
            log.warning("Supabase list failed, falling back to SQLite: %s", e)

    # fallback: SQLite (local dev without auth)
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    projects = result.scalars().all()
    return [_to_response(p) for p in projects]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    x_user_id: Optional[str] = Header(None),
):
    """Get a project — check SQLite cache first, fall back to Supabase."""
    # check SQLite cache
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project:
        from server.cache_manager import touch
        touch(project_id)
        return _to_response(project)

    # not in cache — load from Supabase
    if x_user_id:
        try:
            from server.cache_manager import load_project_into_cache
            await load_project_into_cache(project_id)
            result2 = await db.execute(select(Project).where(Project.id == project_id))
            project = result2.scalar_one_or_none()
            if project:
                return _to_response(project)
        except Exception as e:
            log.warning("Supabase load failed: %s", e)

    raise HTTPException(status_code=404, detail="Project not found")


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    x_user_id: Optional[str] = Header(None),
):
    """Delete project from both SQLite cache and Supabase."""
    # delete from SQLite
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project:
        await db.execute(sql_delete(Message).where(Message.project_id == project_id))
        await db.execute(sql_delete(AgentMemory).where(AgentMemory.project_id == project_id))
        await db.execute(sql_delete(Artifact).where(Artifact.project_id == project_id))
        await db.execute(sql_delete(SprintRun).where(SprintRun.project_id == project_id))
        await db.delete(project)
        await db.commit()

    # delete from Supabase
    try:
        from server import supabase_store
        await supabase_store.delete_project(project_id)
    except Exception as e:
        log.warning("Supabase delete failed: %s", e)

    return {"deleted": project_id}


@router.delete("/projects/{project_id}/workspace")
async def delete_project_workspace(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Delete the workspace directory for a project (frees disk space)."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    from execution.sandbox import WORKSPACE_ROOT
    workspace = Path(WORKSPACE_ROOT) / project_id
    if workspace.exists():
        shutil.rmtree(workspace)
        project.workspace_path = ""
        await db.commit()
        return {"deleted_workspace": project_id}
    return {"deleted_workspace": None, "message": "No workspace found"}


@router.post("/admin/nuke")
async def nuke_everything(
    db: AsyncSession = Depends(get_session),
    x_user_email: Optional[str] = Header(None),
):
    """NUCLEAR — wipe all workspaces from disk AND all records from SQLite + Supabase.

    Admin-only (smukherjee39@wisc.edu).
    """
    if x_user_email != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")

    from execution.sandbox import WORKSPACE_ROOT

    def _dir_size(path: Path) -> int:
        try:
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except Exception:
            return 0

    ws_root = Path(WORKSPACE_ROOT)
    size_before = _dir_size(ws_root)

    # delete each project workspace subdirectory individually
    # (avoids deleting the root /data/workspaces dir itself, and skips locked dirs)
    ws_root.mkdir(parents=True, exist_ok=True)
    for child in list(ws_root.iterdir()):
        if child.is_dir():
            try:
                shutil.rmtree(child)
            except Exception as e:
                log.warning("Could not remove workspace %s: %s", child, e)

    # wipe SQLite
    await db.execute(sql_delete(SprintRun))
    await db.execute(sql_delete(Artifact))
    await db.execute(sql_delete(AgentMemory))
    await db.execute(sql_delete(Message))
    await db.execute(sql_delete(Project))
    await db.commit()
    # reclaim freed pages so the file actually shrinks on disk
    from sqlalchemy import text
    await db.execute(text("VACUUM"))

    # wipe Supabase
    try:
        from server import supabase_store
        await supabase_store.delete_all_projects()
    except Exception as e:
        log.warning("Supabase nuke failed: %s", e)

    log.warning("NUKE: full hard reset. disk before=%d bytes", size_before)

    return {
        "nuked": True,
        "size_before_mb": round(size_before / 1024 / 1024, 2),
        "size_after_bytes": _dir_size(ws_root),
    }


def _to_response(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "user_story": project.user_story,
        "status": project.status,
        "workspace_path": project.workspace_path or "",
        "config": json.loads(project.config) if project.config else {},
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _sb_to_response(p: dict) -> dict:
    """Convert a Supabase project row to ProjectResponse format."""
    config = p.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}
    return {
        "id":             p["id"],
        "name":           p.get("name", ""),
        "user_story":     p.get("user_story", ""),
        "status":         p.get("status", "created"),
        "workspace_path": p.get("workspace_path", ""),
        "config":         config,
        "created_at":     p.get("created_at", ""),
        "updated_at":     p.get("updated_at", ""),
    }
