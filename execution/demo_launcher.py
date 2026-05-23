"""DemoLauncher — starts the generated Flask+React project for live preview.

Responsibilities:
- Run launch commands from StackProfile (backend on 9000, frontend on 9001).
- Kill existing processes on those ports before starting fresh.
- Monitor process health.
- Expose preview URLs: {"backend": "http://localhost:9000", "frontend": "http://localhost:9001"}
- Gracefully shut down all processes on stop().
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Max seconds to wait for a service to become available on its port
SERVICE_READY_TIMEOUT = 60

# Demo idle timeout — kill demo after 30 minutes of no activity
DEMO_IDLE_TIMEOUT_SECS = 30 * 60


def _get_free_port() -> int:
    """Ask the OS for a free TCP port by binding to port 0."""
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@dataclass
class ServiceInfo:
    name: str              # "backend" | "frontend" | "app"
    command: str           # raw launch command string
    port: int
    proc: subprocess.Popen | None = None
    url: str = ""
    ready: bool = False


@dataclass
class DemoStatus:
    project_id: str
    status: str            # "starting" | "running" | "stopped" | "failed"
    urls: dict[str, str] = field(default_factory=dict)    # service → URL
    primary_url: str = ""  # The main URL users should open
    services: list[dict] = field(default_factory=list)
    error: str = ""


class DemoLauncher:
    """Manages the lifecycle of a running project demo.

    Usage:
        launcher = DemoLauncher(workspace_dir, stack, project_id)
        status = await launcher.start()
        # ... demo is running ...
        await launcher.stop()
    """

    def __init__(
        self,
        workspace_dir: Path,
        stack: str,
        project_id: str,
        venv_dir: Path | None = None,
    ):
        self.workspace_dir = workspace_dir
        self.stack = stack
        self.project_id = project_id
        self.venv_dir = venv_dir or (workspace_dir / ".venv")
        self._services: list[ServiceInfo] = []
        self._running = False
        self._monitor_task: asyncio.Task | None = None
        self._last_activity = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0.0

    def touch(self) -> None:
        """Record activity — resets idle timer."""
        try:
            self._last_activity = asyncio.get_event_loop().time()
        except Exception:
            pass

    def idle_seconds(self) -> float:
        """Return seconds since last activity."""
        try:
            return asyncio.get_event_loop().time() - self._last_activity
        except Exception:
            return 0.0

    # ── Public API ─────────────────────────────────────────────────────

    async def start(self, on_event=None) -> DemoStatus:
        """Start all services for the project. Returns DemoStatus."""
        if self._running:
            return self._build_status("running")

        launch_commands = self._get_launch_commands()
        if not launch_commands:
            return DemoStatus(
                project_id=self.project_id,
                status="failed",
                error=f"No launch commands defined for stack '{self.stack}'",
            )

        await _emit(on_event, "demo_starting", {
            "stack": self.stack,
            "services": [name for name, _ in launch_commands],
        })

        # assign OS-free ports for each service
        self._services = []
        for name, cmd in launch_commands:
            port = _get_free_port()
            service = ServiceInfo(
                name=name,
                command=cmd,
                port=port,
                url=f"http://localhost:{port}",
            )
            self._services.append(service)

        self.touch()

        # Patch vite.config.js with the correct base path before starting Vite.
        # Vite bakes the base into all asset URLs — without this every JS module
        # request goes to /src/... which hits the entourage SPA catch-all instead
        # of being routed through /demo/<project_id>/.
        _patch_vite_base(self.workspace_dir, self.project_id)
        _patch_package_json(self.workspace_dir)

        # Start each service
        for service in self._services:
            try:
                await self._start_service(service, on_event)
            except Exception as e:
                import traceback
                log.error("Failed to start service %s: %s\n%s", service.name, e, traceback.format_exc())
                await _emit(on_event, "demo_error", {
                    "service": service.name,
                    "error": f"{type(e).__name__}: {e}",
                })

        # Wait for services to become ready
        ready_tasks = [self._wait_for_ready(s, on_event) for s in self._services]
        await asyncio.gather(*ready_tasks, return_exceptions=True)

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_services(on_event))

        status = self._build_status("running")
        await _emit(on_event, "demo_ready", {
            "urls": status.urls,
            "primary_url": status.primary_url,
        })
        return status

    async def stop(self) -> None:
        """Stop all running services."""
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        for service in self._services:
            await self._stop_service(service)

        self._services = []

    def get_status(self) -> DemoStatus:
        """Get current demo status without starting/stopping."""
        if not self._services:
            return DemoStatus(project_id=self.project_id, status="stopped")
        return self._build_status("running" if self._running else "stopped")

    # ── Service management ─────────────────────────────────────────────

    async def _start_service(self, service: ServiceInfo, on_event=None) -> None:
        """Launch a single service subprocess.

        Uses subprocess.Popen (thread-based) instead of asyncio.create_subprocess_exec
        because uvicorn may have replaced the ProactorEventLoop with SelectorEventLoop
        on Windows, which causes NotImplementedError from asyncio subprocess APIs.

        The port is injected into the command so each demo gets an OS-assigned free port.
        """
        # inject the OS-assigned port into the command
        cmd_with_port = _inject_port(service.command, service.port)
        cmd_parts = _split_command(cmd_with_port)
        resolved_cmd = _resolve_executable(cmd_parts, self.venv_dir)

        # Determine the correct working directory for this service.
        # Frontend services for full-stack stacks run from workspace/frontend/.
        cwd = _service_cwd(self.workspace_dir, service.name, self.stack)

        log.info("Starting %s: %s (port %d) in %s", service.name, " ".join(resolved_cmd), service.port, cwd)
        print(f"\n[DEMO] Starting {service.name}: {' '.join(resolved_cmd)}", flush=True)
        print(f"[DEMO]   cwd={cwd}  port={service.port}  cwd_exists={cwd.exists()}", flush=True)

        # find backend port to pass to frontend so Vite proxy knows where Flask is
        backend_port = next((s.port for s in self._services if s.name in ("backend", "api", "app")), None)
        env = _build_demo_env(self.workspace_dir, self.venv_dir, service.port, getattr(self, 'creds', None), backend_port=backend_port)

        loop = asyncio.get_event_loop()
        # Run Popen in a thread so we don't block the event loop, but avoid
        # asyncio subprocess APIs which require ProactorEventLoop on Windows.
        def _spawn():
            return subprocess.Popen(
                resolved_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                env=env,
            )
        proc = await loop.run_in_executor(None, _spawn)
        service.proc = proc

        await _emit(on_event, "service_started", {
            "service": service.name,
            "port": service.port,
            "pid": proc.pid,
            "command": service.command,
        })

        # Start log streaming task (non-blocking)
        asyncio.create_task(
            self._stream_service_logs(service, on_event),
            name=f"logs_{service.name}",
        )

    async def _stop_service(self, service: ServiceInfo) -> None:
        """Gracefully stop a service process."""
        if service.proc is None:
            return
        proc = service.proc
        loop = asyncio.get_event_loop()
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(loop.run_in_executor(None, proc.wait), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass  # Already dead
        service.proc = None
        service.ready = False

    async def _wait_for_ready(self, service: ServiceInfo, on_event=None) -> None:
        """Poll until the service port is accepting connections."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + SERVICE_READY_TIMEOUT

        while loop.time() < deadline:
            # If the process already died, bail out early rather than waiting 60s
            if service.proc and service.proc.poll() is not None:
                exit_code = service.proc.poll()
                crash_output = ""
                if service.proc.stdout:
                    try:
                        raw = await loop.run_in_executor(None, lambda: service.proc.stdout.read(4096))
                        crash_output = raw.decode("utf-8", errors="replace").strip()
                    except Exception:
                        pass
                log.error(
                    "Service %s exited early with code %d — port %d never opened\nOutput: %s",
                    service.name, exit_code, service.port, crash_output or "(none)",
                )
                await _emit(on_event, "service_crashed", {
                    "service": service.name,
                    "exit_code": exit_code,
                    "message": f"{service.name} crashed before port {service.port} opened",
                    "output": crash_output[:500] if crash_output else "",
                })
                return

            if _port_open("localhost", service.port):
                service.ready = True
                await _emit(on_event, "service_ready", {
                    "service": service.name,
                    "url": service.url,
                })
                return
            await asyncio.sleep(1.0)

        await _emit(on_event, "service_timeout", {
            "service": service.name,
            "port": service.port,
            "message": f"Service did not start within {SERVICE_READY_TIMEOUT}s",
        })

    async def _stream_service_logs(self, service: ServiceInfo, on_event=None) -> None:
        """Stream stdout logs from a service to EventBus.

        Reads from subprocess.Popen stdout in a thread executor to avoid blocking
        the event loop (Popen.stdout is a blocking file object, not an async stream).
        """
        if service.proc is None or service.proc.stdout is None:
            return
        loop = asyncio.get_event_loop()
        try:
            while True:
                raw = await loop.run_in_executor(None, service.proc.stdout.readline)
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    print(f"[DEMO:{service.name}] {text}", flush=True)
                    await _emit(on_event, "service_log", {
                        "service": service.name,
                        "line": text,
                    })
        except Exception:
            pass

    async def _monitor_services(self, on_event=None) -> None:
        """Periodically check that services are still alive."""
        while self._running:
            await asyncio.sleep(10.0)
            for service in self._services:
                if service.proc and service.proc.poll() is not None:
                    # Process died — log it
                    await _emit(on_event, "service_crashed", {
                        "service": service.name,
                        "exit_code": service.proc.poll(),
                    })
                    service.ready = False

    # ── Status builder ─────────────────────────────────────────────────

    def _build_status(self, status: str) -> DemoStatus:
        # Only include URLs for services that actually became ready
        urls = {s.name: s.url for s in self._services if s.ready}
        if not urls:
            # Fall back to all URLs if none are ready (e.g. status check before ready)
            urls = {s.name: s.url for s in self._services}
        # Prefer "frontend" URL as primary; fall back to "app", then "backend"
        primary = (
            urls.get("frontend") or
            urls.get("app") or
            urls.get("backend") or
            (list(urls.values())[0] if urls else "")
        )
        return DemoStatus(
            project_id=self.project_id,
            status=status,
            urls=urls,
            primary_url=primary,
            services=[
                {
                    "name": s.name,
                    "url": s.url,
                    "port": s.port,
                    "ready": s.ready,
                    "command": s.command,
                }
                for s in self._services
            ],
        )

    # ── Stack config ───────────────────────────────────────────────────

    def _get_launch_commands(self) -> list[tuple[str, str]]:
        """Get (name, command) pairs for this stack."""
        try:
            from agents.stacks.profiles import get_profile, STACK_PROFILES
            if self.stack in STACK_PROFILES:
                profile = get_profile(self.stack)
                return list(profile.launch_commands)
        except Exception:
            pass

        # Generic fallbacks
        return _generic_launch_commands(self.workspace_dir)


