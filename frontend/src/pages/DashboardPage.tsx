// projects dashboard — shows the user's projects and lets them create new ones

import { useEffect, useState, useCallback } from 'react'
import { useAuth } from '../lib/AuthContext'
import { api } from '../api/client'
import type { Project } from '../api/client'
import { supabase } from '../lib/supabase'

interface Props {
  onOpenProject: (projectId: string) => void
  onNewProject: () => void
}

export default function DashboardPage({ onOpenProject, onNewProject }: Props) {
  const { user, signOut } = useAuth()
  const [projects, setProjects]   = useState<Project[]>([])
  const [loading, setLoading]     = useState(true)
  const [deleting, setDeleting]   = useState<string | null>(null)
  const [savedCreds, setSavedCreds] = useState<Record<string, string>>({})
  const [showCreds, setShowCreds] = useState(false)
  const [credSaving, setCredSaving] = useState(false)
  const [credSaved, setCredSaved] = useState(false)
  const [nuking, setNuking] = useState(false)
  const [nukeResult, setNukeResult] = useState<string | null>(null)
  const [nukeConfirm, setNukeConfirm] = useState(false)

  const firstName = user?.user_metadata?.full_name?.split(' ')[0]
    || user?.email?.split('@')[0]
    || 'there'

  const load = useCallback(async () => {
    try {
      const ps = await api.listProjects()
      setProjects(ps)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // load saved creds from supabase user_metadata; auto-open panel if missing
  useEffect(() => {
    if (!user) return
    const creds = user.user_metadata?.saved_creds || {}
    setSavedCreds(creds)
    // open creds panel automatically if creds are not yet saved
    const complete = ['azure_openai_api_key','azure_openai_endpoint','azure_openai_deployment_full','azure_openai_api_version','anthropic_api_key']
      .every(k => (creds[k] || '').trim().length > 0)
    if (!complete) setShowCreds(true)
  }, [user])

  const handleDelete = async (id: string) => {
    if (!window.confirm('Delete this project? This cannot be undone.')) return
    setDeleting(id)
    try { await api.deleteProject(id); setProjects(p => p.filter(x => x.id !== id)) }
    finally { setDeleting(null) }
  }

  const isAdmin = user?.email === 'smukherjee39@wisc.edu'

  const handleNuke = async () => {
    if (!nukeConfirm) { setNukeConfirm(true); return }
    setNuking(true)
    try {
      const r = await api.nukeEverything()
      setNukeResult(`Hard reset ✓  freed ${r.size_before_mb} MB`)
      setProjects([])
    } catch (e) {
      setNukeResult(`Error: ${e}`)
    } finally {
      setNuking(false)
      setNukeConfirm(false)
    }
  }

  const handleSaveCreds = async () => {
    setCredSaving(true)
    const { error } = await supabase.auth.updateUser({
      data: { saved_creds: savedCreds }
    })
    setCredSaving(false)
    if (!error) { setCredSaved(true); setTimeout(() => setCredSaved(false), 2000) }
  }

  const statusColor = (s: string) => {
    if (s === 'mvp_ready') return 'var(--green)'
    if (s === 'executing' || s === 'running') return '#a89fff'
    if (s === 'failed' || s === 'execution_failed') return '#ff6b6b'
    if (s === 'completed') return 'var(--purple)'
    return 'var(--dim)'
  }

  const statusLabel = (s: string) => {
    const map: Record<string, string> = {
      mvp_ready: 'Ready',
      executing: 'Building',
      running: 'Planning',
      completed: 'Planned',
      failed: 'Failed',
      execution_failed: 'Build failed',
      created: 'New',
    }
    return map[s] || s
  }

  // creds are required before starting a new project
  const credsComplete = ['azure_openai_api_key','azure_openai_endpoint','azure_openai_deployment_full','azure_openai_api_version','anthropic_api_key']
    .every(k => (savedCreds[k] || '').trim().length > 0)

  return (
    <div className="dash">
      <div className="dash-topbar">
        <div className="dash-brand">
          <img src="/entourage_icon.png" alt="" className="dash-logo" />
          <span className="dash-wordmark">entourage</span>
        </div>
        <div className="dash-user">
          <span className="dash-hello">Hello, {firstName}</span>
          <button className="dash-creds-btn" onClick={() => setShowCreds(v => !v)}>
            {showCreds ? 'Hide credentials' : 'API credentials'}
          </button>
          <button className="dash-signout" onClick={signOut}>Sign out</button>
        </div>
      </div>

      {showCreds && (
        <div className="dash-creds-panel">
          <p className="dash-creds-title">Saved API credentials</p>
          <p className="dash-creds-hint">These are used automatically for every project. Leave blank to enter them per-project.</p>
          <div className="dash-creds-grid">
            {([
              ['azure_openai_api_key',        'Azure OpenAI Key',    'password', 'Azure API key'],
              ['azure_openai_endpoint',        'Azure Endpoint',      'text',     'https://your-resource.openai.azure.com'],
              ['azure_openai_deployment_full', 'Azure Deployment',    'text',     'gpt-4.1'],
              ['azure_openai_api_version',     'Azure API Version',   'text',     '2024-12-01-preview'],
              ['anthropic_api_key',            'Anthropic API Key',   'password', 'sk-ant-...'],
            ] as [string, string, string, string][]).map(([key, label, type, ph]) => (
              <div key={key} className="land-cred-row">
                <label>{label}</label>
                <input
                  type={type}
                  placeholder={ph}
                  value={savedCreds[key] || ''}
                  onChange={e => setSavedCreds(p => ({ ...p, [key]: e.target.value }))}
                  autoComplete="off"
                />
              </div>
            ))}
          </div>
          <button className="dash-save-creds" onClick={handleSaveCreds} disabled={credSaving}>
            {credSaving ? <span className="spin" /> : credSaved ? '✓ Saved' : 'Save credentials'}
          </button>
        </div>
      )}

      <div className="dash-body">
        <div className="dash-header">
          <h1 className="dash-title">Your projects</h1>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {nukeResult && <span style={{ fontSize: '0.7rem', color: 'var(--green)' }}>{nukeResult}</span>}
            {isAdmin && nukeConfirm && !nuking && (
              <button className="dash-card-delete" onClick={() => setNukeConfirm(false)} style={{ fontSize: '0.7rem' }}>Cancel</button>
            )}
            {isAdmin && (
              <button
                className="dash-card-delete"
                onClick={handleNuke}
                disabled={nuking}
                style={{ color: nukeConfirm ? '#ff4444' : undefined, borderColor: nukeConfirm ? '#ff4444' : undefined, fontSize: '0.72rem', padding: '0.3rem 0.75rem' }}
              >
                {nuking ? <span className="spin" /> : nukeConfirm ? '⚠ Confirm hard reset?' : '☢ Hard reset'}
              </button>
            )}
            <button
              className="dash-new"
              onClick={onNewProject}
              disabled={!credsComplete}
              title={!credsComplete ? 'Save your API credentials first' : undefined}
              style={!credsComplete ? { opacity: 0.5, cursor: 'not-allowed' } : undefined}
            >
              + New project
            </button>
            {!credsComplete && (
              <span style={{ fontSize: '0.72rem', color: 'var(--dim)' }}>
                ↑ Save credentials first
              </span>
            )}
          </div>
        </div>

        {loading ? (
          <div className="dash-loading"><span className="spin" /></div>
        ) : projects.length === 0 ? (
          <div className="dash-empty">
            <p>No projects yet.</p>
            <button className="dash-new" onClick={onNewProject}>Start your first project</button>
          </div>
        ) : (
          <div className="dash-grid">
            {projects.map(p => (
              <div key={p.id} className="dash-card" onClick={() => onOpenProject(p.id)}>
                <div className="dash-card-header">
                  <span className="dash-card-name">{p.name || p.user_story.slice(0, 40)}</span>
                  <span className="dash-card-status" style={{ color: statusColor(p.status) }}>
                    {statusLabel(p.status)}
                  </span>
                </div>
                <p className="dash-card-story">{p.user_story.slice(0, 120)}{p.user_story.length > 120 ? '…' : ''}</p>
                <div className="dash-card-footer">
                  <span className="dash-card-date">{new Date(p.created_at).toLocaleDateString()}</span>
                  <button
                    className="dash-card-delete"
                    onClick={e => { e.stopPropagation(); handleDelete(p.id) }}
                    disabled={deleting === p.id}
                  >
                    {deleting === p.id ? <span className="spin" /> : 'Delete'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
