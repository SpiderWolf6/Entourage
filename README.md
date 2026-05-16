# entourage

an ai war room that builds web apps from a single sentence.

describe what you want to build. five azure openai planning agents debate the architecture, split the work into sprints, and hire a team of claude code developers who write and test the app in parallel. when they're done, the app launches live in a preview window.

---

## what it does

### planning phase

five agents run sequentially. each one hands its output to the next:

| agent | role | model |
|-------|------|-------|
| **engineering manager** | asks 0–3 clarifying questions to lock down scope, then produces a structured project brief | gpt-4.1-mini |
| **product owner** | expands the brief into a full requirements document with acceptance criteria | gpt-4.1-mini |
| **architect** | reads the requirements, selects the tech stack (flask + react by default), and outputs architecture docs + starter `requirements.txt` / `package.json` | gpt-4.1 |
| **project lead** | breaks the architecture into numbered sprints with per-agent task assignments, files to create, and done criteria | gpt-4.1 |
| **hr director** | reads the sprint plan, decides which dev personas are needed, writes a system prompt for each one | gpt-4.1-mini |

every agent emits real-time events over websocket — the war room UI shows each agent's orb pulsing, thinking, and going green as it completes.

### execution phase

once planning completes a "run sprints" button appears. clicking it:

1. **sandbox setup** — creates an isolated python venv and runs `npm install` in `workspaces/<project-id>/`
2. **sprint loop** — for each sprint:
   - **python dev + react dev** write code in parallel (separate claude code CLI processes)
   - **qa dev** writes pytest tests after the devs finish (needs their code to exist first)
   - **pytest runs** in the sandbox — pass/fail counts stream to the build log
   - **project lead** reviews what was built and updates notes for the next sprint
3. **final reviewer** — a separate claude code session reads the entire workspace, fixes fatal errors (broken imports, syntax errors, missing return statements, api url mismatches), and produces a structured scorecard: score 1–10, delivered features, missing features, remaining bugs, ship/iterate/rebuild recommendation
4. **demo launch** — flask starts on port 9000, vite dev server on port 9001. the entourage UI opens a live preview panel.

### real-time updates

every event from the backend (agent thinking, file written, sprint done, test results, cost) publishes to an in-process event bus. the websocket endpoint fans those to the frontend. the war room reacts to every event with no polling.

---

## architecture

```
entourage/
├── server/                   fastapi backend — one process, one port
│   ├── main.py               app factory, static file serving, proactor loop policy
│   ├── config.py             pydantic-settings config (env vars)
│   ├── database.py           sqlalchemy async engine + session
│   ├── api/
│   │   ├── projects.py       CRUD: POST /api/projects, GET, DELETE
│   │   ├── planning.py       POST /api/<id>/plan — starts planning pipeline
│   │   ├── execution.py      POST /api/<id>/execute — starts sprint execution
│   │   └── ws.py             /ws/<id> — websocket event stream
│   ├── models/               sqlalchemy ORM models (project, artifact, sprint_run, message)
│   └── services/
│       ├── planning_service.py   bridges planning pipeline → db + event bus
│       ├── execution_service.py  bridges sprint pipeline + demo launcher → db + event bus
│       └── event_bus.py          in-process asyncio.Queue pub/sub
│
├── agents/                   planning agent definitions
│   ├── base.py               BaseAgent (reads system prompt from prompts/, calls llm client)
│   ├── registry.py           @AgentRegistry.register decorator + discover_agents()
│   ├── engineering_manager.py
│   ├── product_owner.py
│   ├── architect.py
│   ├── project_lead.py
│   ├── hr.py
│   └── stacks/profiles.py    per-stack config: launch commands, test command, default agents
│
├── orchestrator/             planning pipeline runner
│   ├── planning_pipeline.py  EM → PO → Arch → PL → HR sequencing, event emission
│   ├── pipeline_state.py     PipelineState dataclass passed between phases
│   ├── clarification_bus.py  asyncio.Future bridge for EM ↔ user clarification Q&A
│   ├── artifact_writer.py    writes architecture docs to workspace
│   ├── context_builder.py    builds per-agent context from prior artifacts
│   └── memory_store.py       in-memory agent memory (no DB dependency)
│
├── execution/                sprint execution pipeline
│   ├── sandbox.py            creates venv, runs npm install, manages workspace files
│   ├── claude_coder.py       spawns claude CLI subprocess per agent per sprint
│   ├── sprint_pipeline.py    sprint loop: parallel devs → qa → pl review
│   ├── reviewer.py           final reviewer: fix fatal errors + build verdict
│   └── demo_launcher.py      launches flask + vite, streams logs, monitors health
│
├── llm/                      llm provider abstraction
│   ├── base_provider.py      abstract LLMProvider interface
│   ├── azure_openai.py       azure openai REST client (no SDK — avoids dependency hell)
│   └── client.py             thin wrapper: async_call_llm_tracked()
│
├── utils/
│   └── parser.py             parse architect output, sprint plans, review summaries
│
├── prompts/                  system prompt txt files for each planning agent
│   ├── engineering_manager.txt
│   ├── product_owner.txt
│   ├── architect.txt
│   ├── project_lead.txt
│   └── hr.txt
│
├── frontend/                 react + typescript UI (vite)
│   └── src/
│       ├── App.tsx           entire UI: war room, monitor panel, build log, file browser
│       ├── index.css         all styles
│       ├── api/client.ts     typed api wrappers
│       └── hooks/
│           ├── useWebSocket.ts   ws connection with exponential-backoff reconnect
│           └── useCanvasNav.ts   pan/zoom for the war room canvas
│
├── workspaces/               generated project sandboxes (gitignored — can be many GB)
├── Dockerfile                single-stage build: python + node + claude code CLI
├── fly.toml                  fly.io deployment config (persistent volume, health checks)
└── run_server.py             local dev server with hot reload
```

