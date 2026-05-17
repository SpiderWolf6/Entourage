# entourage dockerfile
#
# single-stage build — keeps things simple for a project that needs both python and node
# at runtime (node is required to run the generated vite dev server during demos).
#
# build steps:
#   1. install python deps from requirements.txt
#   2. install node + npm (for the entourage frontend build AND demo vite servers)
#   3. install claude code cli globally (used by dev agent subprocesses)
#   4. build the react frontend into frontend/dist/
#      fastapi serves dist/ as static files so no separate node process is needed
#
# runtime:
#   - single uvicorn process on port 8000 (--workers 1 is required — see notes in README)
#   - sqlite db + generated workspaces stored on a persistent fly volume at /data

FROM python:3.12-slim

# system deps: curl for healthcheck, nodejs/npm for frontend build + demo vite servers
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# install python dependencies first (layer cached separately from source code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# install claude code cli — dev agents spawn `claude` as a subprocess
# using --unsafe-perm because we're root inside docker
RUN npm install -g @anthropic-ai/claude-code --unsafe-perm

# copy the rest of the source
COPY . .

# build the react frontend so fastapi can serve it as static files.
# this runs inside the container so the build is reproducible regardless of the
# developer's local node version.
RUN cd frontend && npm install && npm run build

# create a non-root user — claude code refuses --dangerously-skip-permissions as root
RUN useradd -m -u 1000 appuser \
    && mkdir -p /data/workspaces \
    && chown -R appuser:appuser /app /data

USER appuser

# tell the app to store its database and workspaces on the persistent volume.
# these env vars are read by server/config.py and execution/sandbox.py.
ENV DATABASE_URL=sqlite+aiosqlite:////data/entourage.db
ENV WORKSPACE_DIR=/data/workspaces

# single worker is required — the event bus and demo registry are in-process singletons.
# multiple workers would have separate instances and events would be lost across workers.
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
