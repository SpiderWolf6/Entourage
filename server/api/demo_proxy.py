"""Demo proxy — forwards /demo/<project_id>/<path> to the running Vite/Flask demo processes.

When the generated app is running inside the Fly.io machine on dynamically-assigned ports,
this proxy makes it publicly accessible via the entourage server's domain without
needing to expose extra ports.

Routing:
  /demo/<project_id>/          → frontend (Vite, dynamic port)
  /demo/<project_id>/api/*     → backend (Flask, dynamic port)

Ports are read dynamically from the DemoLauncher registry — no hardcoded port numbers.
"""

import httpx
import logging
from fastapi import APIRouter, Request, Response, HTTPException

from execution.demo_launcher import get_demo

log = logging.getLogger(__name__)

router = APIRouter(tags=["demo-proxy"])


def _get_ports(project_id: str) -> tuple[int, int]:
    """Return (backend_port, frontend_port) for a running demo."""
    launcher = get_demo(project_id)
    if not launcher:
        raise HTTPException(
            status_code=404,
            detail=f"No running demo for project {project_id}. Launch it first.",
        )
    backend_port  = next((s.port for s in launcher._services if s.name in ("backend", "api", "app")), None)
    frontend_port = next((s.port for s in launcher._services if s.name == "frontend"), None)

    if not backend_port and not frontend_port:
        raise HTTPException(status_code=503, detail="Demo services not yet started")

    # fall back to whatever port exists
    backend_port  = backend_port  or frontend_port
    frontend_port = frontend_port or backend_port
    return backend_port, frontend_port


async def _proxy(request: Request, target_url: str, project_id: str = "") -> Response:
    """Forward a request to target_url and return the response.

    For HTML responses, rewrites absolute paths so Vite asset requests
    stay within the /demo/<project_id>/ proxy prefix rather than hitting
    the entourage SPA catch-all.
    """
    body = await request.body() if request.method not in ("GET", "HEAD") else None

    headers = dict(request.headers)
    for h in ("host", "connection", "transfer-encoding", "te",
              "trailers", "upgrade", "keep-alive"):
        headers.pop(h, None)

    # Strip conditional-request headers for HTML so Vite never returns 304.
    # A 304 has no body — our fetch-patch injection would be skipped on cached loads.
    if project_id and request.method == "GET":
        accept = headers.get("accept", "")
        if "text/html" in accept:
            for h in ("if-none-match", "if-modified-since"):
                headers.pop(h, None)

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                params=request.query_params,
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Demo service not reachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Demo service timed out")

    resp_headers = dict(resp.headers)
    for h in ("transfer-encoding", "connection", "keep-alive", "content-length", "content-encoding"):
        resp_headers.pop(h, None)

    # For HTML responses, strip caching headers so the browser always gets a fresh
    # copy with our injected fetch-patch script rather than serving a cached version.
    content_type_check = resp.headers.get("content-type", "")
    if project_id and "text/html" in content_type_check:
        for h in ("etag", "last-modified", "cache-control"):
            resp_headers.pop(h, None)
        resp_headers["cache-control"] = "no-store"

    # touch demo activity on every proxied request
    launcher = get_demo(project_id)
    if launcher:
        launcher.touch()

    content_type = resp.headers.get("content-type", "")
    content = resp.content

    # Vite's base is patched to /demo/<project_id>/ before launch so asset URLs
    # are already prefixed. We also inject a fetch/XHR patch so that bare /api/
    # calls from the generated app are rewritten to /demo/<project_id>/api/
    # (otherwise they hit the entourage server and get a 405/404).
    if project_id and "text/html" in content_type:
        base_prefix = f"/demo/{project_id}"
        html_text = content.decode("utf-8", errors="replace")

        base_tag = "" if "<base " in html_text else f'<base href="{base_prefix}/">'

        # Patch fetch + XMLHttpRequest so /api/* resolves through the proxy prefix.
        # Injected as the first thing in <head> so it runs before any app code.
        patch_script = (
            f'<script>'
            f'(function(){{'
            f'  var _p="{base_prefix}";'
            f'  var _f=window.fetch;'
            f'  window.fetch=function(u,o){{'
            f'    if(typeof u==="string"&&u.startsWith("/api/"))u=_p+u;'
            f'    return _f.call(this,u,o);'
            f'  }};'
            f'  var _x=XMLHttpRequest.prototype.open;'
            f'  XMLHttpRequest.prototype.open=function(m,u){{'
            f'    if(typeof u==="string"&&u.startsWith("/api/"))u=_p+u;'
            f'    return _x.apply(this,[m,u].concat([].slice.call(arguments,2)));'
            f'  }};'
            f'}})();'
            f'</script>'
        )
        html_text = html_text.replace("<head>", f"<head>{base_tag}{patch_script}", 1)
        content = html_text.encode("utf-8")

    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


@router.api_route(
    "/demo/{project_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def demo_proxy(project_id: str, path: str, request: Request) -> Response:
    """Proxy requests to the running demo for a project."""
    backend_port, frontend_port = _get_ports(project_id)

    if path.startswith("api/") or path == "api":
        target = f"http://localhost:{backend_port}/{path}"
    else:
        target = f"http://localhost:{frontend_port}/{path}"

    log.debug("proxy %s /demo/%s/%s → %s", request.method, project_id, path, target)
    return await _proxy(request, target, project_id=project_id)


@router.api_route(
    "/demo/{project_id}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def demo_proxy_root(project_id: str, request: Request) -> Response:
    """Proxy root demo URL to the frontend."""
    _, frontend_port = _get_ports(project_id)
    target = f"http://localhost:{frontend_port}/"
    return await _proxy(request, target, project_id=project_id)