**database:** sqlite via sqlalchemy + aiosqlite. one file, zero configuration. tables created automatically on startup. on fly.io the file lives on a persistent volume at `/data/entourage.db`.

**generated projects:** each project gets `workspaces/<uuid>/` containing:
- an isolated python venv
- npm-installed `frontend/node_modules/`
- all generated source code written by claude code agents
- `memory/<agent>_log.md` files — agents read their own log before each sprint so they remember what they built previously
- `memory/project_state.md` — a shared cross-agent state file updated after each sprint

---

## credentials

entourage needs two sets of api credentials:

**azure openai** (five planning agents):
- `AZURE_OPENAI_API_KEY` — your azure openai resource key
- `AZURE_OPENAI_ENDPOINT` — resource endpoint, e.g. `https://myresource.openai.azure.com`
- `AZURE_OPENAI_DEPLOYMENT_FULL` — deployment name for gpt-4.1 (architect + project lead)
- `AZURE_OPENAI_DEPLOYMENT_MINI` — deployment name for gpt-4.1-mini (em + po + hr)
- `AZURE_OPENAI_API_VERSION` — api version, e.g. `2024-12-01-preview`

**anthropic** (claude code dev agents + final reviewer):
- `ANTHROPIC_API_KEY` — `sk-ant-...`

**how credentials flow through the app:**

on the landing page, users paste their credentials into five text inputs. when they submit, the credentials are:
1. sent to `POST /api/<id>/plan` in the request body
2. stored in the project's config JSON in sqlite (so execution can read them too)
3. temporarily patched into `os.environ` before each pipeline run and restored in a `finally` block

since the server runs a single-threaded async event loop, env patching between concurrent projects doesn't race.

**admin password shortcut:** if you set `ADMIN_PASSWORD` in your server's environment, users can type that password into all five credential fields on the landing page. the server detects that all five fields match, validates the password, and uses the server's own environment credentials instead of the user-submitted values. useful for demos where you don't want to share keys with every visitor.

---

## local development

**requirements:** python 3.11+, node 18+, claude code CLI (`npm install -g @anthropic-ai/claude-code`), an azure openai resource with gpt-4.1 and gpt-4.1-mini deployments, an anthropic api key.

```bash
# 1. clone and install python deps
git clone https://github.com/yourname/entourage.git
cd entourage
python -m venv .venv
.venv\Scripts\activate          # windows
# source .venv/bin/activate     # mac/linux
pip install -r requirements.txt

# 2. install frontend deps
cd frontend && npm install && cd ..

# 3. configure credentials
cp .env.example .env
# edit .env with your azure + anthropic keys

# 4. start the backend (port 8000, hot reload)
python run_server.py

# 5. start the frontend dev server (port 3000, separate terminal)
cd frontend && npm run dev
```

open http://localhost:3000. vite proxies `/api` and `/ws` to `localhost:8000`.

**windows note:** uvicorn uses `SelectorEventLoop` by default which doesn't support `asyncio.create_subprocess_exec`. `server/main.py` forces `WindowsProactorEventLoopPolicy` at startup. on linux (fly.io) this policy is ignored and the default loop works fine.

---

## environment variables

copy `.env.example` to `.env` and fill in your values:

