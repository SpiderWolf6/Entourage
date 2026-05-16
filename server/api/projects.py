"""Project CRUD endpoints."""

import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from server.database import get_session
from server.models.project import Project
from server.models.message import Message
from server.models.artifact import Artifact
from server.models.agent_memory import AgentMemory

router = APIRouter(tags=["projects"])


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
async def create_project(req: CreateProjectRequest, db: AsyncSession = Depends(get_session)):
    project = Project(
        name=req.name or req.user_story[:80],
        user_story=req.user_story,
        config=json.dumps(req.config),
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _to_response(project)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    projects = result.scalars().all()
    return [_to_response(p) for p in projects]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return _to_response(project)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await db.execute(sql_delete(Message).where(Message.project_id == project_id))
    await db.execute(sql_delete(AgentMemory).where(AgentMemory.project_id == project_id))
    await db.execute(sql_delete(Artifact).where(Artifact.project_id == project_id))
    await db.delete(project)
    await db.commit()
    return {"deleted": project_id}


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
