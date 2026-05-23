// api client — typed wrappers over the fastapi backend.
//
// all calls go to /api/* which in dev is proxied by vite to localhost:8000.
// in production fastapi serves the built frontend directly so no proxy is needed.
//
// request() throws with the server's `detail` field so callers get readable error messages.

const BASE = '/api';

// auth headers injected per-request from the current Supabase session
let _userId: string | null = null
let _userEmail: string | null = null

export function setAuthHeaders(userId: string | null, email: string | null) {
  _userId = userId
  _userEmail = email
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const authHeaders: Record<string, string> = {}
  if (_userId)    authHeaders['x-user-id']    = _userId
  if (_userEmail) authHeaders['x-user-email'] = _userEmail

  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...authHeaders },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

async function requestText(path: string): Promise<string> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(res.statusText);
  return res.text();
}

// ── types ──────────────────────────────────────────────────────────────────────

export interface Project {
  id: string;
  name: string;
  user_story: string;
  status: 'created' | 'running' | 'completed' | 'failed' | 'executing' | 'mvp_ready' | 'execution_failed';
  workspace_path: string;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface SprintRun {
  sprint: number;
  status: string;
  notes: string;
  created_at: string;
}

export interface ExecutionStatus {
  project_id: string;
  status: string;
  running: boolean;
  workspace_path: string;
  sprint_runs: SprintRun[];
  demo_urls: Record<string, string>;
  demo_primary_url: string;
}

export interface DemoStatus {
  project_id: string;
  status: string;
  urls: Record<string, string>;
  primary_url: string;   // preferred URL to show the user (frontend over backend)
  services: Array<{
    name: string;
    url: string;
    port: number;
    ready: boolean;
    command: string;
  }>;
}

export interface WorkspaceFile {
  path: string;
  size: number;
}

export interface Artifact {
  id: number;
  agent: string;
  artifact_type: string;
  title: string;
  content: string;
  tokens_used: number;
  cost: number;
  created_at: string;
}

export interface PipelineStatus {
  project_id: string;
  status: string;
  running: boolean;
}

// ── api surface ────────────────────────────────────────────────────────────────

export const api = {
  // projects
  listProjects: () =>
    request<Project[]>('/projects'),

  getProject: (id: string) =>
    request<Project>(`/projects/${id}`),

  createProject: (userStory: string, name?: string, config?: Record<string, unknown>) =>
    request<Project>('/projects', {
      method: 'POST',
      body: JSON.stringify({ user_story: userStory, name: name || '', config: config || {} }),
    }),

  deleteProject: (id: string) =>
    request<{ deleted: string }>(`/projects/${id}`, { method: 'DELETE' }),

  // planning — credentials are sent here once and stored in the project config
  // pass use_env_creds: true + admin_password to use the server's saved .env creds
  startPlanning: (id: string, credentials?: Record<string, string | boolean>) =>
    request<{ status: string }>(`/projects/${id}/plan`, {
      method: 'POST',
      body: JSON.stringify({ credentials: credentials || {} }),
    }),

  getPlanningStatus: (id: string) =>
    request<{ project_id: string; status: string; running: boolean }>(`/projects/${id}/plan-status`),

  startPipeline: (id: string) =>
    request<{ status: string }>(`/projects/${id}/run`, { method: 'POST' }),

  // artifacts
  getArtifacts: (id: string) =>
    request<{ project_id: string; artifacts: Artifact[] }>(`/projects/${id}/artifacts`),

  // export
  exportMarkdown: (id: string) =>
    requestText(`/projects/${id}/export/markdown`),

  exportWorkPackages: (id: string) =>
    request<{ project_id: string; work_packages: Array<Record<string, unknown>> }>(`/projects/${id}/work-packages`),

  // execution
  startExecution: (id: string) =>
    request<{ status: string; project_id: string }>(`/projects/${id}/execute`, { method: 'POST' }),

  getExecutionStatus: (id: string) =>
    request<ExecutionStatus>(`/projects/${id}/execution`),

  approveSprint: (id: string, sprintNumber: number, notes?: string) =>
    request<{ status: string; sprint: number }>(`/projects/${id}/sprints/${sprintNumber}/approve`, {
      method: 'POST',
      body: JSON.stringify({ notes: notes || '' }),
    }),

  rejectSprint: (id: string, sprintNumber: number, notes: string) =>
    request<{ status: string; sprint: number }>(`/projects/${id}/sprints/${sprintNumber}/reject`, {
      method: 'POST',
      body: JSON.stringify({ notes }),
    }),

  // demo — launches the generated flask+react app on ports 9000/9001
  startDemo: (id: string) =>
    request<DemoStatus>(`/projects/${id}/demo/start`, { method: 'POST' }),

  stopDemo: (id: string) =>
    request<{ status: string }>(`/projects/${id}/demo/stop`, { method: 'POST' }),

  getDemoStatus: (id: string) =>
    request<DemoStatus>(`/projects/${id}/demo`),

  // nuclear admin reset — wipes all workspaces + all DB records
  nukeEverything: () =>
    request<{ nuked: boolean; size_before_mb: number; size_after_bytes: number }>(
      '/admin/nuke', { method: 'POST' }
    ),

  // workspace file browser
  listWorkspaceFiles: (id: string) =>
    request<{ project_id: string; files: WorkspaceFile[] }>(`/projects/${id}/workspace`),

  readWorkspaceFile: (id: string, path: string) =>
    request<{ path: string; content: string }>(
      `/projects/${id}/workspace/file?path=${encodeURIComponent(path)}`
    ),
};