| variable | description |
|----------|-------------|
| `AZURE_OPENAI_API_KEY` | azure resource key |
| `AZURE_OPENAI_ENDPOINT` | e.g. `https://myresource.openai.azure.com` |
| `AZURE_OPENAI_DEPLOYMENT_FULL` | gpt-4.1 deployment name (architect, project lead) |
| `AZURE_OPENAI_DEPLOYMENT_MINI` | gpt-4.1-mini deployment name (em, po, hr) |
| `AZURE_OPENAI_API_VERSION` | e.g. `2024-12-01-preview` |
| `ANTHROPIC_API_KEY` | sk-ant-... (for claude code dev agents) |
| `ADMIN_PASSWORD` | optional — enables the admin shortcut on the landing page |
| `DATABASE_URL` | defaults to `sqlite+aiosqlite:///./entourage.db` |
| `WORKSPACE_DIR` | where generated projects are stored. defaults to `./workspaces/` |
| `CORS_ORIGINS` | JSON array of allowed origins, e.g. `["https://yourapp.fly.dev"]` |

---

## deploying to fly.io

**why fly.io:** entourage spawns long-running subprocesses, writes to disk, uses websockets, and keeps in-memory state — none of which work on serverless platforms (vercel, netlify). fly.io gives you a persistent VM with a mounted volume, which is exactly what this architecture needs.

**one-time setup:**

```bash
# install flyctl
# mac:     brew install flyctl
# windows: winget install flyctl

flyctl auth login
flyctl launch                                        # creates app, sets primary region
flyctl volumes create entourage_data --size 5        # 5 GB persistent storage
```

after `flyctl launch`, update `app = "entourage"` in `fly.toml` to match the app name fly assigned.

**set secrets:**

```bash
flyctl secrets set \
  AZURE_OPENAI_API_KEY="..." \
  AZURE_OPENAI_ENDPOINT="https://..." \
  AZURE_OPENAI_DEPLOYMENT_FULL="gpt-4.1" \
  AZURE_OPENAI_DEPLOYMENT_MINI="gpt-4.1-mini" \
  AZURE_OPENAI_API_VERSION="2024-12-01-preview" \
  ANTHROPIC_API_KEY="sk-ant-..." \
  ADMIN_PASSWORD="your-admin-password"
```

**deploy:**

```bash
flyctl deploy
```

the dockerfile builds the react frontend at image build time and serves it as static files from fastapi — one process, one port. websockets work on fly.io out of the box.

**estimated cost:** shared-cpu-1x 512MB machine ($3.19/month) + 5GB volume ($0.75/month) = $3.94/month, covered by the hobby plan's $5 usage credit. net cost: **$5/month flat**.

---

## full flow walkthrough

1. user types a project description on the landing page and submits api credentials
2. `POST /api/projects` creates the project record in sqlite
3. `POST /api/<id>/plan` starts the planning pipeline as a background asyncio task
4. **engineering manager** may ask 0–3 clarifying questions — answers arrive via `POST /api/<id>/po-answer` which resolves an asyncio future inside the pipeline
5. each planning agent emits events to the event bus; the frontend renders them in real time over websocket — agent orbs pulse when active, go green when done
6. when hr finishes, a banner appears: "planning complete — the team is ready to build."
7. user clicks **run sprints** → `POST /api/<id>/execute`
8. **sandbox setup:** venv created, pip installs `requirements.txt`, npm installs frontend deps
9. **sprint loop** (for each sprint):
   - python dev and react dev receive their task description, prior sprint memory, and project state — each runs as an isolated `claude -p --dangerously-skip-permissions --output-format stream-json` subprocess in the workspace directory
   - qa dev runs after devs finish — writes pytest tests against the code they just produced
   - pytest runs; stdout streams to the build log with pass/fail counts
   - project lead reviews files written vs expected and updates sprint notes for next iteration
10. **final reviewer** runs a single claude session that reads every file, fixes fatal errors, and writes a structured verdict (score, delivered, missing, bugs, recommendation)
11. project status → `mvp_ready` — a **launch demo** button appears
12. `POST /api/<id>/demo/start` → flask starts on port 9000, vite dev server on port 9001 — the UI opens a live preview iframe

---

## notes

- **workspaces/ is gitignored** — generated projects are local only and can grow to many GB (each has a full venv + node_modules)
- **entourage.db is gitignored** — sqlite db resets on fresh deployments; fly.io volume means it persists across deploys in production
- **claude code CLI must be authenticated** — run `claude auth` once per machine. on fly.io, setting `ANTHROPIC_API_KEY` as a secret is enough; claude code reads it automatically
- **single uvicorn worker required** — the event bus and active demo registry are in-process singletons. multiple workers would have separate instances and events would be lost. run with `--workers 1`
- **demo ports 9000 and 9001 are fixed** — the generated apps hardcode these ports. the demo launcher kills whatever is on those ports before starting fresh. this means you can only run one demo at a time per machine
