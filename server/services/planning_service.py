"""Planning pipeline service — wraps planning_pipeline with DB + EventBus integration.

Handles:
  - DB status updates (project.status, Artifact records)
  - EventBus events for real-time frontend streaming
  - No workspace management, no app launching — pure planning
"""

import asyncio
import json
from datetime import datetime, timezone

from server.database import async_session
from server.models.project import Project
from server.models.artifact import Artifact
from server.services.event_bus import event_bus, Event

from orchestrator.planning_pipeline import (
    run_planning_pipeline,
    PlanningCallbacks,
    PlanningEvent,
    PlanningResult,
)


async def run_planning(project_id: str, user_story: str,
                       config: dict | None = None) -> PlanningResult:
    """Run the planning pipeline with DB + EventBus integration."""
    import logging
    log = logging.getLogger(__name__)
    log.info("run_planning started for project %s", project_id)

    # Update project status to running
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one()
        project.status = "running"
        await db.commit()

    # Build callback that bridges PlanningEvent → EventBus Event
    async def on_event(pe: PlanningEvent):
        ts = datetime.now().strftime("%H:%M:%S")
        summary = pe.data.get("content", pe.data.get("label", pe.type))
        print(f"  [{ts}] [{pe.type}] {pe.agent}: {str(summary)[:120]}")

        await event_bus.publish(Event(
            type=pe.type,
            project_id=project_id,
            data={"agent": pe.agent, **pe.data},
        ))

        # Save artifacts to DB when produced
        if pe.type == "agent_artifact":
            await _save_artifact(
                project_id=project_id,
                agent=pe.agent,
                artifact_type=pe.data.get("artifact_type", "unknown"),
                content=pe.data.get("content", ""),
                tokens_used=pe.data.get("tokens_used", 0),
            )

    callbacks = PlanningCallbacks(on_event=on_event)

    try:
        planning_result = await run_planning_pipeline(
            project_id=project_id,
            user_story=user_story,
            callbacks=callbacks,
            config=config,
        )

        # Update project status to completed and save stack
        async with async_session() as db:
            from sqlalchemy import select
            stmt = select(Project).where(Project.id == project_id)
            result = await db.execute(stmt)
            project = result.scalar_one()
            project.status = "completed"
            # Save resolved stack in config
            try:
                cfg = json.loads(project.config) if project.config else {}
            except (ValueError, TypeError):
                cfg = {}
            cfg["resolved_stack"] = planning_result.stack
            project.config = json.dumps(cfg)
            await db.commit()

        return planning_result

    except Exception as e:
        # Update project status to failed
        async with async_session() as db:
            from sqlalchemy import select
            stmt = select(Project).where(Project.id == project_id)
            result = await db.execute(stmt)
            project = result.scalar_one_or_none()
            if project:
                project.status = "failed"
                await db.commit()

        await event_bus.publish(Event(
            type="error",
            project_id=project_id,
            data={"message": f"Planning failed: {e}"},
        ))
        raise


async def _save_artifact(project_id: str, agent: str, artifact_type: str,
                         content: str, tokens_used: int = 0):
    """Save an artifact to the database."""
    async with async_session() as db:
        artifact = Artifact(
            project_id=project_id,
            agent=agent,
            artifact_type=artifact_type,
            title=f"{agent} — {artifact_type}",
            content=content,
            tokens_used=tokens_used,
        )
        db.add(artifact)
        await db.commit()


async def get_artifacts(project_id: str) -> list[dict]:
    """Get all artifacts for a project."""
    async with async_session() as db:
        from sqlalchemy import select
        stmt = (
            select(Artifact)
            .where(Artifact.project_id == project_id)
            .order_by(Artifact.created_at)
        )
        result = await db.execute(stmt)
        artifacts = result.scalars().all()
        return [
            {
                "id": a.id,
                "agent": a.agent,
                "artifact_type": a.artifact_type,
                "title": a.title,
                "content": a.content,
                "tokens_used": a.tokens_used,
                "cost": a.cost,
                "created_at": a.created_at.isoformat() if a.created_at else "",
            }
            for a in artifacts
        ]


async def export_markdown(project_id: str) -> str:
    """Export all artifacts as a single markdown document."""
    artifacts = await get_artifacts(project_id)

    # Get project info
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()

    project_name = project.name if project else "Untitled Project"
    user_story = project.user_story if project else ""

    sections = [
        f"# {project_name}",
        f"\n## Project Brief\n{user_story}",
    ]

    section_titles = {
        "requirements": "Requirements",
        "architecture": "Architecture",
        "sprint_plan": "Sprint Plan",
        "test_strategy": "Test Strategy",
    }

    for artifact in artifacts:
        title = section_titles.get(artifact["artifact_type"], artifact["artifact_type"].replace("_", " ").title())
        sections.append(f"\n## {title}\n\n{artifact['content']}")

    sections.append(f"\n---\n*Generated by Entourage Process Engine*")

    return "\n".join(sections)


async def get_work_packages(project_id: str) -> list[dict]:
    """Extract individual work packages from sprint plan artifacts."""
    artifacts = await get_artifacts(project_id)

    work_packages = []
    for artifact in artifacts:
        if artifact["artifact_type"] == "sprint_plan":
            # Parse sprint plan content into individual tasks
            content = artifact["content"]
            work_packages.append({
                "type": "sprint_plan",
                "content": content,
                "agent": artifact["agent"],
            })

    return work_packages
