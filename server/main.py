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
    yield


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


    from server.api.projects  import router as projects_router
    from server.api.planning  import router as planning_router
    from server.api.execution import router as execution_router
    from server.api.ws        import router as ws_router

    app.include_router(projects_router,  prefix="/api")
    app.include_router(planning_router,  prefix="/api")
    app.include_router(execution_router, prefix="/api")
    app.include_router(ws_router)

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
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            "frame-src 'self' http://localhost:*;"
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str):
            # api/* and ws/* go to their routers — everything else is the SPA
            if full_path.startswith("api/") or full_path.startswith("ws/"):
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
            index = dist_dir / "index.html"
            return FileResponse(str(index), headers={"Content-Security-Policy": CSP})

    return app


app = create_app()