# ── Registry of active demos ───────────────────────────────────────────────────

_active_demos: dict[str, DemoLauncher] = {}


def register_demo(project_id: str, launcher: DemoLauncher) -> None:
    _active_demos[project_id] = launcher


def get_demo(project_id: str) -> DemoLauncher | None:
    return _active_demos.get(project_id)


def unregister_demo(project_id: str) -> None:
    _active_demos.pop(project_id, None)


# ── Helper functions ───────────────────────────────────────────────────────────

def _kill_port(port: int) -> None:
    """Kill whatever process is listening on the given port (IPv4 and IPv6)."""
    import re
    import subprocess as sp
    if platform.system() == "Windows":
        try:
            result = sp.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10)
            killed = set()
            for line in result.stdout.splitlines():
                if "LISTENING" not in line:
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                # Match both IPv4 (0.0.0.0:9001) and IPv6 ([::1]:9001 or [::]:9001)
                m = re.search(r":(\d+)$", parts[1])
                if not m or int(m.group(1)) != port:
                    continue
                try:
                    pid = int(parts[-1])
                    if pid > 0 and pid not in killed:
                        sp.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
                        killed.add(pid)
                        log.info("Killed PID %d (was on port %d)", pid, port)
                except (ValueError, Exception):
                    pass
        except Exception as e:
            log.warning("_kill_port(%d) failed: %s", port, e)
    else:
        try:
            result = sp.run(["lsof", "-ti", f":{port}"],
                            capture_output=True, text=True, timeout=10)
            for pid_str in result.stdout.split():
                try:
                    import signal as sig
                    os.kill(int(pid_str), sig.SIGKILL)
                    log.info("Killed PID %s (was on port %d)", pid_str, port)
                except Exception:
                    pass
        except Exception as e:
            log.warning("_kill_port(%d) failed: %s", port, e)


