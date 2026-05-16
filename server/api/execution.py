"""Execution API — start/stop sprint execution, PL review gates, demo management."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.database import get_session
from server.models.project import Project
from server.services.execution_service import (
    start_execution,
    approve_sprint,
    reject_sprint,
    start_demo,
    stop_demo,
    get_demo_status,
    get_execution_status,
    get_workspace_files,
    get_workspace_file,
)

router = APIRouter(tags=["execution"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class SprintApprovalRequest(BaseModel):
    notes: str = ""


class SprintRejectionRequest(BaseModel):
    notes: str = ""


# ── Execution control ──────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/execute")
async def start_project_execution(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Start sprint-by-sprint code execution for a project.

    Requires the planning pipeline to be completed first.
    Spawns claude coders for each sprint in the background.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        resp = await start_execution(project_id)
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/projects/{project_id}/execution")
async def get_project_execution_status(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Get current execution status, sprint runs, and workspace info."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    return await get_execution_status(project_id)


# ── Sprint gates ───────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/sprints/{sprint_number}/approve")
async def approve_sprint_gate(
    project_id: str,
    sprint_number: int,
    req: SprintApprovalRequest = SprintApprovalRequest(),
    db: AsyncSession = Depends(get_session),
):
    """Project Lead approves a completed sprint — pipeline continues to next sprint."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        return await approve_sprint(project_id, sprint_number)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/projects/{project_id}/sprints/{sprint_number}/reject")
async def reject_sprint_gate(
    project_id: str,
    sprint_number: int,
    req: SprintRejectionRequest = SprintRejectionRequest(),
    db: AsyncSession = Depends(get_session),
):
    """Project Lead rejects a sprint — pipeline is aborted (re-plan and re-execute)."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        return await reject_sprint(project_id, sprint_number, notes=req.notes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Demo management ────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/demo/start")
async def start_project_demo(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Launch the generated project as a live demo.

    Returns the URL(s) where the running app is accessible.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.status not in ("mvp_ready", "executing", "execution_failed", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Project is not ready for demo (status={project.status})",
        )

    try:
        return await start_demo(project_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Demo launch failed: {e}")


@router.post("/projects/{project_id}/demo/stop")
async def stop_project_demo(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Stop the running demo for a project."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    return await stop_demo(project_id)


@router.get("/projects/{project_id}/demo")
async def get_project_demo_status(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Get current demo status and URLs."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    return await get_demo_status(project_id)


# ── Workspace browser ──────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/workspace")
async def list_workspace_files(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    """List all generated source files in the project workspace."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    files = await get_workspace_files(project_id)
    return {"project_id": project_id, "files": files}


@router.get("/projects/{project_id}/workspace/file")
async def read_workspace_file(
    project_id: str,
    path: str,
    db: AsyncSession = Depends(get_session),
):
    """Read a specific file from the project workspace.

    Pass the relative path as ?path=api/routes/main.py
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    # Basic path traversal guard
    import os
    if ".." in path or path.startswith("/") or path.startswith("\\"):
        raise HTTPException(status_code=400, detail="Invalid path")

    content = await get_workspace_file(project_id, path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    return {"path": path, "content": content}
