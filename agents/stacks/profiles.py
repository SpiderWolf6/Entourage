"""stack profiles — immutable tech-stack definitions that drive agent instructions.

a StackProfile tells agents:
  - what frameworks to use (flask, react)
  - how to structure the workspace
  - which launch commands start the app
  - which agents to spawn for coding
  - how to run tests

only flask_react is supported right now. the auto-detect helpers always return it.
adding a new stack means adding a StackProfile entry here and updating the sandbox
setup steps in execution/sandbox.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StackProfile:
    """immutable description of a technology stack.

    frozen=True means instances are hashable and can't be accidentally mutated
    mid-pipeline, which matters because the same profile is shared across agents.
    """

    name: str
    description: str
    backend_framework: str
    frontend_framework: str

    # injected verbatim into the architect agent's task description
    architect_instructions: str

    # injected into each dev agent's task description
    dev_instructions: str

    workspace_setup: str

    # ordered steps the sandbox runs during setup (python_venv, npm_frontend, etc.)
    setup_steps: tuple[str, ...] = ("python_venv", "npm_frontend")

    # (service_name, command) pairs — service names must match PORT_MAP in demo_launcher
    launch_commands: tuple[tuple[str, str], ...] = (
        ("backend", "python api/run.py"),
        ("frontend", "npm run dev"),
    )

    # default developer agent archetypes spawned by HR
    default_agents: tuple[str, ...] = ("python_dev", "react_dev")

    build_command: str = ""
    test_framework: str = "pytest"
    test_command: str = "python -m pytest -v --tb=short -q"


_FLASK_REACT = StackProfile(
    name="Flask + React",
    description="Python Flask JSON API backend with a Vite-powered React SPA frontend.",
    backend_framework="flask",
    frontend_framework="react",
    architect_instructions=(
        "Design the system as two separate applications:\n"
        "1. A Flask JSON API under `api/` — use an app-factory pattern "
        "(create_app), SQLAlchemy models in `api/models/`, a SINGLE Blueprint "
        "in `api/routes/main.py`, and `api/extensions.py` for shared instances (db).\n"
        "2. A React SPA under `frontend/` — bootstrapped with Vite, using "
        "react-router-dom for routing and plain fetch() for API calls.\n"
        "All API routes must be prefixed with /api/. "
        "Enable CORS in the Flask app factory for http://localhost:9001. "
        "The Vite dev-server proxies /api to Flask on port 9000.\n"
        "Backend runs on port 9000. Frontend runs on port 9001. Hardcode these everywhere.\n"
        "Entry point: api/run.py MUST contain: "
        "`import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`\n"
        "PACKAGE_JSON devDependencies MUST include: vite, @vitejs/plugin-react, @babel/core. "
        "Without all three the dev server cannot start."
    ),
    dev_instructions=(
        "Backend: Python 3.11+, Flask, Flask-SQLAlchemy, Flask-CORS. "
        "Use jsonify() for all responses — no HTML templates. "
        "App factory in api/app.py, models in api/models/, routes in api/routes/.\n"
        "Frontend: React 18+, Vite, react-router-dom. Functional components "
        "with hooks. CSS variables in index.css — no CSS frameworks. "
        "Use fetch() — not axios.\n"
        "frontend/package.json devDependencies MUST include vite, @vitejs/plugin-react, AND @babel/core. "
        "Do not omit any — the dev server will fail to start without them."
    ),
    workspace_setup="flask_react",
    setup_steps=("python_venv", "npm_frontend"),
    launch_commands=(("backend", "python api/run.py"), ("frontend", "npm run dev -- --host")),
    default_agents=("python_dev", "react_dev"),
)

STACK_PROFILES: dict[str, StackProfile] = {
    "flask_react": _FLASK_REACT,
}

# maps human-readable project types to stack keys — used if the architect detects a type
PROJECT_TYPE_TO_STACK: dict[str, str] = {
    "web_app":       "flask_react",
    "fullstack_web": "flask_react",
    "website":       "flask_react",
    "api_service":   "flask_react",
    "rest_api":      "flask_react",
    "dashboard":     "flask_react",
}


def get_profile(name: str) -> StackProfile:
    """return a stack profile by name, falling back to flask_react if unknown."""
    return STACK_PROFILES.get(name, _FLASK_REACT)


def list_profiles() -> list[str]:
    return list(STACK_PROFILES.keys())


def _heuristic_stack(user_story: str) -> str:
    # only one stack exists — always flask_react
    return "flask_react"


async def auto_detect_stack(user_story: str) -> str:
    """always returns flask_react — the only supported stack."""
    return "flask_react"