async def _wait_ports_free(ports: list[int], timeout: float = 10.0) -> None:
    """Wait until all given ports stop accepting connections (i.e. processes have released them)."""
    deadline = asyncio.get_event_loop().time() + timeout
    remaining = list(ports)
    while remaining and asyncio.get_event_loop().time() < deadline:
        still_busy = [p for p in remaining if _port_open("localhost", p)]
        if not still_busy:
            return
        remaining = still_busy
        await asyncio.sleep(0.5)
    if remaining:
        log.warning("Ports still busy after %.1fs: %s — proceeding anyway", timeout, remaining)


def _port_open(host: str, port: int) -> bool:
    """Return True if a TCP connection can be established."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _inject_port(command: str, port: int) -> str:
    """Replace or inject port into a launch command.

    - Strips any existing --port N then appends the OS-assigned port.
    - For Python/Flask: port is injected via PORT env var in _build_demo_env,
      no flag needed in the command itself.
    """
    import re
    # strip any existing --port flags to avoid duplicates (e.g. vite --port 9001)
    command = re.sub(r'--port\s+\d+', '', command).strip()
    # for vite/npm dev, append --port
    if "npm run dev" in command or "npm run start" in command or "vite" in command:
        return f"{command} --port {port}"
    # for python flask, port comes from PORT env var — no flag needed
    return command


def _split_command(command: str) -> list[str]:
    """Split a command string into parts, respecting quoted strings."""
    import shlex
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _resolve_executable(cmd_parts: list[str], venv_dir: Path) -> list[str]:
    """Replace 'python' with venv python, resolve npm/npx etc."""
    if not cmd_parts:
        return cmd_parts

    exe = cmd_parts[0]
    result = list(cmd_parts)

    if exe in ("python", "python3"):
        if platform.system() == "Windows":
            venv_python = venv_dir / "Scripts" / "python.exe"
        else:
            venv_python = venv_dir / "bin" / "python"
        if venv_python.exists():
            result[0] = str(venv_python)

    elif exe in ("npm", "npx"):
        if platform.system() == "Windows":
            result[0] = exe + ".cmd"

    return result


def _build_demo_env(workspace_dir: Path, venv_dir: Path, port: int, creds=None, backend_port: int | None = None) -> dict[str, str]:
    """Build environment variables for a demo service."""
    env = os.environ.copy()

    # Add venv to PATH
    if platform.system() == "Windows":
        venv_bin = str(venv_dir / "Scripts")
    else:
        venv_bin = str(venv_dir / "bin")
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")

    env["FLASK_DEBUG"] = "0"
    env["FLASK_ENV"] = "production"
    env["PORT"] = str(port)
    env["FLASK_RUN_PORT"] = str(port)
    # pass backend port so Vite proxy config can forward /api correctly
    if backend_port:
        env["BACKEND_PORT"] = str(backend_port)
    # Always include workspace root so `api`, `app`, etc. resolve as packages
    env["PYTHONPATH"] = str(workspace_dir) + os.pathsep + env.get("PYTHONPATH", "")

    return env


def _generic_launch_commands(workspace_dir: Path) -> list[tuple[str, str]]:
    """Auto-detect launch commands from workspace files."""
    commands: list[tuple[str, str]] = []

    # Python: look for common entry points
    for entry in ["api/run.py", "app.py", "main.py", "run.py", "serve.py"]:
        if (workspace_dir / entry).exists():
            commands.append(("backend", f"python {entry}"))
            break

    # Node: look for package.json
    pkg = workspace_dir / "package.json"
    if pkg.exists():
        try:
            import json
            npm = "npm.cmd" if platform.system() == "Windows" else "npm"
            data = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "dev" in scripts:
                commands.append(("frontend", f"{npm} run dev -- --host"))
            elif "start" in scripts:
                commands.append(("frontend", f"{npm} start"))
        except Exception:
            commands.append(("frontend", "npm run dev"))

    return commands


def _service_cwd(workspace_dir: Path, service_name: str, stack: str) -> Path:
    """Return the working directory for a service.

    Frontend services run from workspace/frontend/ where package.json lives.
    All other services run from the workspace root.
    """
    if service_name == "frontend":
        fe_dir = workspace_dir / "frontend"
        if fe_dir.exists():
            return fe_dir
    return workspace_dir


def _patch_package_json(workspace_dir: Path) -> None:
    """Strip hardcoded --port flags from the npm dev script.

    Agents often write `vite --port 9001` in package.json. When _inject_port
    appends the OS-assigned port, Vite ends up with two --port flags. Vite uses
    the last one so it works, but the log is noisy. Strip it here so there's
    only ever one --port (the OS-assigned one added by _inject_port).
    """
    pkg_path = workspace_dir / "frontend" / "package.json"
    if not pkg_path.exists():
        return
    try:
        import json as _json, re
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
        scripts = pkg.get("scripts", {})
        changed = False
        for key in ("dev", "start"):
            if key in scripts:
                cleaned = re.sub(r'--port\s+\d+', '', scripts[key]).strip()
                if cleaned != scripts[key]:
                    scripts[key] = cleaned
                    changed = True
        if changed:
            pkg_path.write_text(_json.dumps(pkg, indent=2), encoding="utf-8")
            log.info("Stripped hardcoded --port from package.json dev script")
    except Exception as e:
        log.warning("Failed to patch package.json: %s", e)


def _patch_vite_base(workspace_dir: Path, project_id: str) -> None:
    """Rewrite vite.config.js so Vite prefixes all asset URLs with /demo/<project_id>/.

    Without this the browser requests /src/App.jsx which hits the entourage SPA
    catch-all instead of being routed through the demo proxy. Also strips any
    hardcoded port/strictPort so OS-assigned ports work, and makes the /api proxy
    dynamic via BACKEND_PORT env var.
    """
    vite_config = workspace_dir / "frontend" / "vite.config.js"
    if not vite_config.exists():
        return
    try:
        import re
        text = vite_config.read_text(encoding="utf-8")

        # Remove stale hardcoded port/strictPort lines
        text = re.sub(r'[ \t]*port\s*:\s*\d+\s*,?\n', '', text)
        text = re.sub(r'[ \t]*strictPort\s*:\s*(true|false)\s*,?\n', '', text)

        # Replace hardcoded /api proxy target with dynamic BACKEND_PORT
        text = re.sub(
            r"""(['"/])/api\1\s*:\s*['"]http://localhost:\d+['"]""",
            r"'/api': `http://localhost:${process.env.BACKEND_PORT || 9000}`",
            text,
        )

        # Remove any existing base: line to avoid duplicates
        text = re.sub(r'[ \t]*base\s*:\s*["\'][^"\']*["\']\s*,?\n', '', text)

        # Inject base after 'export default defineConfig({'
        base_line = f'  base: "/demo/{project_id}/",\n'
        text = re.sub(
            r'(export\s+default\s+defineConfig\s*\(\s*\{)',
            r'\1\n' + base_line.rstrip('\n'),
            text,
        )

        vite_config.write_text(text, encoding="utf-8")
        # Make read-only so Vite's HMR watcher doesn't detect a change on first
        # start and restart (which would lose our injected --port flag).
        try:
            vite_config.chmod(0o444)
        except Exception:
            pass
        log.info("Patched vite.config.js: base=/demo/%s/ + dynamic ports", project_id)
    except Exception as e:
        log.warning("Failed to patch vite.config.js: %s", e)


async def _emit(cb, event_type: str, data: dict) -> None:
    if cb is not None:
        try:
            await cb(event_type, data)
        except Exception:
            pass
