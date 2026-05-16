"""Planning pipeline API — start runs, get artifacts, export markdown."""

import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.database import get_session
from server.models.project import Project
from server.services.planning_service import (
    run_planning,
    get_artifacts,
    export_markdown,
    get_work_packages,
)

router = APIRouter(tags=["planning"])

# Track running planning tasks
_running_tasks: dict[str, asyncio.Task] = {}


class RunPlanningRequest(BaseModel):
    credentials: dict[str, str | bool] = {}  # API credentials (or use_env_creds=True for admin)


class POAnswerRequest(BaseModel):
    answer: str


@router.post("/projects/{project_id}/run")
@router.post("/projects/{project_id}/plan")
async def start_planning(
    project_id: str,
    req: RunPlanningRequest = RunPlanningRequest(),
    db: AsyncSession = Depends(get_session),
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

    # Read config from project, store credentials for pipeline use
    try:
        config = json.loads(project.config) if project.config else {}
    except (ValueError, TypeError):
        config = {}

    if req.credentials:
        config["credentials"] = dict(req.credentials)

    # Persist config (so execution phase can also read creds)
    project.config = json.dumps(config)
    await db.commit()

    # Launch planning as a background task
    task = asyncio.create_task(
        _run_planning_safe(project_id, project.user_story, config)
    )
    _running_tasks[project_id] = task

    return {"status": "started", "project_id": project_id}


@router.get("/projects/{project_id}/artifacts")
async def list_artifacts(project_id: str, db: AsyncSession = Depends(get_session)):
    """Get all planning artifacts for a project."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    artifacts = await get_artifacts(project_id)
    return {"project_id": project_id, "artifacts": artifacts}


@router.get("/projects/{project_id}/artifacts/{artifact_type}")
async def get_artifact_by_type(
    project_id: str, artifact_type: str, db: AsyncSession = Depends(get_session)
):
    """Get a specific artifact by type."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    artifacts = await get_artifacts(project_id)
    matching = [a for a in artifacts if a["artifact_type"] == artifact_type]
    if not matching:
        raise HTTPException(status_code=404, detail=f"No {artifact_type} artifact found")

    return matching[-1]  # Return the latest one


@router.get("/projects/{project_id}/export/markdown")
async def export_project_markdown(
    project_id: str, db: AsyncSession = Depends(get_session)
):
    """Export the full project plan as markdown."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    md = await export_markdown(project_id)
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/projects/{project_id}/work-packages")
async def list_work_packages(
    project_id: str, db: AsyncSession = Depends(get_session)
):
    """Get structured work packages from the sprint plan."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    packages = await get_work_packages(project_id)
    return {"project_id": project_id, "work_packages": packages}


@router.post("/projects/{project_id}/po-answer")
async def submit_po_answer(
    project_id: str, req: POAnswerRequest, db: AsyncSession = Depends(get_session)
):
    """Submit the user's answer to Product Owner clarification questions."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")
    from orchestrator.clarification_bus import clarification_bus
    clarification_bus.submit_answer(project_id, req.answer)
    return {"status": "received"}


@router.get("/projects/{project_id}/plan-status")
async def get_planning_status(
    project_id: str, db: AsyncSession = Depends(get_session)
):
    """Get current planning pipeline status."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    running = project_id in _running_tasks and not _running_tasks[project_id].done()

    return {
        "project_id": project_id,
        "status": project.status,
        "running": running,
    }


async def _run_planning_safe(project_id: str, user_story: str,
                              config: dict | None = None):
    """Run the planning pipeline with error handling."""
    import logging
    log = logging.getLogger(__name__)
    config = config or {}
    _restore = _apply_credentials(config.get("credentials", {}))
    try:
        await run_planning(project_id, user_story, config)
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
        _restore()
        _running_tasks.pop(project_id, None)


def _apply_credentials(creds: dict) -> "callable":
    """Temporarily patch os.environ with user-supplied credentials.

    If use_env_creds=True, skips patching (admin shortcut — uses server's saved env).
    Returns a restore() callable that puts the original values back.

    Runs in the async event loop (single-threaded), so no race between projects.
    """
    import os
    if not creds or creds.get("use_env_creds"):
        # Admin shortcut or no creds provided — validate admin password if present
        if creds.get("admin_password"):
            admin_pw = os.environ.get("ADMIN_PASSWORD", "")
            if admin_pw and str(creds["admin_password"]) != admin_pw:
                raise PermissionError("Invalid admin password")
        return lambda: None

    key_map = {
        "azure_openai_api_key":        "AZURE_OPENAI_API_KEY",
        "azure_openai_endpoint":       "AZURE_OPENAI_ENDPOINT",
        "azure_openai_deployment":     "AZURE_OPENAI_DEPLOYMENT_MINI",
        "azure_openai_deployment_full":"AZURE_OPENAI_DEPLOYMENT_FULL",
        "anthropic_api_key":           "ANTHROPIC_API_KEY",
    }

    original: dict[str, str | None] = {}
    for field, env_var in key_map.items():
        val = str(creds.get(field, "")).strip()
        if val:
            original[env_var] = os.environ.get(env_var)
            os.environ[env_var] = val

    def restore():
        for env_var, old_val in original.items():
            if old_val is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = old_val

    return restore
