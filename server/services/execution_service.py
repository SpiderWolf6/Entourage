"""ExecutionService — bridges SprintExecutionPipeline + DemoLauncher → EventBus + DB.

This module owns the execution layer lifecycle:
  1. start_execution(project_id) → build state from DB artifacts, launch pipeline
  2. approve_sprint(project_id, sprint_num) → unblock gate
  3. reject_sprint(project_id, sprint_num) → abort or re-plan
  4. start_demo(project_id) → launch demo server, return URLs
  5. stop_demo(project_id) → tear down demo
  6. get_execution_status(project_id) → current state
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from server.database import async_session
from server.models.project import Project
from server.models.artifact import Artifact
from server.models.execution import SprintRun
from server.services.event_bus import event_bus, Event

from orchestrator.pipeline_state import PipelineState
from execution.sandbox import SandboxManager, WORKSPACE_ROOT
from execution.sprint_pipeline import (
    SprintExecutionPipeline,
    ExecutionCallbacks,
    register_pipeline,
    get_pipeline,
    unregister_pipeline,
)
from execution.demo_launcher import (
    DemoLauncher,
    register_demo,
    get_demo,
    unregister_demo,
)

log = logging.getLogger(__name__)

# Track running execution tasks
_execution_tasks: dict[str, asyncio.Task] = {}


# ── Start execution ────────────────────────────────────────────────────────────

async def start_execution(project_id: str, user_id: str | None = None) -> dict:
    """Start sprint-by-sprint execution for a project.

    Requires: the planning pipeline (EM→PO→Arch→PL→HR) must be completed first.
    """
    # Verify project exists and planning is done
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()
        if not project:
            raise ValueError(f"Project {project_id} not found")
        if project.status not in ("completed", "execution_failed"):
            raise ValueError(
                f"Project must complete planning before execution (status={project.status})"
            )
        if project_id in _execution_tasks and not _execution_tasks[project_id].done():
            raise ValueError("Execution already running")

        # Load config
        try:
            config = json.loads(project.config) if project.config else {}
        except (ValueError, TypeError):
            config = {}

        stack = config.get("resolved_stack", "flask_react")
        user_story = project.user_story
        stored_user_id = config.get("user_id") or user_id

    # Reconstruct PipelineState from DB artifacts
    state = await _rebuild_state(project_id, user_story, stack)
    if not state.sprint_plan:
        raise ValueError("No sprint plan found — run planning pipeline first")

    # Set workspace path
    workspace_dir = Path(WORKSPACE_ROOT) / project_id
    state.project_path = str(workspace_dir)

    # Mark project as executing
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one()
        project.status = "executing"
        project.workspace_path = str(workspace_dir)
        await db.commit()

    # Create sandbox
    sandbox = SandboxManager(project_id=project_id, stack=stack)

    # Build callbacks
    cb = ExecutionCallbacks(on_event=_make_event_callback(project_id))

    # Create and register pipeline
    pipeline = SprintExecutionPipeline(
        project_id=project_id,
        state=state,
        sandbox=sandbox,
        callbacks=cb,
    )
    register_pipeline(project_id, pipeline)

    # Load credentials from Supabase for this user
    creds = None
    try:
        if stored_user_id:
            from server.supabase_store import get_user_saved_creds
            from server.credentials import Credentials
            saved = await get_user_saved_creds(stored_user_id)
            if saved:
                creds = Credentials.from_dict(saved)
        if creds is None:
            # fall back to env (admin path)
            from server.credentials import Credentials
            creds = Credentials.from_env()
    except Exception as e:
        log.warning("Could not load credentials for execution: %s", e)

    # touch cache activity
    from server.cache_manager import touch
    touch(project_id)

    # Launch as background task
    task = asyncio.create_task(
        _run_execution_safe(project_id, pipeline, state, sandbox, stack, config, creds)
    )
    _execution_tasks[project_id] = task

    return {"status": "started", "project_id": project_id}


async def _run_execution_safe(
    project_id: str,
    pipeline: SprintExecutionPipeline,
    state: PipelineState,
    sandbox: SandboxManager,
    stack: str,
    config: dict,
    creds=None,  # server.credentials.Credentials — passed through, not stored
) -> None:
    """Run the execution pipeline with error handling.

    Credentials are passed explicitly through the stack — no os.environ patching.
    The claude coder reads ANTHROPIC_API_KEY from the environment directly
    (claude CLI needs it as an env var), so we set it only for the subprocess env.
    """
    # pass creds to pipeline state so sprint pipeline can forward to claude coder
    if creds and state:
        state.creds = creds
    try:
        results = await pipeline.run()

        # Re-run npm install after agents may have overwritten package.json
        try:
            await sandbox.reinstall_deps()
        except Exception as e:
            log.warning("reinstall_deps failed (non-fatal): %s", e)

        # ── Final Reviewer pass ───────────────────────────────────────────
        # Count real files written (exclude scaffold/memory/docs)
        all_written = [
            f for r in results for f in r.files_written
            if not f.startswith("memory/") and not f.startswith("docs/")
            and f not in ("CLAUDE.md", "requirements.txt")
        ]

        verdict_data = None
        if not all_written:
            log.warning("Skipping reviewer — agents wrote 0 real files (likely auth failure)")
        else:
            from execution.reviewer import FinalReviewer

            reviewer = FinalReviewer(
                workspace_dir=sandbox.workspace_dir,
                project_id=project_id,
            )

            # Collect planning artifacts for context
            planning_artifacts: dict[str, str] = {}
            arch = state.architecture or {}
            if arch.get("em_brief"):
                planning_artifacts["engineering_manager"] = arch["em_brief"]
            if arch.get("po_output"):
                planning_artifacts["product_owner"] = arch["po_output"]
            if arch.get("raw_output"):
                planning_artifacts["architect"] = arch["raw_output"]

            cb_fn = _make_event_callback(project_id)

            async def reviewer_cb(event_type: str, data: dict):
                await cb_fn(event_type, data)

            review_result = await reviewer.run(
                user_story=state.user_story or "",
                planning_artifacts=planning_artifacts,
                on_event=reviewer_cb,
            )

            if review_result.verdict:
                from execution.reviewer import _verdict_to_dict
                verdict_data = _verdict_to_dict(review_result.verdict)

        # All sprints done
        async with async_session() as db:
            from sqlalchemy import select
            stmt = select(Project).where(Project.id == project_id)
            result = await db.execute(stmt)
            project = result.scalar_one_or_none()
            if project:
                project.status = "mvp_ready"
                await db.commit()

        await event_bus.publish(Event(
            type="mvp_ready",
            project_id=project_id,
            data={
                "total_sprints": len(results),
                "workspace": str(sandbox.workspace_dir),
                "files": sandbox.list_files(),
                "verdict": verdict_data,
            },
        ))

    except Exception as e:
        import traceback
        log.error("Execution failed for %s:\n%s", project_id, traceback.format_exc())

        async with async_session() as db:
            from sqlalchemy import select
            stmt = select(Project).where(Project.id == project_id)
            result = await db.execute(stmt)
            project = result.scalar_one_or_none()
            if project:
                project.status = "execution_failed"
                await db.commit()

        await event_bus.publish(Event(
            type="execution_error",
            project_id=project_id,
            data={"message": str(e)},
        ))

    finally:
        _restore()
        unregister_pipeline(project_id)
        _execution_tasks.pop(project_id, None)


# ── Sprint gates ───────────────────────────────────────────────────────────────

async def approve_sprint(project_id: str, sprint_number: int) -> dict:
    """Approve a sprint review gate — pipeline will continue to next sprint."""
    pipeline = get_pipeline(project_id)
    if not pipeline:
        raise ValueError(f"No active execution pipeline for project {project_id}")
    pipeline.approve_sprint(sprint_number)

    # Save approval to DB
    async with async_session() as db:
        run = SprintRun(
            project_id=project_id,
            sprint_number=sprint_number,
            status="approved",
        )
        db.add(run)
        await db.commit()

    await event_bus.publish(Event(
        type="sprint_approved",
        project_id=project_id,
        data={"sprint": sprint_number},
    ))
    return {"status": "approved", "sprint": sprint_number}


async def reject_sprint(project_id: str, sprint_number: int, notes: str = "") -> dict:
    """Reject / request changes for a sprint. Pipeline will be updated and retried."""
    pipeline = get_pipeline(project_id)
    if not pipeline:
        raise ValueError(f"No active execution pipeline for project {project_id}")
    pipeline.reject_sprint(sprint_number, reason=notes)

    async with async_session() as db:
        run = SprintRun(
            project_id=project_id,
            sprint_number=sprint_number,
            status="rejected",
            notes=notes,
        )
        db.add(run)
        await db.commit()

    await event_bus.publish(Event(
        type="sprint_rejected",
        project_id=project_id,
        data={"sprint": sprint_number, "notes": notes},
    ))
    return {"status": "rejected", "sprint": sprint_number}


# ── Demo management ────────────────────────────────────────────────────────────

async def start_demo(project_id: str) -> dict:
    """Launch the demo server for a completed project."""
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()
        if not project:
            raise ValueError(f"Project {project_id} not found")

        try:
            config = json.loads(project.config) if project.config else {}
        except (ValueError, TypeError):
            config = {}

        stack = config.get("resolved_stack", "flask_react")
        workspace_path = project.workspace_path or str(Path(WORKSPACE_ROOT) / project_id)

    workspace_dir = Path(workspace_path)
    if not workspace_dir.exists():
        raise ValueError(f"Workspace not found: {workspace_path}")

    # Stop existing demo if running
    existing = get_demo(project_id)
    if existing:
        await existing.stop()
        unregister_demo(project_id)

    launcher = DemoLauncher(
        workspace_dir=workspace_dir,
        stack=stack,
        project_id=project_id,
        venv_dir=workspace_dir / ".venv",
    )
    register_demo(project_id, launcher)

    async def on_demo_event(event_type: str, data: dict):
        await event_bus.publish(Event(
            type=f"demo_{event_type}",
            project_id=project_id,
            data=data,
        ))

    status = await launcher.start(on_event=on_demo_event)

    # Persist URLs in project config
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()
        if project:
            try:
                cfg = json.loads(project.config) if project.config else {}
            except (ValueError, TypeError):
                cfg = {}
            cfg["demo_urls"] = status.urls
            cfg["demo_primary_url"] = status.primary_url
            project.config = json.dumps(cfg)
            await db.commit()

    return {
        "status": status.status,
        "urls": status.urls,
        "primary_url": status.primary_url,
        "services": status.services,
    }


async def stop_demo(project_id: str) -> dict:
    """Stop the running demo server."""
    launcher = get_demo(project_id)
    if not launcher:
        return {"status": "not_running"}

    await launcher.stop()
    unregister_demo(project_id)
    return {"status": "stopped"}


async def get_demo_status(project_id: str) -> dict:
    """Get current demo status."""
    launcher = get_demo(project_id)
    if not launcher:
        return {"project_id": project_id, "status": "stopped", "urls": {}}
    status = launcher.get_status()
    return {
        "project_id": project_id,
        "status": status.status,
        "urls": status.urls,
        "primary_url": status.primary_url,
        "services": status.services,
    }


# ── Execution status ───────────────────────────────────────────────────────────

async def get_execution_status(project_id: str) -> dict:
    """Get current execution status for a project."""
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()
        if not project:
            return {"project_id": project_id, "status": "not_found"}

        # Get sprint runs
        stmt2 = (
            select(SprintRun)
            .where(SprintRun.project_id == project_id)
            .order_by(SprintRun.sprint_number)
        )
        result2 = await db.execute(stmt2)
        sprint_runs = result2.scalars().all()

    running = (
        project_id in _execution_tasks and
        not _execution_tasks[project_id].done()
    )

    try:
        config = json.loads(project.config) if project.config else {}
    except (ValueError, TypeError):
        config = {}

    return {
        "project_id": project_id,
        "status": project.status,
        "running": running,
        "workspace_path": project.workspace_path or "",
        "sprint_runs": [
            {
                "sprint": r.sprint_number,
                "status": r.status,
                "notes": r.notes or "",
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in sprint_runs
        ],
        "demo_urls": config.get("demo_urls", {}),
        "demo_primary_url": config.get("demo_primary_url", ""),
    }


async def get_workspace_files(project_id: str) -> list[dict]:
    """List all files in the project workspace."""
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()
        if not project:
            return []

    workspace_path = project.workspace_path or str(Path(WORKSPACE_ROOT) / project_id)
    sandbox = SandboxManager(project_id=project_id)
    sandbox.workspace_dir = Path(workspace_path)

    files = []
    for rel in sandbox.list_files():
        content = sandbox.read_file(rel)
        files.append({
            "path": rel,
            "size": len(content) if content else 0,
        })
    return files


async def get_workspace_file(project_id: str, rel_path: str) -> str | None:
    """Read a single file from the workspace."""
    async with async_session() as db:
        from sqlalchemy import select
        stmt = select(Project).where(Project.id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()
        if not project:
            return None

    workspace_path = project.workspace_path or str(Path(WORKSPACE_ROOT) / project_id)
    sandbox = SandboxManager(project_id=project_id)
    sandbox.workspace_dir = Path(workspace_path)
    return sandbox.read_file(rel_path)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_event_callback(project_id: str):
    """Create an async callback that publishes execution events to EventBus."""
    async def on_event(event_type: str, data: dict):
        ts = datetime.now().strftime("%H:%M:%S")
        log.info("[%s] [exec:%s] %s: %s", ts, project_id, event_type,
                 str(data)[:120])
        await event_bus.publish(Event(
            type=event_type,
            project_id=project_id,
            data=data,
        ))

        # Persist sprint results to DB
        if event_type == "sprint_done":
            sprint_num = data.get("sprint", 0)
            async with async_session() as db:
                run = SprintRun(
                    project_id=project_id,
                    sprint_number=sprint_num,
                    status=data.get("status", "done"),
                    files_written=json.dumps(data.get("files_written", [])),
                )
                db.add(run)
                await db.commit()

    return on_event


async def _rebuild_state(project_id: str, user_story: str, stack: str) -> PipelineState:
    """Reconstruct a PipelineState from DB artifacts (produced during planning)."""
    state = PipelineState(
        project_id=project_id,
        project_path="",
        user_story=user_story,
        stack=stack,
    )
    state.architecture = {}

    async with async_session() as db:
        from sqlalchemy import select
        stmt = (
            select(Artifact)
            .where(Artifact.project_id == project_id)
            .order_by(Artifact.created_at)
        )
        result = await db.execute(stmt)
        artifacts = result.scalars().all()

    for artifact in artifacts:
        atype = artifact.artifact_type
        content = artifact.content or ""

        if atype == "requirements":
            state.architecture["po_output"] = content
        elif atype == "architecture":
            state.architecture["raw_output"] = content
            # Parse architect sections
            from utils.parser import parse_architect_output
            sections = parse_architect_output(content)
            state.architecture.update(sections)
        elif atype == "sprint_plan":
            from utils.parser import parse_sprint_plan
            plan = parse_sprint_plan(content)
            if plan and "sprints" in plan:
                state.sprint_plan = plan["sprints"]
        elif atype == "team_roster":
            from agents.hr import _parse_spawn_agents
            spawned = _parse_spawn_agents(content)
            state.architecture["spawned_agents"] = spawned
            state.architecture["hr_output"] = content
        elif atype == "brief":
            state.architecture["em_brief"] = content

    return state
