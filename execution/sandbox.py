"""SandboxManager — isolated workspace per generated project (Flask + React).

Handles:
- Creating the workspace directory under workspaces/<project_id>/
- Python venv creation and pip install from requirements.txt
- npm install in frontend/ from package.json
- Vite scaffold files (index.html, main.jsx, vite.config.js)
- Agent memory files (memory/<agent>_log.md, memory/project_state.md)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# root directory for all generated project workspaces.
# WORKSPACE_DIR env var lets fly.io point this at the persistent volume (/data/workspaces).
# falls back to <repo>/workspaces/ for local development.
WORKSPACE_ROOT = os.environ.get(
    "WORKSPACE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspaces"),
)


class SandboxError(Exception):
    pass


class SandboxManager:
    """Manages an isolated sandbox for a single generated project.

    Each sandbox has:
      - A workspace directory under WORKSPACE_ROOT/<project_id>/
      - An isolated Python venv (if the stack uses Python)
      - npm node_modules (if the stack uses Node)
      - A requirements.txt written by the Architect agent
    """

    def __init__(self, project_id: str, stack: str = "flask_react"):
        self.project_id = project_id
        self.stack = stack
        self.workspace_dir = Path(WORKSPACE_ROOT) / project_id
        self.venv_dir = self.workspace_dir / ".venv"
        self._setup_done = False

    # ── Properties ────────────────────────────────────────────────────

    @property
    def python_exe(self) -> str:
        """Path to the venv Python executable."""
        if platform.system() == "Windows":
            return str(self.venv_dir / "Scripts" / "python.exe")
        return str(self.venv_dir / "bin" / "python")

    @property
    def pip_exe(self) -> str:
        """Path to the venv pip executable."""
        if platform.system() == "Windows":
            return str(self.venv_dir / "Scripts" / "pip.exe")
        return str(self.venv_dir / "bin" / "pip")

    @property
    def node_modules_dir(self) -> Path:
        pkg_subdir = _npm_package_dir(self.stack)
        npm_dir = self.workspace_dir / pkg_subdir if pkg_subdir else self.workspace_dir
        return npm_dir / "node_modules"

    # ── Workspace management ───────────────────────────────────────────

    def ensure_workspace(self) -> Path:
        """Create the workspace directory if it doesn't exist."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        return self.workspace_dir

    def workspace_exists(self) -> bool:
        return self.workspace_dir.exists()

    def write_file(self, rel_path: str, content: str) -> Path:
        """Write a file into the workspace. Creates parent directories as needed."""
        abs_path = self.workspace_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        return abs_path

    def read_file(self, rel_path: str) -> str | None:
        """Read a file from the workspace. Returns None if not found or binary."""
        abs_path = self.workspace_dir / rel_path
        if abs_path.is_file():
            try:
                return abs_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                return None
        return None

    def list_files(self, subdir: str = "") -> list[str]:
        """List all files in the workspace (or a subdirectory), as relative paths."""
        base = self.workspace_dir / subdir if subdir else self.workspace_dir
        if not base.exists():
            return []
        result = []
        BINARY_EXTENSIONS = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".gz",
                              ".zip", ".tar", ".png", ".jpg", ".jpeg", ".gif",
                              ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf",
                              ".lock", ".map"}
        for path in base.rglob("*"):
            if path.is_file():
                # Skip venv, node_modules, __pycache__, .git
                parts = path.relative_to(self.workspace_dir).parts
                if any(p in (".venv", "node_modules", "__pycache__", ".git", ".claude") for p in parts):
                    continue
                # Skip binary file types
                if path.suffix.lower() in BINARY_EXTENSIONS:
                    continue
                result.append(str(path.relative_to(self.workspace_dir)))
        return sorted(result)

    def filter_files_for_sprint(self, allowed_paths: list[str]) -> dict[str, str]:
        """Read only the files in allowed_paths from workspace.

        Returns dict of {rel_path: content}. Silently skips missing files.
        """
        files: dict[str, str] = {}
        for rel in allowed_paths:
            content = self.read_file(rel)
            if content is not None:
                files[rel] = content
        return files

    def get_all_project_files(self) -> dict[str, str]:
        """Read all non-hidden project files from workspace. Used for context."""
        files: dict[str, str] = {}
        for rel in self.list_files():
            content = self.read_file(rel)
            if content is not None:
                files[rel] = content
        return files

    # ── Memory helpers ─────────────────────────────────────────────────

    def ensure_memory_dir(self) -> Path:
        """Create memory/ directory in workspace if it doesn't exist."""
        mem_dir = self.workspace_dir / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        return mem_dir

    def read_agent_log(self, agent_name: str) -> str:
        """Read an agent's sprint log. Returns empty string if not yet created."""
        path = self.workspace_dir / "memory" / f"{agent_name}_log.md"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
        return ""

    def append_agent_log(self, agent_name: str, sprint_num: int, summary: str) -> None:
        """Append a sprint summary to an agent's log file."""
        mem_dir = self.ensure_memory_dir()
        path = mem_dir / f"{agent_name}_log.md"
        is_new = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# {agent_name} Sprint Log\n")
            f.write(f"\n## Sprint {sprint_num}\n{summary.strip()}\n")

    def read_project_state(self) -> str:
        """Read the project-wide state registry."""
        path = self.workspace_dir / "memory" / "project_state.md"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
        return ""

    def update_project_state(self, sprint_num: int, agent_name: str, files_written: list[str], summary: str) -> None:
        """Append a state update to project_state.md after an agent completes work."""
        mem_dir = self.ensure_memory_dir()
        path = mem_dir / "project_state.md"
        if not path.exists():
            path.write_text("# Project State\n\nRunning registry of all code artifacts.\n\n", encoding="utf-8")
        entry = (
            f"\n## Sprint {sprint_num} — {agent_name}\n"
            f"**Files written:** {', '.join(files_written) if files_written else 'none'}\n"
            f"**Summary:** {summary.strip()}\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

    # ── Environment setup ──────────────────────────────────────────────

    async def setup(
        self,
        requirements_txt: str | None = None,
        package_json: str | None = None,
        progress_cb=None,
    ) -> None:
        """Set up the full sandbox environment.

        1. Create workspace directory
        2. Write requirements.txt / package.json if provided
        3. Create Python venv and install deps (if stack uses Python)
        4. Run npm install (if stack uses Node)
        """
        self.ensure_workspace()
        self.ensure_memory_dir()
        uses_python = _stack_uses_python(self.stack)
        uses_npm = _stack_uses_npm(self.stack)

        if requirements_txt and uses_python:
            self.write_file("requirements.txt", requirements_txt)

        if package_json and uses_npm:
            # Ensure @babel/core is present — required by @vitejs/plugin-react
            package_json = _inject_babel_core(package_json)
            # Write package.json to the frontend/ subdir for full-stack stacks,
            # or to root for npm-only stacks (phaser, express_react).
            pkg_target = _npm_package_dir(self.stack)
            self.write_file(f"{pkg_target}package.json", package_json)

        if uses_python:
            await self._setup_python_venv(requirements_txt, progress_cb)

        if uses_npm:
            # Write Vite scaffold files before npm install so Vite can serve the app.
            _write_vite_scaffold(self.workspace_dir, self.stack)
            await self._setup_npm(progress_cb)

        self._setup_done = True

    async def _setup_python_venv(self, requirements_txt: str | None, progress_cb=None) -> None:
        """Create venv and install requirements.

        Uses run_in_executor to run blocking subprocess.run calls — this avoids
        the NotImplementedError that asyncio.create_subprocess_exec raises on Windows
        when running under uvicorn's SelectorEventLoop.
        """
        loop = asyncio.get_event_loop()

        if not self.venv_dir.exists():
            await _emit(progress_cb, "sandbox", f"Creating Python venv in {self.venv_dir}...")

            def _create_venv():
                return subprocess.run(
                    [sys.executable, "-m", "venv", str(self.venv_dir)],
                    capture_output=True,
                    text=True,
                    cwd=str(self.workspace_dir),
                    timeout=120,
                )

            result = await loop.run_in_executor(None, _create_venv)
            if result.returncode != 0:
                raise SandboxError(f"venv creation failed: {result.stderr}")
            await _emit(progress_cb, "sandbox", "Venv created.")

        # Upgrade pip silently
        def _upgrade_pip():
            return subprocess.run(
                [self.python_exe, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
                capture_output=True,
                text=True,
                timeout=60,
            )

        await loop.run_in_executor(None, _upgrade_pip)

        # Always pre-install python-dotenv — agents routinely import it without declaring it.
        def _pip_install_dotenv():
            return subprocess.run(
                [self.pip_exe, "install", "python-dotenv", "--quiet"],
                capture_output=True, text=True, cwd=str(self.workspace_dir),
            )
        await loop.run_in_executor(None, _pip_install_dotenv)

        if requirements_txt:
            req_path = self.workspace_dir / "requirements.txt"
            await _emit(progress_cb, "sandbox", "Installing requirements.txt...")

            def _pip_install():
                return subprocess.run(
                    [self.pip_exe, "install", "-r", str(req_path), "--quiet"],
                    capture_output=True,
                    text=True,
                    cwd=str(self.workspace_dir),
                )

            result = await loop.run_in_executor(None, _pip_install)
            if result.returncode != 0:
                await _emit(progress_cb, "sandbox",
                            f"Warning: pip install had issues: {result.stderr[:500]}")
            else:
                await _emit(progress_cb, "sandbox", "Requirements installed.")

    async def _setup_npm(self, progress_cb=None) -> None:
        """Run npm install in the correct directory for this stack."""
        pkg_subdir = _npm_package_dir(self.stack)
        npm_dir = self.workspace_dir / pkg_subdir if pkg_subdir else self.workspace_dir
        pkg_path = npm_dir / "package.json"
        if not pkg_path.exists():
            return  # package.json hasn't been written yet — skip

        if (npm_dir / "node_modules").exists():
            return  # already installed

        await _emit(progress_cb, "sandbox", f"Running npm install in {pkg_subdir or '.'}...")
        npm_cmd = "npm.cmd" if platform.system() == "Windows" else "npm"

        loop = asyncio.get_event_loop()

        def _npm_install():
            return subprocess.run(
                [npm_cmd, "install"],
                capture_output=True,
                text=True,
                cwd=str(npm_dir),
            )

        result = await loop.run_in_executor(None, _npm_install)
        if result.returncode != 0:
            await _emit(progress_cb, "sandbox",
                        f"Warning: npm install had issues: {result.stderr[:500]}")
        else:
            await _emit(progress_cb, "sandbox", "npm packages installed.")

    async def reinstall_deps(self, progress_cb=None) -> None:
        """Re-run pip install / npm install after agents may have changed dep files."""
        req_path = self.workspace_dir / "requirements.txt"
        if req_path.exists() and _stack_uses_python(self.stack):
            await self._setup_python_venv(req_path.read_text(), progress_cb)
        if _stack_uses_npm(self.stack):
            pkg_subdir = _npm_package_dir(self.stack)
            npm_dir = self.workspace_dir / pkg_subdir if pkg_subdir else self.workspace_dir
            pkg_path = npm_dir / "package.json"
            if pkg_path.exists():
                # Force reinstall — agent may have changed package.json
                nm = npm_dir / "node_modules"
                if nm.exists():
                    try:
                        shutil.rmtree(nm)
                    except (PermissionError, OSError) as e:
                        # Windows file lock — skip rmtree, npm install will update in-place
                        log.warning("Could not remove node_modules (file lock): %s — reinstalling in-place", e)
                await self._setup_npm(progress_cb)

    # ── File snapshots ─────────────────────────────────────────────────

    def snapshot(self) -> dict[str, str]:
        """Return all current workspace files as a dict."""
        return self.get_all_project_files()

    def apply_diff(self, new_files: dict[str, str]) -> list[str]:
        """Write a set of files to workspace. Returns list of written paths."""
        written = []
        for rel, content in new_files.items():
            self.write_file(rel, content)
            written.append(rel)
        return written


# ── Helpers ────────────────────────────────────────────────────────────────────

def _npm_package_dir(_stack: str = "flask_react") -> str:
    return "frontend/"


def _write_vite_scaffold(workspace_dir: Path, _stack: str = "flask_react") -> None:
    """Write the minimal Vite scaffold files (index.html, main.jsx) before npm install."""
    fe_dir = workspace_dir / "frontend"
    fe_dir.mkdir(parents=True, exist_ok=True)
    src_dir = fe_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # index.html — Vite entry point
    index_html = fe_dir / "index.html"
    if not index_html.exists():
        index_html.write_text(
            '<!DOCTYPE html>\n'
            '<html lang="en">\n'
            '  <head>\n'
            '    <meta charset="UTF-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
            '    <title>App</title>\n'
            '  </head>\n'
            '  <body>\n'
            '    <div id="root"></div>\n'
            '    <script type="module" src="/src/main.jsx"></script>\n'
            '  </body>\n'
            '</html>\n',
            encoding="utf-8",
        )

    # src/main.jsx — React root mount
    main_jsx = src_dir / "main.jsx"
    if not main_jsx.exists():
        main_jsx.write_text(
            'import React from "react";\n'
            'import ReactDOM from "react-dom/client";\n'
            'import App from "./App";\n\n'
            'ReactDOM.createRoot(document.getElementById("root")).render(\n'
            '  <React.StrictMode>\n'
            '    <App />\n'
            '  </React.StrictMode>\n'
            ');\n',
            encoding="utf-8",
        )

    # src/App.jsx — fallback so Vite doesn't crash if the agent skips it
    app_jsx = src_dir / "App.jsx"
    if not app_jsx.exists():
        app_jsx.write_text(
            'export default function App() {\n'
            '  return <div style={{padding:32,fontFamily:"sans-serif"}}>Loading...</div>;\n'
            '}\n',
            encoding="utf-8",
        )

    # vite.config.js — proxy /api to backend + src/ aliases so bare imports work
    vite_config = fe_dir / "vite.config.js"
    if not vite_config.exists():
        vite_config.write_text(
            'import { defineConfig } from "vite";\n'
            'import react from "@vitejs/plugin-react";\n'
            'import path from "path";\n\n'
            'export default defineConfig({\n'
            '  plugins: [react()],\n'
            '  resolve: {\n'
            '    alias: {\n'
            '      // Allow bare and src/-prefixed imports to resolve correctly\n'
            '      src:        path.resolve(__dirname, "src"),\n'
            '      components: path.resolve(__dirname, "src/components"),\n'
            '      pages:      path.resolve(__dirname, "src/pages"),\n'
            '      views:      path.resolve(__dirname, "src/views"),\n'
            '      styles:     path.resolve(__dirname, "src/styles"),\n'
            '      hooks:      path.resolve(__dirname, "src/hooks"),\n'
            '      utils:      path.resolve(__dirname, "src/utils"),\n'
            '      api:        path.resolve(__dirname, "src/api"),\n'
            '      store:      path.resolve(__dirname, "src/store"),\n'
            '      context:    path.resolve(__dirname, "src/context"),\n'
            '    },\n'
            '  },\n'
            '  server: {\n'
            '    port: 9001,\n'
            '    strictPort: true,\n'
            '    proxy: {\n'
            '      "/api": "http://localhost:9000",\n'
            '    },\n'
            '  },\n'
            '});\n',
            encoding="utf-8",
        )


def _stack_uses_python(_stack: str = "flask_react") -> bool:
    return True


def _stack_uses_npm(_stack: str = "flask_react") -> bool:
    return True


def _inject_babel_core(package_json: str) -> str:
    """Ensure common React packages are present — agents frequently import these without
    adding them to package.json, causing Vite to fail at import-analysis time."""
    import json as _json

    # Packages agents commonly import but forget to declare
    REQUIRED_DEPS = {
        "prop-types":        "^15.8.1",
        "styled-components":  "^6.1.0",
        "react-router-dom":  "^6.26.0",
    }
    REQUIRED_DEV_DEPS = {
        "@babel/core": "^7.0.0",
    }

    try:
        pkg = _json.loads(package_json)
        deps = pkg.setdefault("dependencies", {})
        dev = pkg.setdefault("devDependencies", {})
        for name, version in REQUIRED_DEPS.items():
            if name not in deps and name not in dev:
                deps[name] = version
        for name, version in REQUIRED_DEV_DEPS.items():
            if name not in dev and name not in deps:
                dev[name] = version
        return _json.dumps(pkg, indent=2)
    except Exception:
        return package_json


async def _emit(cb, stage: str, message: str) -> None:
    if cb is not None:
        try:
            await cb(stage, message)
        except Exception:
            pass
