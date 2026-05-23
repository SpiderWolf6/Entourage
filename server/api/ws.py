"""WebSocket endpoint for real-time pipeline event streaming."""

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import traceback
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.services.event_bus import event_bus

router = APIRouter()
log = logging.getLogger(__name__)

WORKSPACES_DIR = Path(os.environ.get("WORKSPACE_DIR", str(Path(__file__).resolve().parent.parent.parent / "workspaces")))


@router.websocket("/ws/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    """Stream pipeline events for a project in real time.

    Subscribes to the in-process EventBus and fans events out to the client.
    Sends a heartbeat every 30s to keep the connection alive through proxies.
    """
    await websocket.accept()
    log.info("WS connected  project=%s  client=%s", project_id, websocket.client)

    queue = event_bus.subscribe(project_id)

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Heartbeat keeps the connection alive through load balancers/proxies
                await websocket.send_json({"type": "heartbeat"})
                continue

            try:
                await websocket.send_json({
                    "type": event.type,
                    "data": event.data,
                })
            except (WebSocketDisconnect, RuntimeError):
                break
            except Exception as e:
                log.warning("WS send error [%s] project=%s: %s", event.type, project_id, e)
    except WebSocketDisconnect as e:
        log.info("WS disconnected  project=%s  code=%s", project_id, e.code)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("WS unexpected error  project=%s: %s", project_id, e, exc_info=True)
    finally:
        event_bus.unsubscribe(project_id, queue)


# ── Claude Consultant ─────────────────────────────────────────────────────────

def _find_claude_binary() -> str | None:
    candidates = ["claude.cmd", "claude.exe", "claude"] if platform.system() == "Windows" else ["claude"]
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    try:
        result = subprocess.run(["npm", "prefix", "-g"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            prefix = result.stdout.strip()
            candidate = Path(prefix) / ("claude.cmd" if platform.system() == "Windows" else "bin/claude")
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass
    return None


@router.websocket("/ws/{project_id}/claude-consultant")
async def claude_consultant_ws(websocket: WebSocket, project_id: str):
    """Interactive Claude Code session running inside the project workspace."""
    await websocket.accept()
    print(f"[CONSULTANT] CONNECTED  project={project_id}", flush=True)

    workspace = WORKSPACES_DIR / project_id
    if not workspace.exists():
        await websocket.send_json({"type": "error", "text": f"Workspace not found: {project_id}"})
        await websocket.close()
        return

    claude_bin = _find_claude_binary()
    if not claude_bin:
        await websocket.send_json({"type": "error", "text": "claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"})
        await websocket.close()
        return

    env = os.environ.copy()
    env["FORCE_COLOR"] = "0"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["CLAUDE_DISABLE_AUTOUPDATE"] = "1"

    await websocket.send_json({"type": "ready", "text": f"Claude Consultant ready.\nWorkspace: {workspace}\nType your request and press Enter.\n"})

    loop = asyncio.get_event_loop()

    # Each message from the frontend is a standalone prompt run via -p mode.
    # We re-launch claude for each message to keep it stateless-simple on Windows
    # (no PTY needed) while still giving the user a conversational feel.
    conversation: list[dict] = []

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=300.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
                continue

            user_msg = raw.strip()
            if not user_msg:
                continue

            conversation.append({"role": "user", "content": user_msg})

            # Build prompt including conversation history for context
            history_text = ""
            for turn in conversation[:-1]:
                prefix = "User" if turn["role"] == "user" else "Claude"
                history_text += f"{prefix}: {turn['content']}\n\n"

            prompt = (
                "You are a helpful coding assistant embedded in the Entourage platform. "
                "The user has just built a web app and you are running inside its workspace directory. "
                "Help them fix bugs, add features, or understand the codebase. "
                "Use your file tools freely — Read, Write, Edit, Bash.\n\n"
                + (f"Conversation so far:\n{history_text}" if history_text else "")
                + f"User: {user_msg}"
            )

            await websocket.send_json({"type": "thinking", "text": ""})

            run_cmd = [
                claude_bin,
                "-p",
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "--verbose",
                "--add-dir", str(workspace),
                "--no-session-persistence",
            ]

            def _run_blocking() -> tuple[int, str, str]:
                result = subprocess.run(
                    run_cmd,
                    input=prompt.encode("utf-8"),
                    capture_output=True,
                    cwd=str(workspace),
                    env=env,
                    timeout=300,
                )
                return result.returncode, result.stdout.decode("utf-8", errors="replace"), result.stderr.decode("utf-8", errors="replace")

            try:
                exit_code, stdout, stderr = await loop.run_in_executor(None, _run_blocking)
            except Exception as e:
                msg = "Request timed out after 5 minutes." if "timeout" in type(e).__name__.lower() or "TimeoutExpired" in type(e).__name__ else f"Error running Claude: {e}"
                await websocket.send_json({"type": "error", "text": msg})
                continue

            # Parse stream-json output and collect assistant reply
            assistant_reply_parts: list[str] = []
            tool_calls: list[str] = []

            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    etype = ev.get("type", "")
                    if etype == "assistant":
                        for block in ev.get("message", {}).get("content", []):
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    text = block.get("text", "").strip()
                                    if text:
                                        assistant_reply_parts.append(text)
                                        await websocket.send_json({"type": "stream", "text": text})
                                elif block.get("type") == "tool_use":
                                    tool = block.get("name", "")
                                    inp = block.get("input", {})
                                    path = inp.get("file_path") or inp.get("path") or inp.get("command", "")
                                    tool_line = f"[{tool}] {str(path)[:120]}"
                                    tool_calls.append(tool_line)
                                    await websocket.send_json({"type": "tool", "text": tool_line})
                    elif etype == "result":
                        cost = ev.get("total_cost_usd", 0)
                        await websocket.send_json({"type": "done", "cost": cost})
                except Exception:
                    pass

            if exit_code != 0 and not assistant_reply_parts:
                err_text = stderr.strip()[:600] if stderr.strip() else f"Claude exited with code {exit_code}"
                await websocket.send_json({"type": "error", "text": err_text})

            full_reply = "\n\n".join(assistant_reply_parts)
            if tool_calls:
                full_reply += "\n\n" + "\n".join(tool_calls)
            if full_reply:
                conversation.append({"role": "assistant", "content": full_reply})

    except WebSocketDisconnect:
        print(f"[CONSULTANT] DISCONNECTED  project={project_id}", flush=True)
    except Exception as e:
        print(f"[CONSULTANT] ERROR  project={project_id}: {e}", flush=True)
        traceback.print_exc()
    finally:
        print(f"[CONSULTANT] CLOSED  project={project_id}", flush=True)
