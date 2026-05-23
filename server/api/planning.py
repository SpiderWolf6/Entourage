"""Planning pipeline API — start runs, get artifacts, export markdown."""

import asyncio
import json
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from server.database import get_session
from server.models.project import Project
from server.services.planning_service import (
    run_planning,
    get_artifacts,
    export_markdown,
    get_work_packages,
)
from server.credentials import Credentials

log = logging.getLogger(__name__)
router = APIRouter(tags=["planning"])

# track running planning tasks
_running_tasks: dict[str, asyncio.Task] = {}


class RunPlanningRequest(BaseModel):
    # no longer needs credentials — they come from Supabase user_metadata
    # kept for backwards compat but ignored if user has saved creds
    credentials: dict[str, str | bool] = {}


class POAnswerRequest(BaseModel):
    answer: str


def _resolve_credentials(
    creds_dict: dict,
    user_id: str | None = None,
) -> Credentials:
    """Resolve credentials from the request.

    Priority:
    1. Admin path: use_env_creds=True + valid ADMIN_PASSWORD → read from os.environ
    2. user_id provided → fetch saved creds from Supabase (called separately)
    3. Explicit creds in request body → use those
    """
    if creds_dict.get("use_env_creds"):
        admin_pw = os.environ.get("ADMIN_PASSWORD", "")
        submitted = str(creds_dict.get("admin_password", ""))
        if not admin_pw or submitted != admin_pw:
            raise PermissionError("Invalid admin password")
        return Credentials.from_env()

    creds = Credentials.from_dict(creds_dict)
    creds.validate()
    return creds


@router.post("/projects/{project_id}/run")
@router.post("/projects/{project_id}/plan")
async def start_planning(
    project_id: str,
    req: RunPlanningRequest = RunPlanningRequest(),
    db: AsyncSession = Depends(get_session),
    x_user_id: Optional[str] = Header(None),
):
    """Start the planning pipeline for a project (runs in background)."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.status == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    if project_id in _running_tasks and not _running_tasks[project_id].done():
        raise HTTPException(status_code=409, detail="Pipeline already running")

    # resolve credentials — try saved creds from Supabase first, then request body
    try:
        if x_user_id and not req.credentials:
            from server.supabase_store import get_user_saved_creds
            saved = await get_user_saved_creds(x_user_id)
            creds = _resolve_credentials(saved)
        else:
            creds = _resolve_credentials(req.credentials)
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    # persist user_id on project config (for cache sync)
    try:
        config = json.loads(project.config) if project.config else {}
    except (ValueError, TypeError):
        config = {}

    if x_user_id:
        config["user_id"] = x_user_id

    project.config = json.dumps(config)
    await db.commit()

    # touch the cache activity tracker
    from server.cache_manager import touch
    touch(project_id)

    # launch planning as background task — pass credentials through the stack
    task = asyncio.create_task(
        _run_planning_safe(project_id, project.user_story, config, creds)
    )
    _running_tasks[project_id] = task

    return {"status": "started", "project_id": project_id}


@router.get("/projects/{project_id}/artifacts")
async def list_artifacts(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")
    artifacts = await get_artifacts(project_id)
    return {"project_id": project_id, "artifacts": artifacts}


@router.get("/projects/{project_id}/artifacts/{artifact_type}")
async def get_artifact_by_type(
    project_id: str, artifact_type: str, db: AsyncSession = Depends(get_session)
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")
    artifacts = await get_artifacts(project_id)
    matching = [a for a in artifacts if a["artifact_type"] == artifact_type]
    if not matching:
        raise HTTPException(status_code=404, detail=f"No {artifact_type} artifact found")
    return matching[-1]


@router.get("/projects/{project_id}/export/markdown")
async def export_project_markdown(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")
    md = await export_markdown(project_id)
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/projects/{project_id}/work-packages")
async def list_work_packages(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")
    packages = await get_work_packages(project_id)
    return {"project_id": project_id, "work_packages": packages}


@router.post("/projects/{project_id}/po-answer")
async def submit_po_answer(
    project_id: str, req: POAnswerRequest, db: AsyncSession = Depends(get_session)
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")
    from orchestrator.clarification_bus import clarification_bus
    clarification_bus.submit_answer(project_id, req.answer)
    return {"status": "received"}


@router.get("/projects/{project_id}/plan-status")
async def get_planning_status(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    running = project_id in _running_tasks and not _running_tasks[project_id].done()
    return {"project_id": project_id, "status": project.status, "running": running}


async def _run_planning_safe(
    project_id: str,
    user_story: str,
    config: dict,
    creds: Credentials,
) -> None:
    """Run the planning pipeline — credentials passed explicitly, no env patching."""
    log.info("_run_planning_safe started for project %s", project_id)
    try:
        await run_planning(project_id, user_story, config, creds=creds)
    except Exception as e:
        log.error("Planning failed for project %s: %s", project_id, e, exc_info=True)

        from server.database import async_session
        from server.services.event_bus import event_bus, Event

        async with async_session() as db:
            result = await db.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project:
                project.status = "failed"
                await db.commit()

        await event_bus.publish(Event(
            type="error",
            project_id=project_id,
            data={"message": f"Planning failed: {str(e)}"},
        ))
    finally:
        _running_tasks.pop(project_id, None)
        # sync to Supabase on completion
        try:
            user_id = config.get("user_id")
            from server.cache_manager import sync_project_to_supabase
            await sync_project_to_supabase(project_id, user_id)
        except Exception as e:
            log.warning("Failed to sync project %s to Supabase after planning: %s", project_id, e)
