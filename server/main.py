"""fastapi app factory for the entourage process engine.

the ProactorEventLoop policy must be set before any asyncio code runs on windows.
uvicorn's default SelectorEventLoop does not support asyncio.create_subprocess_exec,
which the sandbox and demo launcher both use — so we force Proactor at module load.

static file serving: if frontend/dist exists (i.e. after `npm run build`), fastapi
serves the vite output directly. this means one process, one port in production —
no separate node server needed on fly.io.
"""

import sys
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# must be before any asyncio.get_event_loop() calls — uvicorn may set its own policy
# later on linux, but on linux subprocess works with either loop so it's fine to set here
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from server.config import settings
from server.database import init_db
from agents.registry import discover_agents


@asynccontextmanager
async def lifespan(app: FastAPI):
    # initialize sqlite tables and register agent classes on startup
    await init_db()
    discover_agents()

    # start background tasks
    from server.cache_manager import run_eviction_loop
    eviction_task = asyncio.create_task(run_eviction_loop())
    demo_idle_task = asyncio.create_task(_demo_idle_killer())

    yield

    # clean shutdown
    eviction_task.cancel()
    demo_idle_task.cancel()


async def _demo_idle_killer():
    """Kill demos that have been idle for more than 30 minutes.

    Frees ports and resets project status to mvp_ready so users can relaunch.
    Runs every 5 minutes.
    """
    from execution.demo_launcher import get_demo, unregister_demo, DEMO_IDLE_TIMEOUT_SECS
    from server.database import async_session
    from server.models.project import Project
    from sqlalchemy import select

    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            from execution.demo_launcher import _active_demos
            for project_id in list(_active_demos.keys()):
                launcher = get_demo(project_id)
                if launcher and launcher.idle_seconds() > DEMO_IDLE_TIMEOUT_SECS:
                    log.info("Demo idle timeout for project %s — killing", project_id)
                    await launcher.stop()
                    unregister_demo(project_id)
                    # reset project status so Launch button reappears
                    async with async_session() as db:
                        result = await db.execute(
                            select(Project).where(Project.id == project_id)
                        )
                        project = result.scalar_one_or_none()
                        if project and project.status != "mvp_ready":
                            project.status = "mvp_ready"
                            await db.commit()
        except Exception as e:
            log.warning("Demo idle killer error: %s", e)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Entourage Process Engine",
        version="0.3.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


    from server.api.projects   import router as projects_router
    from server.api.planning   import router as planning_router
    from server.api.execution  import router as execution_router
    from server.api.ws         import router as ws_router
    from server.api.demo_proxy import router as demo_proxy_router

    app.include_router(projects_router,  prefix="/api")
    app.include_router(planning_router,  prefix="/api")
    app.include_router(execution_router, prefix="/api")
    app.include_router(ws_router)
    app.include_router(demo_proxy_router)

    @app.get("/api/health")
    async def health():
        from agents.registry import AgentRegistry
        return {"status": "ok", "agents": AgentRegistry.list_agents()}

    # serve the built react frontend as static files if the dist directory exists.
    # in development the vite dev server runs separately; in production (fly.io)
    # we do `npm run build` in the dockerfile and serve from here.
    import os
    from pathlib import Path
    dist_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist_dir.exists():
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse

        # mount /assets separately so static files get cache headers
        app.mount("/assets", StaticFiles(directory=str(dist_dir / "assets")), name="assets")

        CSP = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https://lh3.googleusercontent.com; "
            "connect-src 'self' ws: wss: https://*.supabase.co https://accounts.google.com; "
            "frame-src 'self' http://localhost:* https://accounts.google.com;"
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str):
            # api/* and ws/* go to their routers — everything else is the SPA
            if full_path.startswith("api/") or full_path.startswith("ws/"):
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
            # serve static files from dist root (icons, favicon, etc.) if they exist
            static_file = dist_dir / full_path
            if full_path and static_file.exists() and static_file.is_file():
                return FileResponse(str(static_file))
            index = dist_dir / "index.html"
            return FileResponse(str(index), headers={"Content-Security-Policy": CSP})

    return app


app = create_app()
