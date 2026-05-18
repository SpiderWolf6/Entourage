import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { api } from './api/client'
import { useWebSocket, type WSEvent } from './hooks/useWebSocket'
import { useCanvasNav } from './hooks/useCanvasNav'
import type { WorkspaceFile } from './api/client'

// ─── Types ────────────────────────────────────────────────────────────────────

type AppState = 'idle' | 'submitting' | 'running' | 'done' | 'error'
type AppPhase = 'clarifying' | 'planning' | 'building' | 'reviewing' | 'done'

const PIPELINE_AGENTS = [
  'engineering_manager', 'product_owner', 'architect', 'project_lead', 'hr',
] as const
type PipelineAgent = typeof PIPELINE_AGENTS[number]

const AGENT_META: Record<PipelineAgent, {
  label: string; abbr: string; color: string; colorB: string
  seat: { x: number; y: number }
  roleDesc: string
}> = {
  engineering_manager: { label: 'Eng. Manager',  abbr: 'EM', color: '#4a4580', colorB: '#6b64b8', seat: { x: -320, y: -170 }, roleDesc: 'Clarifies requirements & scopes the project' },
  product_owner:       { label: 'Product Owner', abbr: 'PO', color: '#0e7490', colorB: '#1a9db5', seat: { x:  320, y: -170 }, roleDesc: 'Defines features & acceptance criteria' },
  architect:           { label: 'Architect',     abbr: 'AR', color: '#6b3fa0', colorB: '#9060cc', seat: { x:  320, y:  170 }, roleDesc: 'Designs system architecture & tech stack' },
  project_lead:        { label: 'Project Lead',  abbr: 'PL', color: '#9e1a55', colorB: '#c73070', seat: { x:    0, y:  280 }, roleDesc: 'Plans sprints & reviews code quality' },
  hr:                  { label: 'HR Director',   abbr: 'HR', color: '#9a4a10', colorB: '#c06018', seat: { x: -320, y:  170 }, roleDesc: 'Assembles & briefs the dev team' },
}

const DEV_AGENT_LABELS: Record<string, string> = {
  python_dev: 'Python Dev', react_dev: 'React Dev', flutter_dev: 'Flutter Dev',
  qa_dev: 'QA Dev', c_dev: 'C Dev', backend_dev: 'Backend Dev',
  frontend_dev: 'Frontend Dev', fullstack_dev: 'Fullstack Dev',
  go_dev: 'Go Dev', rust_dev: 'Rust Dev', node_dev: 'Node Dev',
  discord_dev: 'Discord Dev', slack_dev: 'Slack Dev', mobile_dev: 'Mobile Dev',
}

const ARCHETYPE_META: Record<string, { color: string; colorB: string; role: string; abbr: string; tech: string }> = {
  backend_dev:   { color: '#0e7490', colorB: '#1a9db5', role: 'Backend Dev',   abbr: 'BE', tech: 'Python · Flask' },
  frontend_dev:  { color: '#4a4580', colorB: '#6b64b8', role: 'Frontend Dev',  abbr: 'FE', tech: 'React · TypeScript' },
  react_dev:     { color: '#4a4580', colorB: '#6b64b8', role: 'React Dev',     abbr: 'RX', tech: 'React · TypeScript' },
  python_dev:    { color: '#0e7490', colorB: '#1a9db5', role: 'Python Dev',    abbr: 'PY', tech: 'Python' },
  mobile_dev:    { color: '#9e1a55', colorB: '#c73070', role: 'Mobile Dev',    abbr: 'MB', tech: 'React Native' },
  data_engineer: { color: '#9a4a10', colorB: '#c06018', role: 'Data Engineer', abbr: 'DE', tech: 'Python · SQL' },
  ml_engineer:   { color: '#6b3fa0', colorB: '#9060cc', role: 'ML Engineer',   abbr: 'ML', tech: 'Python · PyTorch' },
  devops:        { color: '#1a6645', colorB: '#278a5e', role: 'DevOps',        abbr: 'DO', tech: 'Docker · CI/CD' },
  researcher:    { color: '#8a6a10', colorB: '#b08520', role: 'Researcher',    abbr: 'RS', tech: 'Analysis · Docs' },
  data_analyst:  { color: '#0e7490', colorB: '#1a9db5', role: 'Data Analyst',  abbr: 'DA', tech: 'SQL · Python' },
  systems_dev:   { color: '#3a4a5c', colorB: '#506070', role: 'Systems Dev',   abbr: 'SY', tech: 'C · Systems' },
  fullstack_dev: { color: '#9a4a10', colorB: '#c06018', role: 'Fullstack Dev', abbr: 'FS', tech: 'Flask · React' },
  qa_dev:        { color: '#1a6645', colorB: '#278a5e', role: 'QA Engineer',   abbr: 'QA', tech: 'Testing · Pytest' },
  go_dev:        { color: '#0e7490', colorB: '#1a9db5', role: 'Go Dev',        abbr: 'GO', tech: 'Go' },
  rust_dev:      { color: '#9a4a10', colorB: '#c06018', role: 'Rust Dev',      abbr: 'RS', tech: 'Rust' },
  node_dev:      { color: '#1a6645', colorB: '#278a5e', role: 'Node Dev',      abbr: 'ND', tech: 'Node.js · Express' },
}

interface AgentState {
  status: 'idle' | 'thinking' | 'done'
  thoughts: string[]
  artifact: string | null
  artifactType: string | null
}

interface BuildVerdict {
  score: number
  recommendation: 'ship' | 'iterate' | 'rebuild'
  delivered: string[]
  missing: string[]
  fatal_fixes: string[]
  bugs_remaining: string[]
  fidelity_summary: string
}

interface SpawnedAgent {
  name: string; archetype: string; systemPrompt: string
}

interface Clarification {
  question: string; round: number; maxRounds: number; agent: PipelineAgent
}

type ModalTarget =
  | { kind: 'pipeline'; agent: PipelineAgent }
  | { kind: 'spawned'; agent: SpawnedAgent }
  | { kind: 'clarification'; clarification: Clarification }
  | null

// Maps sprint-plan role names → HR archetype names so active glow matches spawned orbs
const ROLE_TO_ARCHETYPE: Record<string, string> = {
  python_dev: 'backend_dev', react_dev: 'frontend_dev',
  node_dev: 'backend_dev', go_dev: 'go_dev', rust_dev: 'rust_dev',
}
function agentNameToArchetype(name: string): string {
  return ROLE_TO_ARCHETYPE[name] ?? name
}

function makeDefaultStates(): Record<PipelineAgent, AgentState> {
  const entries = PIPELINE_AGENTS.map(a => [a, { status: 'idle' as const, thoughts: [] as string[], artifact: null, artifactType: null }])
  return Object.fromEntries(entries) as Record<PipelineAgent, AgentState>
}


function derivePhase(
  appState: AppState,
  agentStates: Record<PipelineAgent, AgentState>,
  clarification: Clarification | null,
  execRunning: boolean,
  execEvents: Array<{ type: string; data: Record<string, unknown> }>,
  isMvpReady: boolean,
  reviewerRunning: boolean,
): AppPhase {
  if (isMvpReady) return 'done'
  if (reviewerRunning) return 'reviewing'
  if (execRunning || execEvents.some(e => e.type === 'sprint_start' || e.type === 'execution_start')) return 'building'
  const anyDone = PIPELINE_AGENTS.some(a => agentStates[a].status === 'done')
  if (appState === 'running' && !anyDone) return 'clarifying'
  if (clarification !== null) return 'clarifying'
  return 'planning'
}

// ─── App root ─────────────────────────────────────────────────────────────────

export default function App() {
  const [appState, setAppState]     = useState<AppState>('idle')
  const [story, setStory]           = useState('')
  const [projectName, setProjectName] = useState('')
  const [projectId, setProjectId]   = useState<string | null>(
    () => new URLSearchParams(window.location.search).get('p')
  )
  const [agentStates, setAgentStates] = useState<Record<PipelineAgent, AgentState>>(makeDefaultStates())
  const [activeAgent, setActiveAgent] = useState<PipelineAgent | null>(null)
  const [spawnedAgents, setSpawnedAgents] = useState<SpawnedAgent[]>([])
  const [clarification, setClarification] = useState<Clarification | null>(null)
  // Tracks the highest em_question round the user has already answered,
  // so replayed events from earlier rounds don't re-show the banner.
  const answeredRoundRef = useRef<number>(0)
  // Tracks how many events have already been processed so replays are skipped
  const processedEventsRef = useRef<number>(0)
  const [modal, setModal]           = useState<ModalTarget>(null)
  const [monitorOpen, setMonitorOpen] = useState(false)
  const [errorMsg, setErrorMsg]     = useState('')
  const [creds, setCreds] = useState({ azureKey: '', azureEndpoint: '', azureDeploymentFull: '', azureApiVersion: '', anthropicKey: '' })
  const [totalCostUsd, setTotalCostUsd] = useState(0)
  const hrOutputRef = useRef<string>('')

  const [execPanel, setExecPanel]   = useState(false)
  const [execRunning, setExecRunning] = useState(false)
  const [execEvents, setExecEvents] = useState<Array<{ type: string; data: Record<string, unknown> }>>([])
  const [pendingGates, setPendingGates] = useState<number[]>([])
  const [demoUrl, setDemoUrl]       = useState<string>('')
  const [demoLoading, setDemoLoading] = useState(false)
  const [buildVerdict, setBuildVerdict] = useState<BuildVerdict | null>(null)
  const [reviewerRunning, setReviewerRunning] = useState(false)

  // Active spawned agent during execution (for glow effect)
  const [activeSpawnedNames, setActiveSpawnedNames] = useState<Set<string>>(new Set())

  // Human-in-the-loop mode
  const [hiloMode, setHiloMode]     = useState<'auto' | 'review'>('auto')
  const hiloModeRef = useRef(hiloMode)
  useEffect(() => { hiloModeRef.current = hiloMode }, [hiloMode])

  // File panel
  const [filePanel, setFilePanel]   = useState(false)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [fileContent, setFileContent] = useState('')
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[]>([])
  const [filePanelTab, setFilePanelTab] = useState<'files' | 'changes'>('files')

  // Onboarding tour — null means not showing, starts on every new project
  const [tourStep, setTourStep]     = useState<number | null>(null)

  // Keep URL in sync with projectId so page reloads resume the same project
  useEffect(() => {
    const url = new URL(window.location.href)
    if (projectId) {
      url.searchParams.set('p', projectId)
    } else {
      url.searchParams.delete('p')
    }
    window.history.replaceState(null, '', url.toString())
  }, [projectId])

  // On mount: if URL has a project ID, fetch it and restore state
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('p')
    if (!id) return
    api.getProject(id).then(project => {
      setProjectId(project.id)
      setProjectName(project.name || '')
      setStory(project.user_story || '')
      const s = project.status
      if (s === 'completed' || s === 'mvp_ready') setAppState('done')
      else if (s === 'executing') { setAppState('done'); setExecRunning(true); setExecPanel(true) }
      else if (s === 'failed' || s === 'execution_failed') setAppState('error')
      else setAppState('running') // running, created, or unknown — show war room and let WS catch up
    }).catch(() => {
      // Project not found — clear stale URL param and show landing
      window.history.replaceState(null, '', '/')
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const { events } = useWebSocket(projectId ?? undefined)

  const handleApproveSprint = useCallback(async (sprint: number) => {
    if (!projectId) return
    await api.approveSprint(projectId, sprint)
    setPendingGates(prev => prev.filter(n => n !== sprint))
  }, [projectId])

  useEffect(() => {
    const newEvents = events.slice(processedEventsRef.current)
    processedEventsRef.current = events.length
    for (const ev of newEvents) {
      const agent = ev.data?.agent as PipelineAgent | undefined

      if (ev.type === 'pipeline_start') setAppState('running')

      if (ev.type === 'agent_start' && agent && PIPELINE_AGENTS.includes(agent)) {
        setActiveAgent(agent)
        setAgentStates(prev => ({ ...prev, [agent]: { ...prev[agent], status: 'thinking' } }))
      }

      if (ev.type === 'clarification_received') {
        // Backend confirmed it got the answer — clear any stale question banner
        setClarification(null)
      }

      if (ev.type === 'agent_thinking' && agent && PIPELINE_AGENTS.includes(agent)) {
        const thought = ev.data?.content as string || ''
        if (thought.includes('Got it') || thought.includes('Got your') || thought.includes('Writing structured') || thought.includes('Finalizing')) {
          setClarification(null)
        }
        setAgentStates(prev => ({
          ...prev,
          [agent]: { ...prev[agent], thoughts: [...prev[agent].thoughts, thought] },
        }))
      }

      if (ev.type === 'em_question') {
        const round = ev.data?.round as number || 1
        // Only surface the question if it hasn't been answered yet
        if (round > answeredRoundRef.current) {
          setClarification({ question: ev.data?.question as string || '', round, maxRounds: ev.data?.max_rounds as number || 3, agent: 'engineering_manager' })
        }
      }
      if (ev.type === 'po_question') {
        setClarification({ question: ev.data?.questions as string || '', round: ev.data?.round as number || 1, maxRounds: ev.data?.max_rounds as number || 2, agent: 'product_owner' })
      }

      if (ev.type === 'agent_artifact' && agent && PIPELINE_AGENTS.includes(agent)) {
        const content = ev.data?.content as string || ''
        setAgentStates(prev => ({ ...prev, [agent]: { ...prev[agent], artifact: content, artifactType: ev.data?.artifact_type as string || '' } }))
        if (agent === 'hr') hrOutputRef.current = content
      }

      if (ev.type === 'agent_done' && agent && PIPELINE_AGENTS.includes(agent)) {
        setAgentStates(prev => ({ ...prev, [agent]: { ...prev[agent], status: 'done' } }))
        if (activeAgent === agent) setActiveAgent(null)
        const cost = ev.data?.cost_usd as number
        if (cost) setTotalCostUsd(prev => prev + cost)
      }

      // Track active spawned agents (during execution phase) — multiple can run in parallel
      // Normalize role names to their archetype equivalent so the Set matches sa.name from HR output
      if (ev.type === 'agent_start' && agent && !PIPELINE_AGENTS.includes(agent as PipelineAgent)) {
        const normalized = agentNameToArchetype(agent as string)
        setActiveSpawnedNames(prev => { const n = new Set(prev); n.add(agent as string); n.add(normalized); return n })
      }
      if (ev.type === 'agent_done' && agent && !PIPELINE_AGENTS.includes(agent as PipelineAgent)) {
        const normalized = agentNameToArchetype(agent as string)
        setActiveSpawnedNames(prev => { const n = new Set(prev); n.delete(agent as string); n.delete(normalized); return n })
      }

      // PL glow during execution sprint reviews
      if (ev.type === 'pl_review_start') {
        setActiveAgent('project_lead')
        setAgentStates(prev => ({ ...prev, project_lead: { ...prev.project_lead, status: 'thinking' } }))
      }
      if (ev.type === 'pl_review_done') {
        setActiveAgent(null)
        setAgentStates(prev => ({ ...prev, project_lead: { ...prev.project_lead, status: 'done' } }))
      }

      if (ev.type === 'agent_spawned') {
        const name = ev.data?.spawned_name as string || ''
        const archetype = ev.data?.spawned_archetype as string || ''
        const systemPrompt = (ev.data?.system_prompt as string) || parseSystemPrompt(hrOutputRef.current, name)
        setSpawnedAgents(prev => prev.some(a => a.name === name) ? prev : [...prev, { name, archetype, systemPrompt }])
        const cost = ev.data?.cost_usd as number
        if (cost) setTotalCostUsd(prev => prev + cost)
      }

      if (ev.type === 'pipeline_done') { setActiveAgent(null); setAppState('done'); setMonitorOpen(true) }
      if (ev.type === 'error') { setErrorMsg((ev.data?.message as string) || 'Unknown error'); setAppState('error') }

      // Reviewer events
      if (ev.type === 'reviewer_start') setReviewerRunning(true)
      if (ev.type === 'reviewer_done') {
        setReviewerRunning(false)
        const verdict = ev.data?.verdict as BuildVerdict | undefined
        if (verdict) setBuildVerdict(verdict)
        const reviewerCost = ev.data?.cost_usd as number
        if (reviewerCost) setTotalCostUsd(prev => prev + reviewerCost)
      }
      if (ev.type === 'mvp_ready') {
        const verdict = ev.data?.verdict as BuildVerdict | undefined
        if (verdict) setBuildVerdict(verdict)
      }

      const execTypes = ['execution_start','sprint_start','sprint_done','agent_start','agent_done',
        'agent_stream','pl_review_start','pl_review_done','awaiting_approval','sandbox_progress','mvp_ready',
        'execution_error','all_sprints_done','demo_demo_ready','reviewer_start','reviewer_stream','reviewer_done',
        'demo_service_crashed','demo_demo_error','demo_service_timeout','qa_tests_running','qa_tests_done']
      if (execTypes.includes(ev.type)) {
        setExecEvents(prev => [...prev, { type: ev.type, data: ev.data as Record<string, unknown> }])
      }
      if (ev.type === 'awaiting_approval') {
        const sprint = ev.data?.sprint as number
        if (sprint) {
          if (hiloModeRef.current === 'auto') {
            // auto-approve
            handleApproveSprint(sprint)
          } else {
            setPendingGates(prev => prev.includes(sprint) ? prev : [...prev, sprint])
          }
        }
      }
      if (ev.type === 'sprint_approved' || ev.type === 'sprint_rejected') {
        setPendingGates(prev => prev.filter(n => n !== ev.data?.sprint))
      }
      if (ev.type === 'execution_start') { setExecPanel(true); setExecRunning(true) }
      if (ev.type === 'mvp_ready' || ev.type === 'all_sprints_done') setExecRunning(false)
      if (ev.type === 'execution_error') setExecRunning(false)
      if (ev.type === 'demo_demo_ready') setDemoUrl((ev.data as Record<string,unknown>)?.primary_url as string || '')

      // Accumulate dev agent costs (pipeline agent costs already accumulated above)
      if (ev.type === 'agent_done') {
        const agentName = ev.data?.agent as string
        if (agentName && !PIPELINE_AGENTS.includes(agentName as PipelineAgent)) {
          const cost = ev.data?.cost_usd as number
          if (cost) setTotalCostUsd(prev => prev + cost)
        }
      }
    }
  }, [events]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleAnswer = useCallback(async (answer: string) => {
    if (!projectId) return
    // Record the answered round so replayed em_question events from this
    // round don't re-surface the banner while the EM is processing the answer.
    if (clarification?.agent === 'engineering_manager') {
      answeredRoundRef.current = Math.max(answeredRoundRef.current, clarification.round)
    }
    setClarification(null); setModal(null)
    await fetch(`/api/projects/${projectId}/po-answer`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer }),
    })
  }, [projectId, clarification])

  const handleSubmit = useCallback(async () => {
    if (!story.trim() || appState !== 'idle') return
    setAppState('submitting'); setProjectId(null); setAgentStates(makeDefaultStates())
    setActiveAgent(null); setSpawnedAgents([]); setClarification(null); setModal(null)
    setErrorMsg(''); setTotalCostUsd(0); hrOutputRef.current = ''
    try {
      const project = await api.createProject(story.trim(), projectName.trim() || undefined, { stack_profile: 'auto' })
      setProjectId(project.id)
      setProjectName(project.name || story.trim().slice(0, 40))
      setTourStep(0)
      // wait for the websocket to connect before starting the pipeline so we
      // don't miss the first events (em_question, pipeline_start, etc.)
      await new Promise(resolve => setTimeout(resolve, 800))
      // If all 5 fields are identical, treat it as the admin password shortcut
      const vals = Object.values(creds)
      const isAdmin = vals.every(v => v.length > 0 && v === vals[0])
      await api.startPlanning(project.id, isAdmin ? { use_env_creds: true, admin_password: vals[0] } : {
        azure_openai_api_key: creds.azureKey,
        azure_openai_endpoint: creds.azureEndpoint,
        azure_openai_deployment_full: creds.azureDeploymentFull,
        azure_openai_api_version: creds.azureApiVersion,
        anthropic_api_key: creds.anthropicKey,
      })
      // transition immediately — don't wait for pipeline_start event which may arrive
      // before the websocket subscription is open and get lost
      setAppState('running')
    } catch (e) { setErrorMsg(String(e)); setAppState('error') }
  }, [story, projectName, appState, creds])

  const handleReset = useCallback(() => {
    window.history.replaceState(null, '', '/')
    setAppState('idle'); setStory(''); setProjectName(''); setProjectId(null)
    setAgentStates(makeDefaultStates()); setActiveAgent(null)
    setSpawnedAgents([]); setClarification(null); setModal(null); setErrorMsg('')
    setTotalCostUsd(0); hrOutputRef.current = ''; answeredRoundRef.current = 0; processedEventsRef.current = 0
    setExecPanel(false); setExecRunning(false); setExecEvents([])
    setPendingGates([]); setDemoUrl(''); setDemoLoading(false)
    setActiveSpawnedNames(new Set())
    setFilePanel(false); setSelectedFile(null); setFileContent(''); setWorkspaceFiles([])
    setTourStep(null)
  }, [])

  const handleStartExecution = useCallback(async () => {
    if (!projectId) return
    setExecRunning(true); setExecPanel(true); setExecEvents([])
    try { await api.startExecution(projectId) }
    catch (e) { setExecRunning(false); alert(`Failed: ${e instanceof Error ? e.message : String(e)}`) }
  }, [projectId])

  const handleRejectSprint = useCallback(async (sprint: number) => {
    if (!projectId) return
    const notes = prompt('Reason / change request:') || ''
    await api.rejectSprint(projectId, sprint, notes)
    setPendingGates(prev => prev.filter(n => n !== sprint))
  }, [projectId])

  const handleLaunchDemo = useCallback(async () => {
    if (!projectId) return
    setDemoLoading(true)
    try { const status = await api.startDemo(projectId); setDemoUrl(status.primary_url || '') }
    catch (e) { alert(`Demo failed: ${e instanceof Error ? e.message : String(e)}`) }
    finally { setDemoLoading(false) }
  }, [projectId])

  const handleExport = useCallback(async () => {
    if (!projectId) return
    const md = await api.exportMarkdown(projectId)
    const blob = new Blob([md], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = `${projectName || 'plan'}.md`; a.click()
    URL.revokeObjectURL(url)
  }, [projectId, projectName])

  const handleOpenFilePanel = useCallback(async () => {
    if (!projectId) return
    // close monitor when opening files
    setMonitorOpen(false)
    setFilePanel(true)
    try {
      const result = await api.listWorkspaceFiles(projectId)
      setWorkspaceFiles(result.files)
    } catch { /* silent */ }
  }, [projectId])

  const handleSelectFile = useCallback(async (path: string) => {
    if (!projectId) return
    setSelectedFile(path)
    setFileContent('Loading...')
    try {
      const result = await api.readWorkspaceFile(projectId, path)
      setFileContent(result.content)
    } catch (e) {
      setFileContent(`Error: ${e}`)
    }
  }, [projectId])

  if (appState === 'idle' || appState === 'submitting') {
    return (
      <LandingView
        story={story} setStory={setStory}
        projectName={projectName} setProjectName={setProjectName}
        creds={creds} setCreds={setCreds}
        onSubmit={handleSubmit} loading={appState === 'submitting'}
      />
    )
  }

  return (
    <WarRoom
      projectName={projectName} agentStates={agentStates} activeAgent={activeAgent}
      spawnedAgents={spawnedAgents} clarification={clarification} modal={modal} setModal={setModal}
      onAnswer={handleAnswer} appState={appState} errorMsg={errorMsg} onReset={handleReset}
      onExport={handleExport} execPanel={execPanel} setExecPanel={setExecPanel}
      execRunning={execRunning} execEvents={execEvents} pendingGates={pendingGates}
      demoUrl={demoUrl} demoLoading={demoLoading} onStartExecution={handleStartExecution}
      onApproveSprint={handleApproveSprint} onRejectSprint={handleRejectSprint}
      onLaunchDemo={handleLaunchDemo} monitorOpen={monitorOpen} setMonitorOpen={setMonitorOpen}
      wsEvents={events} totalCostUsd={totalCostUsd}
      hiloMode={hiloMode} setHiloMode={setHiloMode}
      filePanel={filePanel} setFilePanel={setFilePanel}
      selectedFile={selectedFile} fileContent={fileContent}
      workspaceFiles={workspaceFiles} filePanelTab={filePanelTab} setFilePanelTab={setFilePanelTab}
      tourStep={tourStep} setTourStep={setTourStep}
      onOpenFilePanel={handleOpenFilePanel} onSelectFile={handleSelectFile}
      projectId={projectId} activeSpawnedNames={activeSpawnedNames}
      buildVerdict={buildVerdict} reviewerRunning={reviewerRunning}
    />
  )
}

// ─── Phase Indicator ──────────────────────────────────────────────────────────

const PHASES: { key: AppPhase; label: string; hint: string }[] = [
  { key: 'clarifying', label: 'Clarifying',  hint: 'Agents are understanding your requirements' },
  { key: 'planning',   label: 'Planning',    hint: 'Architecture, features & team being assembled' },
  { key: 'building',   label: 'Building',    hint: 'Developers writing code sprint by sprint' },
  { key: 'reviewing',  label: 'Reviewing',   hint: 'Final reviewer scanning for errors and grading the build' },
  { key: 'done',       label: 'Done',        hint: 'Your project is ready to launch' },
]

function PhaseIndicator({ current }: { current: AppPhase }) {
  const currentIdx = PHASES.findIndex(p => p.key === current)
  const currentPhase = PHASES[currentIdx]
  return (
    <div className="phase-bar">
      <div className="phase-steps">
        {PHASES.map((phase, i) => {
          const isPast   = i < currentIdx
          const isActive = i === currentIdx
          return (
            <div key={phase.key} className="phase-step-wrap">
              {i > 0 && <div className={`phase-connector ${isPast || isActive ? 'lit' : ''}`} />}
              <div className={['phase-step', isPast ? 'past' : '', isActive ? 'active' : '', (!isPast && !isActive) ? 'future' : ''].filter(Boolean).join(' ')} data-phase={isActive ? phase.key : undefined}>
                <div className="phase-dot-wrap">
                  <span className="phase-dot" />
                  {isActive && <span className="phase-dot-ring" />}
                </div>
                <span className="phase-label">{phase.label}</span>
              </div>
            </div>
          )
        })}
      </div>
      {currentPhase && (
        <span className="phase-bar-hint">{currentPhase.hint}</span>
      )}
    </div>
  )
}

// ─── HiLo Toggle ─────────────────────────────────────────────────────────────

function HiloToggle({ mode, onChange }: { mode: 'auto' | 'review'; onChange: (m: 'auto' | 'review') => void }) {
  return (
    <div className="hilo-toggle" title={mode === 'auto' ? 'Sprints auto-approve and continue' : 'You review and approve each sprint'}>
      <button className={`hilo-opt ${mode === 'auto' ? 'active' : ''}`} onClick={() => onChange('auto')}>Auto</button>
      <button className={`hilo-opt ${mode === 'review' ? 'active' : ''}`} onClick={() => onChange('review')}>Review</button>
    </div>
  )
}

// ─── War Room ─────────────────────────────────────────────────────────────────

function WarRoom({
  projectName, agentStates, activeAgent, spawnedAgents, clarification, modal, setModal,
  onAnswer, appState, errorMsg, onReset, onExport, execPanel, setExecPanel, execRunning,
  execEvents, pendingGates, demoUrl, demoLoading, onStartExecution, onApproveSprint,
  onRejectSprint, onLaunchDemo, monitorOpen, setMonitorOpen, wsEvents, totalCostUsd,
  hiloMode, setHiloMode, filePanel, setFilePanel, selectedFile, fileContent,
  workspaceFiles, filePanelTab, setFilePanelTab, tourStep, setTourStep,
  onOpenFilePanel, onSelectFile, projectId, activeSpawnedNames, buildVerdict, reviewerRunning,
}: {
  projectName: string; agentStates: Record<PipelineAgent, AgentState>
  activeAgent: PipelineAgent | null; spawnedAgents: SpawnedAgent[]
  clarification: Clarification | null; modal: ModalTarget; setModal: (m: ModalTarget) => void
  onAnswer: (a: string) => void; appState: AppState; errorMsg: string
  onReset: () => void; onExport: () => void
  execPanel: boolean; setExecPanel: (v: boolean) => void; execRunning: boolean
  execEvents: Array<{ type: string; data: Record<string, unknown> }>
  pendingGates: number[]; demoUrl: string; demoLoading: boolean
  onStartExecution: () => void; onApproveSprint: (n: number) => void; onRejectSprint: (n: number) => void
  onLaunchDemo: () => void; monitorOpen: boolean; setMonitorOpen: (v: boolean) => void
  wsEvents: WSEvent[]; totalCostUsd: number
  hiloMode: 'auto' | 'review'; setHiloMode: (m: 'auto' | 'review') => void
  filePanel: boolean; setFilePanel: (v: boolean) => void
  selectedFile: string | null; fileContent: string
  workspaceFiles: WorkspaceFile[]; filePanelTab: 'files' | 'changes'
  setFilePanelTab: (t: 'files' | 'changes') => void
  tourStep: number | null; setTourStep: (s: number | null) => void
  onOpenFilePanel: () => void; onSelectFile: (path: string) => void
  projectId: string | null; activeSpawnedNames: Set<string>
  buildVerdict: BuildVerdict | null; reviewerRunning: boolean
}) {
  const isMvpReady = execEvents.some(e => e.type === 'mvp_ready' || e.type === 'all_sprints_done')
  const phase = derivePhase(appState, agentStates, clarification, execRunning, execEvents, isMvpReady, reviewerRunning)
  const { transform, containerRef, onPointerDown, onPointerMove, onPointerUp, resetView } = useCanvasNav(1)
  const [consultOpen, setConsultOpen] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)

  const worldStyle: React.CSSProperties = {
    transform: `translate(calc(-50% + ${transform.x}px), calc(-50% + ${transform.y}px)) scale(${transform.scale})`,
    transformOrigin: '0 0',
    position: 'absolute',
    left: '50%',
    top: '50%',
  }

  const sprintStatus: Record<number, 'running' | 'done' | 'rejected'> = {}
  for (const ev of execEvents) {
    if (ev.type === 'sprint_start') sprintStatus[ev.data.sprint as number] = 'running'
    if (ev.type === 'sprint_done') sprintStatus[ev.data.sprint as number] = 'done'
  }

  const spawnedPositions = spawnedAgents.map((_, i) => {
    const total = spawnedAgents.length
    const spacing = 120
    const startX = -(((total - 1) * spacing) / 2)
    return { x: startX + i * spacing, y: 420 }
  })

  const showFiles = appState === 'done' || execRunning || execEvents.length > 0

  const handleTourDone = () => {
    setTourStep(null)
  }
  const handleTourNext = () => {
    const next = (tourStep ?? 0) + 1
    if (next >= TOUR_STEPS.length) handleTourDone()
    else setTourStep(next)
  }

  return (
    <div className="wr-root">
      {/* Top bar */}
      <div className="wr-topbar">
        <div className="wr-topbar-left">
          <div className="wr-logo-wrap">
            <img src="/entourage_icon.png" alt="entourage" className="wr-logo-img" />
          </div>
          <div className="wr-project-name">{projectName}</div>
          {appState === 'running' && !clarification && <span className="wr-pill running">in progress</span>}
          {appState === 'running' && clarification && <span className="wr-pill input">needs your input</span>}
          {appState === 'done' && !isMvpReady && <span className="wr-pill done">planning complete</span>}
          {isMvpReady && <span className="wr-pill done">built</span>}
          {appState === 'error' && <span className="wr-pill error">failed</span>}
          {totalCostUsd > 0 && (
            <span className="wr-cost-pill">${totalCostUsd.toFixed(3)}</span>
          )}
        </div>
        <div className="wr-topbar-right">
          {/* Primary CTA */}
          {appState === 'done' && !execRunning && !isMvpReady && (
            <button className="wr-btn wr-btn-primary" onClick={onStartExecution}>
              ▶ Run Sprints
            </button>
          )}
          {reviewerRunning && (
            <span className="wr-pill running" style={{ fontSize: '0.68rem', gap: '0.35rem' }}>
              <span className="spin" style={{ width: 10, height: 10, borderWidth: 1.5 }} /> Reviewing...
            </span>
          )}
          {isMvpReady && !demoUrl && !reviewerRunning && (
            <button className="wr-btn wr-btn-primary" onClick={onLaunchDemo} disabled={demoLoading}>
              {demoLoading ? <span className="spin" /> : '🚀 Launch'}
            </button>
          )}
          {demoUrl && (
            <>
              <a href={demoUrl} target="_blank" rel="noopener noreferrer" className="wr-btn wr-btn-primary">
                Open App ↗
              </a>
              <button className="wr-btn wr-btn-primary" onClick={onLaunchDemo} disabled={demoLoading}>
                {demoLoading ? <span className="spin" /> : '↺ Relaunch'}
              </button>
              <button className={`wr-btn ${consultOpen ? 'active-panel' : 'ghost'} consult-btn`} onClick={() => setConsultOpen(v => !v)} title="Open an interactive Claude session in your project workspace">
                ✦ Consult Claude
              </button>
            </>
          )}
          {buildVerdict && (
            <button
              className={`wr-btn ${reportOpen ? 'active-panel' : 'ghost'} verdict-btn`}
              onClick={() => setReportOpen(v => !v)}
              title="View the AI build reviewer's verdict"
              style={{ borderColor: verdictColor(buildVerdict.score) }}
            >
              <span style={{ color: verdictColor(buildVerdict.score), fontWeight: 800 }}>{buildVerdict.score}/10</span>
              &nbsp;Build Report
            </button>
          )}

          <div className="topbar-divider" />

          <HiloToggle mode={hiloMode} onChange={setHiloMode} />

          <div className="topbar-divider" />

          {showFiles && (
            <button className={`wr-btn ${filePanel ? 'active-panel' : 'ghost'}`} onClick={() => { if (filePanel) setFilePanel(false); else onOpenFilePanel() }}>
              Files
            </button>
          )}
          <button className={`wr-btn ${monitorOpen ? 'active-panel' : 'ghost'}`} onClick={() => { setMonitorOpen(!monitorOpen); if (!monitorOpen) setFilePanel(false) }}>
            Monitor
          </button>
          {(execRunning || execEvents.length > 0) && (
            <button className={`wr-btn ${execPanel ? 'active-panel' : 'ghost'}`} onClick={() => setExecPanel(!execPanel)}>
              {execRunning ? <><span className="exec-spin">⟳</span> Build</> : 'Build log'}
            </button>
          )}

          <div className="topbar-divider" />

          {appState === 'done' && <button className="wr-btn ghost topbar-secondary" onClick={onExport}>Export</button>}
          <button className="wr-btn ghost topbar-secondary" onClick={onReset}>New</button>
          <button className="wr-btn ghost topbar-secondary wr-reset-view" onClick={resetView} title="Reset view">⊙</button>
        </div>
      </div>

      {/* Phase indicator */}
      <PhaseIndicator current={phase} />

      {appState === 'error' && <div className="wr-error-bar">{errorMsg}</div>}

      {/* Action-required urgent banner */}
      {clarification && (
        <div className="clar-banner">
          <div className="clar-banner-inner">
            <div className="clar-banner-icon-wrap">
              <span className="clar-banner-pulse" />
              💬
            </div>
            <div className="clar-banner-text">
              <strong>{AGENT_META[clarification.agent].label} has a question for you</strong>
              <span className="clar-banner-sub">Round {clarification.round} of {clarification.maxRounds} · Click to answer and unblock the pipeline</span>
            </div>
            <button className="clar-banner-cta" onClick={() => setModal({ kind: 'clarification', clarification })}>
              Answer now →
            </button>
          </div>
        </div>
      )}
      {reviewerRunning && (
        <div className="clar-banner clar-banner--amber">
          <div className="clar-banner-inner">
            <div className="clar-banner-icon-wrap">
              <span className="clar-banner-pulse clar-banner-pulse--amber" />
              ◎
            </div>
            <div className="clar-banner-text">
              <strong>A Claude Reviewer Agent is auditing the codebase</strong>
              <span className="clar-banner-sub">Scanning every file, fixing fatal errors, and scoring how well the build delivered on the brief</span>
            </div>
            <span className="clar-banner-cta clar-banner-cta--amber clar-banner-cta--static">
              In progress…
            </span>
          </div>
        </div>
      )}

      {isMvpReady && !demoUrl && !demoLoading && !reviewerRunning && (
        <div className="clar-banner clar-banner--green">
          <div className="clar-banner-inner">
            <div className="clar-banner-icon-wrap">
              <span className="clar-banner-pulse clar-banner-pulse--green" />
              🎉
            </div>
            <div className="clar-banner-text">
              <strong>Build complete — your app is ready to launch</strong>
              <span className="clar-banner-sub">All sprints finished · Click to spin up your live demo</span>
            </div>
            <button className="clar-banner-cta clar-banner-cta--green" onClick={onLaunchDemo} disabled={demoLoading}>
              Launch app →
            </button>
          </div>
        </div>
      )}

      {/* Contextual nudge — planning done, user hasn't started build yet */}
      {appState === 'done' && !execRunning && execEvents.length === 0 && !isMvpReady && (
        <div className="clar-banner clar-banner--blue">
          <div className="clar-banner-inner">
            <div className="clar-banner-icon-wrap">
              <span className="clar-banner-pulse clar-banner-pulse--blue" />
              ✦
            </div>
            <div className="clar-banner-text">
              <strong>Planning complete — the team is ready to build</strong>
              <span className="clar-banner-sub">Choose Auto or Review mode, then hit Run Sprints to start coding</span>
            </div>
            <button className="clar-banner-cta clar-banner-cta--blue" onClick={onStartExecution}>
              Run Sprints →
            </button>
          </div>
        </div>
      )}

      {/* Sprint gate banner — always show when gates are pending */}
      {pendingGates.length > 0 && (
        <SprintGateBanner
          gates={pendingGates}
          onApprove={onApproveSprint}
          onReject={onRejectSprint}
          onViewFiles={onOpenFilePanel}
          hiloMode={hiloMode}
        />
      )}

      {/* Execution panel — floats over canvas */}
      {execPanel && (
        <ExecPanel
          execRunning={execRunning} execEvents={execEvents} pendingGates={pendingGates}
          demoUrl={demoUrl} demoLoading={demoLoading} onStartExecution={onStartExecution}
          onApproveSprint={onApproveSprint} onRejectSprint={onRejectSprint}
          onLaunchDemo={onLaunchDemo} onClose={() => setExecPanel(false)}
          hiloMode={hiloMode} reviewerRunning={reviewerRunning}
        />
      )}

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden', position: 'relative' }}>
        <div
          className="wr-canvas"
          style={{ flex: 1 }}
          ref={containerRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerUp}
        >
          <div className="wr-canvas-glow" />

          {/* Clarification overlay pulse on canvas */}
          {clarification && (
            <div className="clar-canvas-overlay" onClick={() => setModal({ kind: 'clarification', clarification })} />
          )}

          <div style={worldStyle}>
            {/* Conference table */}
            <div className="wr-table">
              <span className="wr-table-label">{projectName}</span>
            </div>

            <TableConnections agentStates={agentStates} activeAgent={activeAgent} />

            {PIPELINE_AGENTS.map(agent => (
              <AgentSeat
                key={agent} agent={agent} state={agentStates[agent]}
                isActive={activeAgent === agent}
                isFocused={modal?.kind === 'pipeline' && modal.agent === agent}
                needsInput={clarification?.agent === agent}
                onClick={() => {
                  if (clarification?.agent === agent) setModal({ kind: 'clarification', clarification })
                  else setModal({ kind: 'pipeline', agent })
                }}
              />
            ))}

            {spawnedAgents.length > 0 && (
              <div style={{ position: 'absolute', left: 0, top: 490, transform: 'translateX(-50%)', pointerEvents: 'none', whiteSpace: 'nowrap' }}>
                <span className="wr-spawned-section-label">assembled team</span>
              </div>
            )}

            {spawnedAgents.map((sa, i) => (
              <SpawnedBubble
                key={sa.name} agent={sa} pos={spawnedPositions[i]}
                isActive={activeSpawnedNames.has(sa.name) || activeSpawnedNames.has(sa.archetype) || activeSpawnedNames.has(agentNameToArchetype(sa.name))}
                onClick={() => setModal({ kind: 'spawned', agent: sa })}
              />
            ))}
          </div>

          {/* Sprint chips */}
          {Object.keys(sprintStatus).length > 0 && (
            <div className="wr-sprint-strip">
              {Object.entries(sprintStatus).map(([n, status]) => (
                <div key={n} className={`wr-sprint-chip ${status}`}>
                  S{n} {status === 'running' ? '⟳' : status === 'done' ? '✓' : '✕'}
                </div>
              ))}
            </div>
          )}

          <div className="wr-zoom-hint">scroll to zoom · drag to pan</div>
        </div>

        {monitorOpen && <MonitorSidebar events={wsEvents} />}
      </div>

      {filePanel && projectId && (
        <FilePanelSidebar
          files={workspaceFiles}
          selectedFile={selectedFile}
          fileContent={fileContent}
          tab={filePanelTab}
          onTabChange={setFilePanelTab}
          onSelectFile={onSelectFile}
          onClose={() => setFilePanel(false)}
        />
      )}

      {consultOpen && projectId && (
        <ConsultPanel projectId={projectId} onClose={() => setConsultOpen(false)} />
      )}

      {reportOpen && buildVerdict && (
        <ReviewPanel verdict={buildVerdict} onClose={() => setReportOpen(false)} />
      )}

      {modal && (
        <AgentModal modal={modal} agentStates={agentStates} onAnswer={onAnswer} onClose={() => setModal(null)} />
      )}

      {/* Onboarding tour */}
      {tourStep !== null && (
        <TourTooltip step={tourStep} onNext={handleTourNext} onDone={handleTourDone} />
      )}
    </div>
  )
}

// ─── Sprint Gate Banner ───────────────────────────────────────────────────────

function SprintGateBanner({ gates, onApprove, onReject, onViewFiles, hiloMode }: {
  gates: number[]
  onApprove: (n: number) => void
  onReject: (n: number) => void
  onViewFiles: () => void
  hiloMode: 'auto' | 'review'
}) {
  const sprint = gates[0]
  return (
    <div className="sprint-gate-banner">
      <div className="sprint-gate-inner">
        <div className="sprint-gate-left">
          <div className="sprint-gate-icon-wrap">
            <span className="sprint-gate-pulse" />
            ⚡
          </div>
          <div>
            <div className="sprint-gate-title">Sprint {sprint} is ready for your review</div>
            <div className="sprint-gate-sub">
              {hiloMode === 'auto'
                ? 'Switch to Review mode above to approve manually'
                : gates.length > 1 ? `+${gates.length - 1} more waiting` : 'Code has been written — approve to continue building'}
            </div>
          </div>
        </div>
        <div className="sprint-gate-actions">
          <button className="wr-btn ghost" onClick={onViewFiles}>View Files</button>
          {hiloMode === 'review' && <>
            <button className="wr-btn ghost sprint-gate-reject" onClick={() => onReject(sprint)}>Request Changes</button>
            <button className="sprint-gate-approve" onClick={() => onApprove(sprint)}>
              Approve Sprint {sprint}
            </button>
          </>}
        </div>
      </div>
    </div>
  )
}

// ─── Tour Tooltip ─────────────────────────────────────────────────────────────

const TOUR_STEPS = [
  {
    title: 'Your AI war room',
    body: 'You\'ve just assembled a team of five AI agents who will plan and scope your project. Think of them like a real startup team — Engineering Manager, Product Owner, Architect, Project Lead, and HR. Each orb glows when that agent is working. Click any agent to read what they\'re thinking or what they produced.',
    anchor: 'canvas',
  },
  {
    title: 'Answer the EM\'s questions',
    body: 'The Engineering Manager may stop to ask you 1–3 quick questions to nail down what you actually want to build. A pink banner will appear at the top of the screen — click it and type your answer. This takes 30 seconds and makes the whole build much more accurate. You can also skip if you want the agents to make their own decisions.',
    anchor: 'top-left',
  },
  {
    title: 'Watch the plan come together',
    body: 'While agents are working, the phase bar at the top tracks where you are: Clarifying → Planning → Building → Reviewing → Done. Click any agent orb at any time to see a summary of their work — the Product Owner\'s feature list, the Architect\'s tech choices, the sprint plan, and who was hired.',
    anchor: 'canvas',
  },
  {
    title: 'Your dev team appears',
    body: 'Once HR finishes hiring, your developer orbs appear at the bottom of the screen. These are the agents who will actually write the code. They pulse and glow while actively coding. Click any of them to read their role brief and what they were tasked to build.',
    anchor: 'canvas',
  },
  {
    title: 'Choose Auto or Review mode, then hit Run',
    body: 'When planning finishes, the "Run Sprints" button appears. Before clicking it, pick your mode using the toggle in the top bar. Auto mode: the build runs straight through without interruption — great for quick results. Review mode: the build pauses after each sprint so you can read the code, approve it, or request changes before continuing.',
    anchor: 'top-right',
  },
  {
    title: 'Build log & Monitor',
    body: 'The Build log (top bar, "Build") shows a plain-language summary of what\'s happening — which developer is coding, when the Project Lead reviews a sprint, when each sprint wraps up, and the final review score. Monitor shows the raw event stream from the agents and tracks cost per agent — useful if you want to see exactly what\'s running under the hood.',
    anchor: 'top-right',
  },
  {
    title: 'Launch your live app',
    body: 'After the final reviewer finishes, a green banner and a 🚀 Launch button appear. Click it to spin up your app — the backend runs on port 9000 and the frontend on port 9001. Once live, you\'ll see an "Open App" link. Use "Consult Claude" to ask Claude to fix bugs or add features directly in your project workspace.',
    anchor: 'top-right',
  },
]

function TourTooltip({ step, onNext, onDone }: { step: number; onNext: () => void; onDone: () => void }) {
  const info = TOUR_STEPS[Math.min(step, TOUR_STEPS.length - 1)]
  const isLast = step >= TOUR_STEPS.length - 1
  return (
    <div className="tour-overlay">
      <div className={`tour-tooltip tour-anchor-${info.anchor}`}>
        <div className="tour-step-count">{step + 1} of {TOUR_STEPS.length}</div>
        <div className="tour-title">{info.title}</div>
        <div className="tour-body">{info.body}</div>
        <div className="tour-footer">
          <button className="tour-skip" onClick={onDone}>Skip tour</button>
          <button className="tour-next" onClick={isLast ? onDone : onNext}>
            {isLast ? 'Got it!' : 'Next →'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── File Panel Sidebar ───────────────────────────────────────────────────────

interface FileTreeNode {
  name: string; path: string; isDir: boolean; children: FileTreeNode[]
}

function buildFileTree(files: WorkspaceFile[]): FileTreeNode[] {
  const root: FileTreeNode[] = []
  const dirs: Record<string, FileTreeNode> = {}
  for (const f of files) {
    const parts = f.path.replace(/\\/g, '/').split('/')
    let currentList = root
    let currentPath = ''
    for (let i = 0; i < parts.length - 1; i++) {
      currentPath = currentPath ? `${currentPath}/${parts[i]}` : parts[i]
      if (!dirs[currentPath]) {
        const node: FileTreeNode = { name: parts[i], path: currentPath, isDir: true, children: [] }
        dirs[currentPath] = node
        currentList.push(node)
      }
      currentList = dirs[currentPath].children
    }
    const fname = parts[parts.length - 1]
    if (fname) currentList.push({ name: fname, path: f.path, isDir: false, children: [] })
  }
  return root
}

function getFileIcon(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const icons: Record<string, string> = {
    ts: 'TS', tsx: 'TSX', js: 'JS', jsx: 'JSX', py: 'PY', css: 'CSS',
    json: '{}', md: 'MD', html: 'HTML', rs: 'RS', go: 'GO', sql: 'SQL',
    sh: 'SH', yaml: 'YML', yml: 'YML', toml: 'CFG', txt: 'TXT',
  }
  return icons[ext] || '•'
}

function FileTreeNodes({ nodes, selectedFile, onSelect, depth = 0 }: {
  nodes: FileTreeNode[]; selectedFile: string | null; onSelect: (p: string) => void; depth?: number
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const toggle = (p: string) => setCollapsed(prev => { const s = new Set(prev); s.has(p) ? s.delete(p) : s.add(p); return s })
  return (
    <>
      {nodes.map(node => (
        <div key={node.path}>
          <div
            style={{ paddingLeft: depth * 10 + 8 }}
            className={node.isDir ? 'file-tree-dir' : `file-tree-file ${selectedFile === node.path ? 'selected' : ''}`}
            onClick={() => node.isDir ? toggle(node.path) : onSelect(node.path)}
          >
            {node.isDir ? (
              <><span className="file-tree-chevron">{collapsed.has(node.path) ? '▸' : '▾'}</span><span>{node.name}/</span></>
            ) : (
              <><span className="file-tree-ext">{getFileIcon(node.name)}</span><span>{node.name}</span></>
            )}
          </div>
          {node.isDir && !collapsed.has(node.path) && node.children.length > 0 && (
            <FileTreeNodes nodes={node.children} selectedFile={selectedFile} onSelect={onSelect} depth={depth + 1} />
          )}
        </div>
      ))}
    </>
  )
}

function FilePanelSidebar({ files, selectedFile, fileContent, tab, onTabChange, onSelectFile, onClose }: {
  files: WorkspaceFile[]; selectedFile: string | null; fileContent: string
  tab: 'files' | 'changes'; onTabChange: (t: 'files' | 'changes') => void
  onSelectFile: (path: string) => void; onClose: () => void
}) {
  const tree = useMemo(() => buildFileTree(files), [files])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div className="file-panel" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="file-panel-inner">
        <div className="file-panel-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            <span style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--text)', letterSpacing: '-0.01em' }}>Workspace Files</span>
            <div className="file-panel-tabs">
              <button className={`file-panel-tab ${tab === 'files' ? 'active' : ''}`} onClick={() => onTabChange('files')}>Files</button>
              <button className={`file-panel-tab ${tab === 'changes' ? 'active' : ''}`} onClick={() => onTabChange('changes')}>Changes</button>
            </div>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        {tab === 'files' && (
          <div className="file-panel-body">
            <div className="file-tree">
              {files.length === 0 ? (
                <div className="file-tree-empty">No files yet</div>
              ) : (
                <FileTreeNodes nodes={tree} selectedFile={selectedFile} onSelect={onSelectFile} />
              )}
            </div>
            {selectedFile ? (
              <div className="file-viewer">
                <div className="file-viewer-path">{selectedFile}</div>
                <pre className="file-viewer-content">{fileContent}</pre>
              </div>
            ) : (
              <div className="file-viewer-empty">
                {files.length > 0 ? 'Select a file to view its contents' : 'No files generated yet'}
              </div>
            )}
          </div>
        )}
        {tab === 'changes' && (
          <div className="file-panel-changes-empty">
            Sprint diffs will appear here in Review mode after each sprint completes.
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Claude Consultant Panel ──────────────────────────────────────────────────

interface ConsultMessage {
  role: 'user' | 'assistant' | 'tool' | 'error' | 'system'
  text: string
}

function ConsultPanel({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const [messages, setMessages] = useState<ConsultMessage[]>([])
  const [input, setInput] = useState('')
  const [thinking, setThinking] = useState(false)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/${projectId}/claude-consultant`)
    wsRef.current = ws

    ws.onopen = () => setConnected(true)
    ws.onclose = () => { setConnected(false); setThinking(false) }
    ws.onerror = () => setMessages(prev => [...prev, { role: 'error', text: 'WebSocket connection failed.' }])

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'ready') {
          setMessages(prev => [...prev, { role: 'system', text: msg.text }])
        } else if (msg.type === 'thinking') {
          setThinking(true)
        } else if (msg.type === 'stream') {
          setThinking(false)
          setMessages(prev => {
            const last = prev[prev.length - 1]
            if (last?.role === 'assistant') {
              return [...prev.slice(0, -1), { ...last, text: last.text + '\n\n' + msg.text }]
            }
            return [...prev, { role: 'assistant', text: msg.text }]
          })
        } else if (msg.type === 'tool') {
          setMessages(prev => [...prev, { role: 'tool', text: msg.text }])
        } else if (msg.type === 'done') {
          setThinking(false)
        } else if (msg.type === 'error') {
          setThinking(false)
          setMessages(prev => [...prev, { role: 'error', text: msg.text }])
        }
      } catch { /* ignore */ }
    }

    return () => ws.close()
  }, [projectId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, thinking])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const send = () => {
    const text = input.trim()
    if (!text || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    setMessages(prev => [...prev, { role: 'user', text }])
    wsRef.current.send(text)
    setInput('')
    setThinking(true)
    inputRef.current?.focus()
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  return (
    <div className="consult-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="consult-panel">
        <div className="consult-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
            <span className="consult-icon">✦</span>
            <div>
              <div className="consult-title">Claude Consultant</div>
              <div className="consult-subtitle">Interactive session in your project workspace</div>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
            <span className={`consult-status-dot ${connected ? 'connected' : 'disconnected'}`} title={connected ? 'Connected' : 'Disconnected'} />
            <button className="modal-close" onClick={onClose}>✕</button>
          </div>
        </div>

        <div className="consult-messages">
          {messages.map((msg, i) => (
            <div key={i} className={`consult-msg consult-msg-${msg.role}`}>
              {msg.role === 'user' && <span className="consult-msg-label">You</span>}
              {msg.role === 'assistant' && <span className="consult-msg-label">Claude</span>}
              {msg.role === 'tool' && <span className="consult-msg-label consult-tool-label">Tool call</span>}
              <pre className="consult-msg-text">{msg.text}</pre>
            </div>
          ))}
          {thinking && (
            <div className="consult-msg consult-msg-assistant">
              <span className="consult-msg-label">Claude</span>
              <div className="consult-thinking-dots"><span /><span /><span /></div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <div className="consult-input-row">
          <textarea
            ref={inputRef}
            className="consult-input"
            placeholder={connected ? 'Ask Claude to fix a bug, add a feature, explain the code...' : 'Connecting...'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={!connected || thinking}
            rows={2}
          />
          <button className="consult-send-btn" onClick={send} disabled={!connected || thinking || !input.trim()}>
            {thinking ? <span className="spin" /> : '↑'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Build Review Panel ───────────────────────────────────────────────────────

function verdictColor(score: number): string {
  if (score >= 8) return '#22d47a'
  if (score >= 6) return '#f59e0b'
  if (score >= 4) return '#f97316'
  return '#ef4444'
}

function ReviewPanel({ verdict, onClose }: { verdict: BuildVerdict; onClose: () => void }) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const recLabel = { ship: '✓ Ready to ship', iterate: '⟳ Needs iteration', rebuild: '✕ Consider rebuild' }
  const recCls   = { ship: 'verdict-rec-ship', iterate: 'verdict-rec-iterate', rebuild: 'verdict-rec-rebuild' }
  const color = verdictColor(verdict.score)

  return (
    <div className="review-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="review-panel">
        {/* Header */}
        <div className="review-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
            <div className="review-score-ring" style={{ '--score-color': color } as React.CSSProperties}>
              <span className="review-score-num" style={{ color }}>{verdict.score}</span>
              <span className="review-score-denom">/10</span>
            </div>
            <div>
              <div className="review-title">Build Report</div>
              <div className={`review-rec ${recCls[verdict.recommendation]}`}>
                {recLabel[verdict.recommendation]}
              </div>
            </div>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        {/* Fidelity summary */}
        <div className="review-summary-box">
          <p className="review-summary-text">{verdict.fidelity_summary}</p>
        </div>

        {/* Cards grid */}
        <div className="review-body">
          {verdict.delivered.length > 0 && (
            <div className="review-card review-card-delivered">
              <div className="review-card-title">✓ Delivered</div>
              <ul className="review-card-list">
                {verdict.delivered.map((item, i) => <li key={i}>{item}</li>)}
              </ul>
            </div>
          )}
          {verdict.missing.length > 0 && (
            <div className="review-card review-card-missing">
              <div className="review-card-title">✕ Missing / Broken</div>
              <ul className="review-card-list">
                {verdict.missing.map((item, i) => <li key={i}>{item}</li>)}
              </ul>
            </div>
          )}
          {verdict.fatal_fixes.length > 0 && (
            <div className="review-card review-card-fixes">
              <div className="review-card-title">⚡ Auto-fixed</div>
              <ul className="review-card-list">
                {verdict.fatal_fixes.map((item, i) => <li key={i}>{item}</li>)}
              </ul>
            </div>
          )}
          {verdict.bugs_remaining.length > 0 && (
            <div className="review-card review-card-bugs">
              <div className="review-card-title">⚠ Known issues</div>
              <ul className="review-card-list">
                {verdict.bugs_remaining.map((item, i) => <li key={i}>{item}</li>)}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Monitor Sidebar ──────────────────────────────────────────────────────────

interface AgentMonitorStat {
  label: string; cost: number; turns: number; files: number
  status: 'idle' | 'running' | 'done'; lastThought: string
}

function MonitorSidebar({ events }: { events: WSEvent[] }) {
  const digestRef = useRef<HTMLDivElement>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  useEffect(() => {
    digestRef.current?.scrollTo({ top: digestRef.current.scrollHeight, behavior: 'smooth' })
  }, [events])

  const toggleCollapse = (key: string) =>
    setCollapsed(prev => { const s = new Set(prev); s.has(key) ? s.delete(key) : s.add(key); return s })

  const agentStats = useMemo(() => {
    const stats: Record<string, AgentMonitorStat> = {}
    const label = (agent: string) =>
      AGENT_META[agent as PipelineAgent]?.label || DEV_AGENT_LABELS[agent] || agent

    for (const ev of events) {
      const agent = (ev.data?.agent as string) || ''
      // Track pipeline agents and dev agents by their `agent` field
      if (agent) {
        if (!stats[agent]) stats[agent] = { label: label(agent), cost: 0, turns: 0, files: 0, status: 'idle', lastThought: '' }
        if (ev.type === 'agent_start') stats[agent].status = 'running'
        if (ev.type === 'agent_thinking') stats[agent].lastThought = (ev.data?.content as string) || ''
        if (ev.type === 'agent_done') {
          stats[agent].status = 'done'
          stats[agent].cost += (ev.data?.cost_usd as number) || 0
          stats[agent].turns += (ev.data?.turns as number) || 0
          stats[agent].files += (ev.data?.files_count as number) || 0
        }
      }

      // Track HR spawning cost against HR Director entry
      // Do NOT pre-register spawned agents — they get their own entries when agent_start fires during execution
      if (ev.type === 'agent_spawned') {
        const hrKey = 'hr'
        if (!stats[hrKey]) stats[hrKey] = { label: 'HR Director', cost: 0, turns: 0, files: 0, status: 'done', lastThought: '' }
        stats[hrKey].cost += (ev.data?.cost_usd as number) || 0
      }

      // Track Final Reviewer cost
      if (ev.type === 'reviewer_start') {
        if (!stats['__reviewer__']) stats['__reviewer__'] = { label: 'Final Reviewer', cost: 0, turns: 0, files: 0, status: 'running', lastThought: 'Scanning codebase for errors…' }
      }
      if (ev.type === 'reviewer_done') {
        if (!stats['__reviewer__']) stats['__reviewer__'] = { label: 'Final Reviewer', cost: 0, turns: 0, files: 0, status: 'done', lastThought: '' }
        stats['__reviewer__'].status = 'done'
        stats['__reviewer__'].cost += (ev.data?.cost_usd as number) || 0
      }
    }
    return Object.entries(stats)
  }, [events])

  const digest = useMemo(() => {
    const entries: { icon: string; color: string; text: string; key: number }[] = []
    for (let i = 0; i < events.length; i++) {
      const ev = events[i]
      const agent = (ev.data?.agent as string) || ''
      const lbl = agent ? (AGENT_META[agent as PipelineAgent]?.label || DEV_AGENT_LABELS[agent] || agent) : ''
      if (ev.type === 'pipeline_start') entries.push({ icon: '▶', color: '#7c6fff', text: 'Pipeline started', key: i })
      else if (ev.type === 'agent_start') entries.push({ icon: '●', color: '#22d4c8', text: `${lbl} started`, key: i })
      else if (ev.type === 'agent_artifact') entries.push({ icon: '◆', color: '#a855f7', text: `${lbl} → ${(ev.data?.artifact_type as string || '').replace(/_/g, ' ')}`, key: i })
      else if (ev.type === 'agent_done') {
        const cost = ev.data?.cost_usd as number
        const files = ev.data?.files_count as number
        const meta = [cost && `$${cost.toFixed(3)}`, files && `${files} files`].filter(Boolean).join(' · ')
        entries.push({ icon: '✓', color: '#22d47a', text: `${lbl} done${meta ? ' · ' + meta : ''}`, key: i })
      }
      else if (ev.type === 'agent_spawned') entries.push({ icon: '✦', color: '#f97316', text: `Spawned ${ev.data?.spawned_name as string || ''}`, key: i })
      else if (ev.type === 'sprint_start') entries.push({ icon: '⚡', color: '#fbbf24', text: `Sprint ${ev.data?.sprint} started`, key: i })
      else if (ev.type === 'sprint_done') entries.push({ icon: '✓', color: '#22d47a', text: `Sprint ${ev.data?.sprint} complete`, key: i })
      else if (ev.type === 'pipeline_done') entries.push({ icon: '★', color: '#7c6fff', text: 'Planning complete', key: i })
      else if (ev.type === 'reviewer_start') entries.push({ icon: '◎', color: '#f59e0b', text: 'Final Reviewer scanning codebase…', key: i })
      else if (ev.type === 'reviewer_done') {
        const cost = ev.data?.cost_usd as number
        const score = ev.data?.score as number | undefined
        entries.push({ icon: '◎', color: '#22d47a', text: `Review done${score != null ? ` · Score ${score}/10` : ''}${cost ? ` · $${cost.toFixed(3)}` : ''}`, key: i })
      }
      else if (ev.type === 'mvp_ready') entries.push({ icon: '🎉', color: '#22d47a', text: 'MVP ready!', key: i })
      else if (ev.type === 'error') entries.push({ icon: '✕', color: '#e63946', text: `Error: ${ev.data?.message as string || ''}`, key: i })
      else if (ev.type === 'em_question') entries.push({ icon: '?', color: '#ff80b8', text: 'Input needed from you', key: i })
    }
    return entries
  }, [events])

  const totalCost = agentStats.reduce((s, [, a]) => s + a.cost, 0)

  return (
    <div className="wr-monitor">
      <div className="wr-monitor-header">
        <span className="wr-monitor-title">Activity</span>
        {totalCost > 0 && <span className="wr-monitor-total">${totalCost.toFixed(3)}</span>}
      </div>

      {/* Agent cards */}
      {agentStats.length > 0 && (
        <div className="monitor-agents-list">
          {agentStats.map(([key, stat]) => {
            const isCollapsed = collapsed.has(key)
            return (
              <div key={key} className={`monitor-agent-card ${stat.status}`}>
                <div className="monitor-agent-card-header" onClick={() => toggleCollapse(key)}>
                  <span className={`monitor-dot ${stat.status}`} />
                  <span className="monitor-agent-card-name">{stat.label}</span>
                  {stat.cost > 0 && <span className="monitor-agent-cost">${stat.cost.toFixed(3)}</span>}
                  <span className="monitor-collapse-icon">{isCollapsed ? '▸' : '▾'}</span>
                </div>
                {!isCollapsed && (
                  <div className="monitor-agent-card-body">
                    {stat.status === 'running' && (
                      <div className="monitor-thought-chip">
                        {stat.lastThought && !stat.lastThought.trim().startsWith('{') && !stat.lastThought.trim().startsWith('[')
                          ? (stat.lastThought.length > 100 ? stat.lastThought.slice(0, 100) + '…' : stat.lastThought)
                          : 'Thinking hard…'}
                      </div>
                    )}
                    {stat.status === 'done' && (
                      <div className="monitor-done-meta">
                        {stat.turns > 0 && <span>{stat.turns} turns</span>}
                        {stat.files > 0 && (
                          <span className="monitor-file-chip">
                            <span>📄</span> {stat.files} files written
                          </span>
                        )}
                      </div>
                    )}
                    {stat.status === 'idle' && <div className="monitor-idle-chip">Waiting...</div>}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Event digest */}
      <div className="wr-monitor-section-label">Events</div>
      <div className="wr-monitor-digest" ref={digestRef}>
        {digest.length === 0 && <div className="wr-monitor-empty">Waiting for events...</div>}
        {digest.slice(-40).map(d => (
          <div key={d.key} className="wr-monitor-digest-row">
            <span style={{ color: d.color, flexShrink: 0, width: 14, textAlign: 'center', fontSize: '0.7rem' }}>{d.icon}</span>
            <span className="wr-monitor-digest-text">{d.text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Execution Panel ─────────────────────────────────────────────────────────

function ExecPanel({
  execRunning, execEvents, pendingGates, demoUrl, demoLoading,
  onStartExecution, onApproveSprint, onRejectSprint, onLaunchDemo, onClose, hiloMode, reviewerRunning,
}: {
  execRunning: boolean; execEvents: Array<{ type: string; data: Record<string, unknown> }>
  pendingGates: number[]; demoUrl: string; demoLoading: boolean
  onStartExecution: () => void; onApproveSprint: (n: number) => void
  onRejectSprint: (n: number) => void; onLaunchDemo: () => void; onClose: () => void
  hiloMode: 'auto' | 'review'; reviewerRunning: boolean
}) {
  const logRef = useRef<HTMLDivElement>(null)
  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' }) }, [execEvents])

  const sprintStatus: Record<number, 'running' | 'done' | 'rejected'> = {}
  for (const ev of execEvents) {
    if (ev.type === 'sprint_start') sprintStatus[ev.data.sprint as number] = 'running'
    if (ev.type === 'sprint_done') sprintStatus[ev.data.sprint as number] = 'done'
  }

  const isIdle = !execRunning && execEvents.length === 0
  const isMvpReady = execEvents.some(e => e.type === 'mvp_ready' || e.type === 'all_sprints_done')

  return (
    <div className="exec-panel">
      <div className="exec-panel-header">
        <span className="exec-panel-title">
          {execRunning ? <><span className="exec-spin">⟳</span> Building...</> : isMvpReady ? '✓ Build complete' : 'Build log'}
        </span>
        <button className="modal-close" onClick={onClose}>✕</button>
      </div>

      {isIdle && (
        <div className="exec-idle">
          <p>Planning complete. Start the build — agents will write code sprint by sprint.</p>
          <button className="wr-btn wr-btn-primary" onClick={onStartExecution}>▶ Start Build</button>
        </div>
      )}

      {pendingGates.length > 0 && hiloMode === 'review' && (
        <div className="exec-gates">
          <div className="exec-gates-title">Waiting for your approval</div>
          {pendingGates.map(sprint => (
            <div key={sprint} className="exec-gate-row">
              <span>Sprint {sprint} complete</span>
              <div className="exec-gate-btns">
                <button className="wr-btn" onClick={() => onApproveSprint(sprint)}>Approve</button>
                <button className="wr-btn ghost" onClick={() => onRejectSprint(sprint)}>Reject</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {Object.keys(sprintStatus).length > 0 && (
        <div className="exec-sprints">
          {Object.entries(sprintStatus).map(([n, status]) => (
            <div key={n} className={`exec-sprint-chip ${status}`}>
              S{n} {status === 'running' ? '⟳' : status === 'done' ? '✓' : '✕'}
            </div>
          ))}
        </div>
      )}

      {execEvents.length > 0 && (
        <div className="exec-log" ref={logRef}>
          {(() => {
            const seenTexts = new Set<string>()
            return execEvents.map((ev, i) => {
            const agentLabel = (name: string) =>
              DEV_AGENT_LABELS[name] || AGENT_META[name as PipelineAgent]?.label || name

            let text = ''
            let cls = ''
            if (ev.type === 'execution_start') {
              text = 'Starting up — getting the team ready to build'
              cls = 'info'
            }
            else if (ev.type === 'sprint_start') {
              const agents = ev.data.agents as string[] | undefined
              const who = agents?.length ? agents.map(agentLabel).join(' & ') : 'the team'
              text = `Sprint ${ev.data.sprint}  —  ${who} picked up the work`
              cls = 'sprint'
            }
            else if (ev.type === 'agent_start') {
              const name = ev.data.agent as string
              if (!PIPELINE_AGENTS.includes(name as PipelineAgent)) {
                text = `${agentLabel(name)} started coding`
                cls = 'dim'
              } else return null
            }
            else if (ev.type === 'agent_done') {
              const name = ev.data.agent as string
              if (!PIPELINE_AGENTS.includes(name as PipelineAgent)) {
                const files = ev.data.files_count as number
                text = `${agentLabel(name)} finished${files ? ` · ${files} files written` : ''}`
                cls = 'dim'
              } else return null
            }
            else if (ev.type === 'pl_review_start') {
              text = `Project Lead reviewing sprint ${ev.data.sprint}...`
              cls = 'dim'
            }
            else if (ev.type === 'pl_review_done') {
              text = `Project Lead signed off on sprint ${ev.data.sprint}`
              cls = 'info'
            }
            else if (ev.type === 'sprint_done') {
              text = `Sprint ${ev.data.sprint} wrapped up`
              cls = 'success'
            }
            else if (ev.type === 'awaiting_approval') {
              text = `Sprint ${ev.data.sprint} is done — waiting on your approval to continue`
              cls = 'gate'
            }
            else if (ev.type === 'sandbox_progress') {
              const msg = String(ev.data.message || '')
              if (msg.match(/setting up|environment|installing|scaffold|starting|server|venv|built|compiled|ready/i) &&
                  !msg.match(/already satisfied|Requirement|Collecting|Downloading|Using cached|\.whl|━━|━|WARNING/i)) {
                text = friendlySetupMsg(msg)
                cls = 'dim'
              } else return null
            }
            else if (ev.type === 'agent_stream') return null
            else if (ev.type === 'mvp_ready') {
              text = 'Build complete — your app is ready'
              cls = 'success big'
            }
            else if (ev.type === 'qa_tests_running') {
              text = `QA running test suite for sprint ${ev.data.sprint}...`
              cls = 'dim'
            }
            else if (ev.type === 'qa_tests_done') {
              const out = String(ev.data.output || '')
              const passed = out.match(/(\d+) passed/)?.[1]
              const failed = out.match(/(\d+) failed/)?.[1]
              const errors = out.match(/(\d+) error/)?.[1]
              if (failed || errors) {
                text = `QA tests logged — ${failed ? `${failed} failed` : ''}${errors ? ` ${errors} errors` : ''}`
                cls = 'err'
              } else if (passed) {
                text = `QA tests passed — ${passed} test${Number(passed) !== 1 ? 's' : ''} green`
                cls = 'info'
              } else {
                text = `QA logged test results for sprint ${ev.data.sprint}`
                cls = 'info'
              }
            }
            else if (ev.type === 'all_sprints_done') {
              text = 'All sprints done — running final review...'
              cls = 'success'
            }
            else if (ev.type === 'reviewer_start') {
              text = 'Reviewer scanning codebase for fatal errors...'
              cls = 'dim'
            }
            else if (ev.type === 'reviewer_done') {
              const fixes = ev.data.fixes_count as number
              const score = ev.data.score as number | undefined
              text = fixes > 0
                ? `Reviewer fixed ${fixes} issue${fixes !== 1 ? 's' : ''}${score != null ? ` · Score: ${score}/10` : ''}`
                : `Code review passed${score != null ? ` · Score: ${score}/10` : ''}`
              cls = 'info'
            }
            else if (ev.type === 'reviewer_stream') return null
            else if (ev.type === 'execution_error') {
              text = `Something went wrong: ${(ev.data.error as string) || (ev.data.message as string) || 'unknown error'}`
              cls = 'err'
            }
            else if (ev.type === 'demo_service_crashed') {
              const out = ev.data.output as string
              text = `Demo crashed (${ev.data.service}): ${out || 'no output captured'}`
              cls = 'err'
            }
            else if (ev.type === 'demo_demo_error') {
              text = `Demo error (${ev.data.service}): ${(ev.data.error as string) || 'unknown'}`
              cls = 'err'
            }
            else if (ev.type === 'demo_service_timeout') {
              text = `Demo service timed out: ${ev.data.service} never opened port ${ev.data.port}`
              cls = 'err'
            }
            else return null
            if (!text) return null
            // Deduplicate: include event index so per-sprint messages aren't suppressed
            const dedupeKey = `${i}:${text}`
            if (seenTexts.has(dedupeKey)) return null
            seenTexts.add(dedupeKey)
            return (
              <div key={i} className={`exec-log-line ${cls}`}>
                {text}
              </div>
            )
          })
        })()}
        </div>
      )}

      {isMvpReady && !reviewerRunning && (
        <div className="exec-demo">
          {demoUrl ? (
            <div className="exec-demo-ready">
              <span>Live at:</span>
              <a href={demoUrl} target="_blank" rel="noopener noreferrer" className="exec-demo-link">{demoUrl}</a>
            </div>
          ) : (
            <button className="wr-btn wr-btn-primary" onClick={onLaunchDemo} disabled={demoLoading}>
              {demoLoading ? <><span className="exec-spin">⟳</span> Launching...</> : '🚀 Launch Demo'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Table Connections ────────────────────────────────────────────────────────

const SEAT_PADDING = 80
const ALL_X = Object.values(AGENT_META).map(m => m.seat.x)
const ALL_Y = Object.values(AGENT_META).map(m => m.seat.y)
const SVG_LEFT   = Math.min(...ALL_X) - SEAT_PADDING
const SVG_TOP    = Math.min(...ALL_Y) - SEAT_PADDING
const SVG_WIDTH  = Math.max(...ALL_X) - SVG_LEFT + SEAT_PADDING
const SVG_HEIGHT = Math.max(...ALL_Y) - SVG_TOP  + SEAT_PADDING

function TableConnections({ agentStates, activeAgent }: {
  agentStates: Record<PipelineAgent, AgentState>
  activeAgent: PipelineAgent | null
}) {
  const order: PipelineAgent[] = ['engineering_manager', 'product_owner', 'architect', 'project_lead', 'hr']
  const ARROW_INSET = 38 // keep arrows clear of orb edges
  return (
    <svg style={{ position: 'absolute', left: SVG_LEFT, top: SVG_TOP, width: SVG_WIDTH, height: SVG_HEIGHT, overflow: 'visible', pointerEvents: 'none', zIndex: 1 }}>
      <defs>
        {/* Arrow markers for each connection */}
        {order.slice(0, -1).map((agent, i) => {
          const next = order[i + 1]
          const isDone = agentStates[agent].status === 'done'
          const gradId = `grad-${agent}`
          const markerId = `arrow-${agent}`
          const markerColor = isDone ? AGENT_META[next].colorB : 'rgba(124,111,255,0.18)'
          return (
            <g key={agent}>
              <linearGradient id={gradId} gradientUnits="userSpaceOnUse"
                x1={AGENT_META[agent].seat.x - SVG_LEFT} y1={AGENT_META[agent].seat.y - SVG_TOP}
                x2={AGENT_META[next].seat.x - SVG_LEFT} y2={AGENT_META[next].seat.y - SVG_TOP}>
                <stop offset="0%" stopColor={AGENT_META[agent].colorB} stopOpacity={isDone ? 0.55 : 0.12} />
                <stop offset="100%" stopColor={AGENT_META[next].colorB} stopOpacity={isDone ? 0.4 : 0.08} />
              </linearGradient>
              <marker id={markerId} markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 L1.5,3 Z" fill={markerColor} opacity={isDone ? 0.9 : 0.3} />
              </marker>
            </g>
          )
        })}
        {/* Animated dash filter for idle connections */}
        <style>{`
          @keyframes dash-flow { to { stroke-dashoffset: -24; } }
          .tc-line-active { animation: dash-flow 1.4s linear infinite; }
        `}</style>
      </defs>
      {order.slice(0, -1).map((agent, i) => {
        const next = order[i + 1]
        const from = AGENT_META[agent].seat
        const to = AGENT_META[next].seat
        const isDone = agentStates[agent].status === 'done'
        const isActive = activeAgent === agent || activeAgent === next

        // Shorten line endpoints so arrow doesn't overlap orbs
        const dx = to.x - from.x
        const dy = to.y - from.y
        const len = Math.sqrt(dx * dx + dy * dy)
        const ux = dx / len
        const uy = dy / len
        const x1 = (from.x - SVG_LEFT) + ux * ARROW_INSET
        const y1 = (from.y - SVG_TOP)  + uy * ARROW_INSET
        const x2 = (to.x - SVG_LEFT)   - ux * (ARROW_INSET + 4)
        const y2 = (to.y - SVG_TOP)     - uy * (ARROW_INSET + 4)

        return (
          <line
            key={`${agent}-${next}`}
            x1={x1} y1={y1} x2={x2} y2={y2}
            stroke={`url(#grad-${agent})`}
            strokeWidth={isDone ? 1.5 : 1}
            strokeDasharray={isDone ? 'none' : isActive ? '6 6' : '4 9'}
            strokeLinecap="round"
            markerEnd={`url(#arrow-${agent})`}
            className={isActive && !isDone ? 'tc-line-active' : ''}
            style={{ transition: 'stroke-width 0.5s, opacity 0.5s' }}
          />
        )
      })}
    </svg>
  )
}

// ─── Agent Faces ─────────────────────────────────────────────────────────────

function AgentFace({ agent }: { agent: PipelineAgent }) {
  const faces: Record<PipelineAgent, React.ReactElement> = {
    engineering_manager: (
      <svg viewBox="0 0 56 56" fill="none" className="wr-face-svg">
        <circle cx="28" cy="28" r="10" stroke="rgba(255,255,255,0.12)" strokeWidth="1.5" fill="none"/>
        <line x1="18" y1="28" x2="10" y2="28" stroke="rgba(255,255,255,0.1)" strokeWidth="1.5"/>
        <line x1="38" y1="28" x2="46" y2="28" stroke="rgba(255,255,255,0.1)" strokeWidth="1.5"/>
        <line x1="28" y1="18" x2="28" y2="10" stroke="rgba(255,255,255,0.1)" strokeWidth="1.5"/>
        <circle cx="10" cy="28" r="2" fill="rgba(255,255,255,0.18)"/>
        <circle cx="46" cy="28" r="2" fill="rgba(255,255,255,0.18)"/>
        <circle cx="28" cy="10" r="2" fill="rgba(255,255,255,0.18)"/>
        <rect x="20" y="24" width="6" height="2.5" rx="1.2" fill="rgba(255,255,255,0.9)"/>
        <rect x="30" y="24" width="6" height="2.5" rx="1.2" fill="rgba(255,255,255,0.9)"/>
        <path d="M23 32 Q28 30 33 32" stroke="rgba(255,255,255,0.75)" strokeWidth="1.5" strokeLinecap="round" fill="none"/>
        <line x1="20" y1="22" x2="26" y2="21" stroke="rgba(255,255,255,0.6)" strokeWidth="1.2" strokeLinecap="round"/>
        <line x1="36" y1="21" x2="30" y2="22" stroke="rgba(255,255,255,0.6)" strokeWidth="1.2" strokeLinecap="round"/>
      </svg>
    ),
    product_owner: (
      <svg viewBox="0 0 56 56" fill="none" className="wr-face-svg">
        <circle cx="28" cy="28" r="12" stroke="rgba(255,255,255,0.1)" strokeWidth="1" fill="none"/>
        <path d="M28 16 L30 26 L28 28 L26 26 Z" fill="rgba(255,255,255,0.18)"/>
        <path d="M28 40 L26 30 L28 28 L30 30 Z" fill="rgba(255,255,255,0.08)"/>
        <circle cx="22" cy="25" r="3.2" fill="rgba(255,255,255,0.92)"/>
        <circle cx="34" cy="25" r="3.2" fill="rgba(255,255,255,0.92)"/>
        <circle cx="23" cy="24.2" r="1.3" fill="rgba(0,0,0,0.5)"/>
        <circle cx="35" cy="24.2" r="1.3" fill="rgba(0,0,0,0.5)"/>
        <path d="M22 32 Q28 37 34 32" stroke="rgba(255,255,255,0.85)" strokeWidth="1.8" strokeLinecap="round" fill="none"/>
      </svg>
    ),
    architect: (
      <svg viewBox="0 0 56 56" fill="none" className="wr-face-svg">
        <polygon points="28,12 44,40 12,40" stroke="rgba(255,255,255,0.12)" strokeWidth="1.5" fill="none"/>
        <line x1="28" y1="20" x2="28" y2="36" stroke="rgba(255,255,255,0.07)" strokeWidth="1"/>
        <line x1="22" y1="32" x2="34" y2="32" stroke="rgba(255,255,255,0.07)" strokeWidth="1"/>
        <rect x="19" y="24" width="7" height="2" rx="1" fill="rgba(255,255,255,0.88)"/>
        <rect x="30" y="24" width="7" height="2" rx="1" fill="rgba(255,255,255,0.88)"/>
        <line x1="23" y1="31.5" x2="33" y2="31.5" stroke="rgba(255,255,255,0.65)" strokeWidth="1.5" strokeLinecap="round"/>
        <path d="M19 21.5 Q22.5 20 26 21.5" stroke="rgba(255,255,255,0.55)" strokeWidth="1.2" strokeLinecap="round" fill="none"/>
      </svg>
    ),
    project_lead: (
      <svg viewBox="0 0 56 56" fill="none" className="wr-face-svg">
        <path d="M14 28 L36 28 M30 21 L38 28 L30 35" stroke="rgba(255,255,255,0.14)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
        <ellipse cx="22" cy="25" rx="3" ry="2.2" fill="rgba(255,255,255,0.9)"/>
        <ellipse cx="34" cy="25" rx="3" ry="2.2" fill="rgba(255,255,255,0.9)"/>
        <circle cx="22.6" cy="25" r="1.1" fill="rgba(0,0,0,0.45)"/>
        <circle cx="34.6" cy="25" r="1.1" fill="rgba(0,0,0,0.45)"/>
        <path d="M22 32 Q26 34.5 34 31.5" stroke="rgba(255,255,255,0.78)" strokeWidth="1.6" strokeLinecap="round" fill="none"/>
        <path d="M19 22.5 Q22 21 25 22.5" stroke="rgba(255,255,255,0.55)" strokeWidth="1.2" strokeLinecap="round" fill="none"/>
        <line x1="31" y1="23" x2="37" y2="23" stroke="rgba(255,255,255,0.45)" strokeWidth="1.2" strokeLinecap="round"/>
      </svg>
    ),
    hr: (
      <svg viewBox="0 0 56 56" fill="none" className="wr-face-svg">
        <path d="M28 10 L29.8 21.8 L41 16 L34.4 25.8 L46 28 L34.4 30.2 L41 40 L29.8 34.2 L28 46 L26.2 34.2 L15 40 L21.6 30.2 L10 28 L21.6 25.8 L15 16 L26.2 21.8 Z" stroke="rgba(255,255,255,0.1)" strokeWidth="1" fill="none"/>
        <path d="M19 25 Q22 22.5 25 25" stroke="rgba(255,255,255,0.9)" strokeWidth="2" strokeLinecap="round" fill="none"/>
        <path d="M31 25 Q34 22.5 37 25" stroke="rgba(255,255,255,0.9)" strokeWidth="2" strokeLinecap="round" fill="none"/>
        <path d="M21 31 Q28 37.5 35 31" stroke="rgba(255,255,255,0.88)" strokeWidth="1.8" strokeLinecap="round" fill="none"/>
        <circle cx="19" cy="31" r="1.5" fill="rgba(255,255,255,0.2)"/>
        <circle cx="37" cy="31" r="1.5" fill="rgba(255,255,255,0.2)"/>
      </svg>
    ),
  }
  return faces[agent]
}

function SpawnedFace({ archetype }: { archetype: string }) {
  const faces: Record<string, React.ReactElement> = {
    backend_dev: (<>
      <rect x="12" y="14" width="22" height="4" rx="1" stroke="rgba(255,255,255,0.09)" strokeWidth="1" fill="none"/>
      <rect x="12" y="20" width="22" height="4" rx="1" stroke="rgba(255,255,255,0.09)" strokeWidth="1" fill="none"/>
      <rect x="12" y="26" width="22" height="4" rx="1" stroke="rgba(255,255,255,0.09)" strokeWidth="1" fill="none"/>
      <rect x="18" y="22" width="5" height="3" rx="1" fill="rgba(255,255,255,0.88)"/>
      <rect x="27" y="22" width="5" height="3" rx="1" fill="rgba(255,255,255,0.88)"/>
      <line x1="19" y1="33" x2="27" y2="33" stroke="rgba(255,255,255,0.55)" strokeWidth="1.4" strokeLinecap="round"/>
    </>),
    frontend_dev: (<>
      <path d="M10 18 L14 23 L10 28" stroke="rgba(255,255,255,0.1)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
      <path d="M36 18 L32 23 L36 28" stroke="rgba(255,255,255,0.1)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
      <circle cx="19" cy="22" r="3" fill="rgba(255,255,255,0.9)"/>
      <circle cx="27" cy="22" r="3" fill="rgba(255,255,255,0.9)"/>
      <circle cx="19.8" cy="21.2" r="1.2" fill="rgba(0,0,0,0.5)"/>
      <circle cx="27.8" cy="21.2" r="1.2" fill="rgba(0,0,0,0.5)"/>
      <path d="M18 30 Q23 34 28 30" stroke="rgba(255,255,255,0.8)" strokeWidth="1.6" strokeLinecap="round" fill="none"/>
    </>),
    python_dev: (<>
      <path d="M23 10 Q30 10 32 16 Q32 22 23 22 Q14 22 14 16 Q16 10 23 10Z" stroke="rgba(255,255,255,0.08)" strokeWidth="1" fill="none"/>
      <path d="M16 22 Q19 19.5 22 22" stroke="rgba(255,255,255,0.88)" strokeWidth="2" strokeLinecap="round" fill="none"/>
      <path d="M24 22 Q27 19.5 30 22" stroke="rgba(255,255,255,0.88)" strokeWidth="2" strokeLinecap="round" fill="none"/>
      <path d="M19 29 Q23 32 27 29" stroke="rgba(255,255,255,0.65)" strokeWidth="1.5" strokeLinecap="round" fill="none"/>
    </>),
    mobile_dev: (<>
      <rect x="18" y="11" width="10" height="18" rx="2.5" stroke="rgba(255,255,255,0.1)" strokeWidth="1" fill="none"/>
      <circle cx="19" cy="21" r="2.8" fill="rgba(255,255,255,0.9)"/>
      <circle cx="27" cy="21" r="2.8" fill="rgba(255,255,255,0.9)"/>
      <circle cx="19.7" cy="20.3" r="1.1" fill="rgba(0,0,0,0.5)"/>
      <circle cx="27.7" cy="20.3" r="1.1" fill="rgba(0,0,0,0.5)"/>
      <path d="M18 29 Q23 33.5 28 29" stroke="rgba(255,255,255,0.82)" strokeWidth="1.6" strokeLinecap="round" fill="none"/>
    </>),
    devops: (<>
      <path d="M13 23 Q17 17 23 23 Q29 29 33 23" stroke="rgba(255,255,255,0.1)" strokeWidth="1.5" fill="none"/>
      <rect x="16" y="20" width="6" height="3" rx="1.5" fill="rgba(255,255,255,0.85)"/>
      <rect x="24" y="20" width="6" height="3" rx="1.5" fill="rgba(255,255,255,0.85)"/>
      <line x1="18" y1="29" x2="28" y2="29" stroke="rgba(255,255,255,0.6)" strokeWidth="1.5" strokeLinecap="round"/>
    </>),
    ml_engineer: (<>
      <circle cx="14" cy="30" r="1.2" fill="rgba(255,255,255,0.12)"/>
      <circle cx="19" cy="24" r="1.2" fill="rgba(255,255,255,0.12)"/>
      <circle cx="25" cy="28" r="1.2" fill="rgba(255,255,255,0.12)"/>
      <circle cx="30" cy="20" r="1.2" fill="rgba(255,255,255,0.12)"/>
      <circle cx="19" cy="21" r="3.2" fill="rgba(255,255,255,0.9)"/>
      <circle cx="27" cy="21" r="3.2" fill="rgba(255,255,255,0.9)"/>
      <circle cx="20" cy="20" r="1.4" fill="rgba(0,0,0,0.5)"/>
      <circle cx="28" cy="20" r="1.4" fill="rgba(0,0,0,0.5)"/>
      <path d="M18 30 Q23 33 28 30" stroke="rgba(255,255,255,0.7)" strokeWidth="1.5" strokeLinecap="round" fill="none"/>
    </>),
    qa_dev: (<>
      <path d="M15 30 L20 35 L31 22" stroke="rgba(255,255,255,0.1)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
      <rect x="17" y="21" width="6" height="2.5" rx="1.2" fill="rgba(255,255,255,0.88)"/>
      <rect x="27" y="21" width="6" height="2.5" rx="1.2" fill="rgba(255,255,255,0.88)"/>
      <path d="M17 18.5 Q20 17 23 18.5" stroke="rgba(255,255,255,0.55)" strokeWidth="1.2" strokeLinecap="round" fill="none"/>
      <path d="M19 29.5 Q23 27 27 29.5" stroke="rgba(255,255,255,0.6)" strokeWidth="1.4" strokeLinecap="round" fill="none"/>
    </>),
  }

  const glassyFace = (<>
    <rect x="14" y="20" width="7" height="5" rx="2.5" stroke="rgba(255,255,255,0.82)" strokeWidth="1.4" fill="none"/>
    <rect x="25" y="20" width="7" height="5" rx="2.5" stroke="rgba(255,255,255,0.82)" strokeWidth="1.4" fill="none"/>
    <line x1="21" y1="22.5" x2="25" y2="22.5" stroke="rgba(255,255,255,0.7)" strokeWidth="1.2"/>
    <line x1="11" y1="22.5" x2="14" y2="22.5" stroke="rgba(255,255,255,0.5)" strokeWidth="1.2"/>
    <line x1="32" y1="22.5" x2="35" y2="22.5" stroke="rgba(255,255,255,0.5)" strokeWidth="1.2"/>
    <line x1="18" y1="30" x2="28" y2="30" stroke="rgba(255,255,255,0.6)" strokeWidth="1.4" strokeLinecap="round"/>
  </>)

  const face = faces[archetype] ?? glassyFace
  return (
    <svg viewBox="0 0 46 46" fill="none" className="wr-face-svg wr-face-svg--sm">
      {face}
    </svg>
  )
}

// ─── Agent Seat ───────────────────────────────────────────────────────────────

function AgentSeat({ agent, state, isActive, isFocused, needsInput, onClick }: {
  agent: PipelineAgent; state: AgentState
  isActive: boolean; isFocused: boolean; needsInput: boolean; onClick: () => void
}) {
  const meta = AGENT_META[agent]
  const latestThought = state.thoughts[state.thoughts.length - 1] || ''
  // Agents at y < 0 are in the top half — show bubble below orb to avoid canvas edge
  const bubbleBelow = meta.seat.y < 0

  return (
    <div
      className={['wr-seat', isActive ? 'active' : '', state.status === 'done' ? 'done' : '', isFocused ? 'focused' : '', needsInput ? 'needs-input' : ''].filter(Boolean).join(' ')}
      style={{ left: meta.seat.x, top: meta.seat.y, '--seat-color': meta.color, '--seat-color-b': meta.colorB } as React.CSSProperties}
      onClick={onClick}
    >
      {isActive && latestThought && !needsInput && (
        <div className={`wr-bubble${bubbleBelow ? ' wr-bubble--below' : ''}`}>
          {latestThought.length > 80 ? latestThought.slice(0, 80) + '…' : latestThought}
        </div>
      )}
      {needsInput && (
        <div className={`wr-input-badge${bubbleBelow ? ' wr-input-badge--below' : ''}`}>
          ● needs your input
        </div>
      )}
      <div className="wr-avatar">
        <div className="wr-avatar-orb">
          <AgentFace agent={agent} />
        </div>
        {isActive && <div className="wr-avatar-ring" />}
        {isActive && <div className="wr-avatar-ring wr-avatar-ring-2" />}
        {state.status === 'done' && <div className="wr-avatar-done">✓</div>}
      </div>
      <div className="wr-nametag" title={meta.roleDesc}>
        <span className="wr-nametag-label">{meta.label}</span>
      </div>
      {state.artifactType && state.status === 'done' && (
        <div className="wr-artifact-chip">{state.artifactType.replace(/_/g, ' ')}</div>
      )}
    </div>
  )
}

// ─── Spawned Bubble ───────────────────────────────────────────────────────────

function SpawnedBubble({ agent, pos, isActive, onClick }: {
  agent: SpawnedAgent; pos: { x: number; y: number }; isActive: boolean; onClick: () => void
}) {
  const meta = ARCHETYPE_META[agent.archetype] || { color: '#6b6b80', colorB: '#888', role: agent.archetype, abbr: agent.archetype.slice(0, 2).toUpperCase(), tech: '' }
  return (
    <div
      className={`wr-spawned-bubble${isActive ? ' active' : ''}`}
      style={{ left: pos.x, top: pos.y, '--bubble-color': meta.color, '--bubble-color-b': meta.colorB } as React.CSSProperties}
      onClick={onClick}
    >
      <div className="wr-spawned-orb-wrap">
        {isActive && <div className="wr-spawned-ring" />}
        {isActive && <div className="wr-spawned-ring wr-spawned-ring-2" />}
        <div className="wr-spawned-orb">
          <SpawnedFace archetype={agent.archetype} />
        </div>
      </div>
      <div className="wr-spawned-name">{meta.role}</div>
      <div className="wr-spawned-role">{meta.tech}</div>
    </div>
  )
}

// ─── Agent Modal ──────────────────────────────────────────────────────────────

function AgentModal({ modal, agentStates, onAnswer, onClose }: {
  modal: NonNullable<ModalTarget>; agentStates: Record<PipelineAgent, AgentState>
  onAnswer: (a: string) => void; onClose: () => void
}) {
  const onBackdrop = (e: React.MouseEvent) => { if (e.target === e.currentTarget) onClose() }
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onBackdrop}>
      <div className="modal-box">
        {modal.kind === 'pipeline' && <PipelineAgentPanel agent={modal.agent} state={agentStates[modal.agent]} onClose={onClose} />}
        {modal.kind === 'spawned' && <SpawnedAgentPanel agent={modal.agent} onClose={onClose} />}
        {modal.kind === 'clarification' && <ClarificationModalPanel clarification={modal.clarification} onAnswer={onAnswer} onClose={onClose} />}
      </div>
    </div>
  )
}

// ─── Artifact renderer ────────────────────────────────────────────────────────

// ─── Thinking word rotator ────────────────────────────────────────────────────

const THINKING_WORDS = [
  'Pondering...', 'Deliberating...', 'Crafting...', 'Synthesizing...', 'Reasoning...',
  'Scheming...', 'Architecting...', 'Mulling it over...', 'Cogitating...', 'Deep in thought...',
  'Noodling...', 'Ruminating...', 'Strategizing...', 'Connecting dots...', 'Brain is spinning...',
]

function useThinkingWord() {
  const [idx, setIdx] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setIdx(i => (i + 1) % THINKING_WORDS.length), 2200)
    return () => clearInterval(t)
  }, [])
  return THINKING_WORDS[idx]
}

// ─── Artifact summary cards ───────────────────────────────────────────────────

// ─── Extract named sections from agent artifacts ─────────────────────────────

function extractSection(text: string, key: string): string {
  const re = new RegExp(`${key}:\\s*\\n([\\s\\S]*?)(?=\\n[A-Z_]{3,}:|$)`, 'i')
  const m = text.match(re)
  return m ? m[1].trim() : ''
}

function extractJsonBlock(text: string): unknown | null {
  const m = text.match(/```(?:json)?\s*([\s\S]*?)```/)
  if (!m) return null
  try { return JSON.parse(m[1]) } catch { return null }
}

function cleanLine(s: string) { return s.replace(/\*\*/g, '').replace(/\*/g, '').trim() }

function bulletLines(text: string, max = 6): string[] {
  return text.split('\n')
    .map(l => l.trim())
    .filter(l => l.length > 4 && !l.match(/^[A-Z_]{4,}:?\s*$/) && !l.startsWith('```') && !l.startsWith('{') && !l.startsWith('['))
    .map(l => l.replace(/^[-*•]\s+/, '').replace(/^\d+\.\s+/, ''))
    .filter(l => l.length > 4)
    .map(cleanLine)
    .filter((l, i, a) => a.indexOf(l) === i)
    .slice(0, max)
}

interface SummaryCard { heading: string; bullets: string[] }

function parseArchitectSummary(artifact: string): SummaryCard[] {
  const cards: SummaryCard[] = []

  const techStack = extractSection(artifact, 'TECH_STACK')
  if (techStack) {
    const lines = techStack.split('\n').filter(l => l.trim() && !l.match(/^[A-Z_]{4,}:?\s*$/))
    cards.push({ heading: 'Tech Stack', bullets: lines.map(cleanLine).filter(l => l.length > 2).slice(0, 6) })
  }

  const arch = extractSection(artifact, 'ARCHITECTURE_MD')
  if (arch) {
    cards.push({ heading: 'Architecture', bullets: bulletLines(arch, 5) })
  }

  const dir = extractSection(artifact, 'DIRECTORY_STRUCTURE')
  if (dir) {
    const paths = dir.split('\n').map(l => l.trim()).filter(l => l.match(/\.(py|js|jsx|ts|tsx|html|css|yaml|json)/) || l.endsWith('/'))
    const grouped: Record<string, string[]> = {}
    for (const p of paths) {
      const top = p.split('/')[0] || 'root'
      if (!grouped[top]) grouped[top] = []
      grouped[top].push(p)
    }
    const topDirs = Object.keys(grouped).slice(0, 5)
    if (topDirs.length > 0) {
      cards.push({ heading: 'Directory Layout', bullets: topDirs.map(d => `${d}/ (${grouped[d].length} files)`) })
    }
  }

  const coding = extractSection(artifact, 'CODING_STANDARDS_MD')
  if (coding) {
    cards.push({ heading: 'Coding Standards', bullets: bulletLines(coding, 5) })
  }

  return cards
}

function parseProjectLeadSummary(artifact: string): SummaryCard[] {
  const cards: SummaryCard[] = []

  const sprintJson = extractJsonBlock(artifact)
  if (sprintJson && typeof sprintJson === 'object' && sprintJson !== null) {
    const plan = sprintJson as Record<string, unknown>
    const totalSprints = plan.total_sprints as number
    const demoReady = plan.demo_ready_after_sprint as number
    const critPath = (plan.critical_path as string[]) || []
    const sprints = (plan.sprints as Array<Record<string, unknown>>) || []

    const actualSprintCount = sprints.length || totalSprints
    cards.push({
      heading: 'Sprint Overview',
      bullets: [
        `${actualSprintCount} sprint${actualSprintCount !== 1 ? 's' : ''} planned`,
        demoReady ? `Demo-ready after sprint ${demoReady}` : '',
        ...critPath.slice(0, 3).map(s => String(s)),
      ].filter(Boolean),
    })

    for (const sprint of sprints.slice(0, 4)) {
      const num = sprint.sprint as number
      const goal = sprint.goal as string
      const tasks = (sprint.tasks as Array<Record<string, unknown>>) || []
      cards.push({
        heading: `Sprint ${num}: ${goal?.slice(0, 60) || ''}`,
        bullets: tasks.map(t => {
          const role = (t.role as string || '').replace(/_/g, ' ')
          const task = (t.task as string || '').slice(0, 80)
          return `${role}: ${task}`
        }).slice(0, 4),
      })
    }
  }

  const hrRequest = extractSection(artifact, 'HR_HANDOFF_REQUEST')
  if (hrRequest) {
    const roles = hrRequest.split('\n').filter(l => l.trim().match(/^-\s+\w+:/))
      .map(l => l.replace(/^-\s+/, '').trim()).map(cleanLine).slice(0, 6)
    if (roles.length > 0) {
      cards.push({ heading: 'Developer Team Requested', bullets: roles })
    }
  }

  return cards
}

function parseHRSummary(artifact: string): SummaryCard[] {
  const cards: SummaryCard[] = []

  // Parse each SPAWN_AGENT block
  const blocks = artifact.split(/^---\s*$/m)
  for (const block of blocks) {
    const nameM = block.match(/^name:\s*(.+)$/m)
    const archetypeM = block.match(/^archetype:\s*(.+)$/m)
    const promptM = block.match(/system_prompt:\s*\|\s*\n([\s\S]+?)(?=\n\w|\Z)/)
    if (!nameM) continue

    const name = nameM[1].trim().replace(/_/g, ' ')
    const archetype = archetypeM ? archetypeM[1].trim().replace(/_/g, ' ') : ''
    const prompt = promptM ? promptM[1] : ''

    const bullets: string[] = []
    if (archetype && archetype !== name) bullets.push(`Archetype: ${archetype}`)
    // Pull a few key sentences from the prompt — first 2 non-boilerplate lines
    const promptLines = prompt.split('\n').map(l => l.trim()).filter(l =>
      l.length > 20 && !l.match(/^You are/) && !l.match(/^RULES:/) && !l.match(/^OUTPUT/) &&
      !l.match(/^UI QUALITY/) && !l.match(/^Every/) && !l.match(/^Design/) && !l.match(/^Write every/)
    )
    bullets.push(...promptLines.slice(0, 3).map(l => l.length > 100 ? l.slice(0, 97) + '…' : l))

    cards.push({ heading: name.charAt(0).toUpperCase() + name.slice(1), bullets: bullets.slice(0, 4) })
  }

  return cards
}

function parseGenericSummary(artifact: string, artifactType: string | null): SummaryCard[] {
  // Strip code/JSON blobs
  const cleaned = artifact
    .replace(/```[\s\S]*?```/g, '')
    .replace(/\{[\s\S]{300,}?\}/g, '')

  const lines = cleaned.split('\n').map(l => l.trim()).filter(Boolean)
  const result: SummaryCard[] = []
  let current: SummaryCard | null = null

  const isBullet = (l: string) => /^[-*•]\s+/.test(l) || /^\d+\.\s+/.test(l)
  // Only treat lines as headings if they use markdown # or are ALL_CAPS section keys (not mixed case)
  const isHeading = (l: string) => /^#{1,4}\s+/.test(l) || /^[A-Z][A-Z_\s]{3,}:\s*$/.test(l)
  const extractBullet = (l: string) => l.replace(/^[-*•]\s+/, '').replace(/^\d+\.\s+/, '').trim()
  const extractHeading = (l: string) => l.replace(/^#{1,4}\s+/, '').replace(/:$/, '').trim()
  const cleanH = (h: string) => h.replace(/\*\*/g, '').replace(/\*/g, '').replace(/_/g, ' ').replace(/:$/, '').trim()

  const shorten = (b: string): string => {
    const s = b.replace(/\*\*/g, '').replace(/\*/g, '').trim()
    if (/^[A-Za-z\s]+:\s+\S/.test(s) && s.length < 120) return s
    const sent = s.match(/^[^.!?]+[.!?]/)
    if (sent && sent[0].length < 120) return sent[0].trim()
    return s.length > 120 ? s.slice(0, 117) + '…' : s
  }

  for (const line of lines) {
    if (isHeading(line)) {
      if (current && current.bullets.length > 0) result.push(current)
      current = { heading: cleanH(extractHeading(line)), bullets: [] }
    } else if (isBullet(line)) {
      if (!current) current = { heading: '', bullets: [] }
      const b = shorten(extractBullet(line))
      if (b && !current.bullets.includes(b)) current.bullets.push(b)
    } else if (line.length > 20 && !line.startsWith('|') && !line.startsWith('-')) {
      if (!current) current = { heading: '', bullets: [] }
      const b = shorten(line)
      if (b && !current.bullets.includes(b)) current.bullets.push(b)
    }
  }
  if (current && current.bullets.length > 0) result.push(current)

  if (result.length === 0 && artifact.trim()) {
    const words = artifact.replace(/\*\*/g, '').split(/\s+/).filter(Boolean)
    return [{ heading: artifactType?.replace(/_/g, ' ') || 'Summary', bullets: [words.slice(0, 40).join(' ') + (words.length > 40 ? '…' : '')] }]
  }

  return result.slice(0, 7).map(s => ({ ...s, bullets: s.bullets.slice(0, 5) }))
}

function ArtifactSummaryCards({ artifact, artifactType, agentName, color, colorB }: {
  artifact: string; artifactType: string | null; agentName?: PipelineAgent; color: string; colorB: string
}) {
  const sections = useMemo(() => {
    if (agentName === 'architect') return parseArchitectSummary(artifact)
    if (agentName === 'project_lead') return parseProjectLeadSummary(artifact)
    if (agentName === 'hr') return parseHRSummary(artifact)
    return parseGenericSummary(artifact, artifactType)
  }, [artifact, artifactType, agentName])

  if (sections.length === 0) {
    return <div className="modal-empty">No output yet.</div>
  }

  return (
    <div className="artifact-cards">
      {sections.map((s, i) => (
        <div key={i} className="artifact-card" style={{ '--card-accent': color, '--card-accent-b': colorB } as React.CSSProperties}>
          {s.heading && <div className="artifact-card-heading" style={{ color: colorB }}>{s.heading}</div>}
          <ul className="artifact-card-list">
            {s.bullets.map((b, j) => <li key={j}>{b}</li>)}
          </ul>
        </div>
      ))}
    </div>
  )
}

// ─── Pipeline Agent Panel ──────────────────────────────────────────────────────

function PipelineAgentPanel({ agent, state, onClose }: {
  agent: PipelineAgent; state: AgentState; onClose: () => void
}) {
  const meta = AGENT_META[agent]
  const [tab, setTab] = useState<'activity' | 'summary' | 'raw'>(
    state.artifact ? 'summary' : 'activity'
  )
  const bottomRef = useRef<HTMLDivElement>(null)
  const thinkingWord = useThinkingWord()

  useEffect(() => { if (tab === 'activity') bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [state.thoughts, tab])
  useEffect(() => { if (state.artifact && tab === 'activity') setTab('summary') }, [state.artifact])

  // Clean thoughts for activity log
  const cleanThoughts = useMemo(() => {
    const seen = new Set<string>()
    return state.thoughts
      .filter(t => {
        const trimmed = t.trim()
        if (!trimmed || trimmed.length < 8) return false
        if (trimmed.startsWith('{') || trimmed.startsWith('[')) return false
        if (trimmed.startsWith('```')) return false
        const key = trimmed.slice(0, 60).toLowerCase()
        if (seen.has(key)) return false
        seen.add(key)
        return true
      })
      .slice(-20)
  }, [state.thoughts])

  const tabs: { key: 'activity' | 'summary' | 'raw'; label: string }[] = [
    { key: 'activity', label: 'Activity' },
    ...(state.artifact ? [
      { key: 'summary' as const, label: 'Summary' },
      { key: 'raw' as const, label: 'Raw' },
    ] : []),
  ]

  return (
    <>
      <div className="modal-header" style={{ '--seat-color': meta.color, '--seat-color-b': meta.colorB } as React.CSSProperties}>
        <div className="modal-header-left">
          <div className="modal-agent-orb" style={{ background: `radial-gradient(circle at 35% 35%, ${meta.colorB}, ${meta.color})` }}>
            <AgentFace agent={agent} />
          </div>
          <div>
            <div className="modal-title" style={{ color: meta.colorB }}>{meta.label}</div>
            <div className="modal-subtitle">{meta.roleDesc}</div>
          </div>
        </div>
        <div className="modal-header-right">
          {state.status === 'thinking' && <span className="modal-spinner" style={{ borderTopColor: meta.color }} />}
          {state.status === 'done' && <span className="modal-check">✓</span>}
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
      </div>

      <div className="modal-status-row">
        <span className={`modal-status-dot ${state.status}`} />
        <span className="modal-status-text">
          {state.status === 'thinking'
            ? <span className="modal-thinking-word">{thinkingWord}</span>
            : state.status === 'done'
            ? `Done · ${state.artifactType?.replace(/_/g, ' ') || 'output ready'}`
            : 'Waiting to start'}
        </span>
      </div>

      <div className="modal-tabs">
        {tabs.map(t => (
          <button
            key={t.key}
            className={`modal-tab ${tab === t.key ? 'active' : ''}`}
            onClick={() => setTab(t.key)}
            style={tab === t.key ? { color: meta.colorB, borderBottomColor: meta.color } : {}}
          >
            {t.label}
            {t.key === 'raw' && <span className="modal-tab-badge">dev</span>}
          </button>
        ))}
      </div>

      <div className="modal-body">
        {tab === 'activity' && (
          <div className="modal-thought-log">
            {cleanThoughts.length === 0 && state.status === 'idle' && <div className="modal-empty">Waiting to start...</div>}
            {cleanThoughts.length === 0 && state.status === 'thinking' && (
              <div className="modal-empty modal-empty--thinking">
                <span className="modal-thinking-dots" style={{ color: meta.colorB }}>●●●</span>
                <span>{thinkingWord}</span>
              </div>
            )}
            {cleanThoughts.map((t, i) => (
              <div key={i} className={`modal-thought-line ${i === cleanThoughts.length - 1 && state.status === 'thinking' ? 'latest' : ''}`}>
                <span className="modal-thought-bullet" style={{ color: meta.colorB }}>▸</span>
                <span>{t.length > 200 ? t.slice(0, 200) + '…' : t}</span>
              </div>
            ))}
            {state.status === 'thinking' && cleanThoughts.length > 0 && (
              <div className="modal-cursor" style={{ background: meta.color }} />
            )}
            <div ref={bottomRef} />
          </div>
        )}
        {tab === 'summary' && state.artifact && (
          <div className="modal-artifact-rich">
            <ArtifactSummaryCards
              artifact={state.artifact}
              artifactType={state.artifactType}
              agentName={agent}
              color={meta.color}
              colorB={meta.colorB}
            />
          </div>
        )}
        {tab === 'raw' && state.artifact && (
          <pre className="modal-artifact">{state.artifact}</pre>
        )}
      </div>
    </>
  )
}

// ─── Spawned Agent Panel ──────────────────────────────────────────────────────

function SpawnedAgentPanel({ agent, onClose }: { agent: SpawnedAgent; onClose: () => void }) {
  const meta = ARCHETYPE_META[agent.archetype] || { color: '#6b6b80', colorB: '#888', role: agent.archetype, abbr: agent.archetype.slice(0, 2).toUpperCase() }
  const sections = parsePromptSections(agent.systemPrompt)

  return (
    <>
      <div className="modal-header" style={{ '--seat-color': meta.color, '--seat-color-b': meta.colorB } as React.CSSProperties}>
        <div className="modal-header-left">
          <div className="modal-agent-orb" style={{ background: `radial-gradient(circle at 35% 35%, ${meta.colorB}, ${meta.color})` }}>
            <SpawnedFace archetype={agent.archetype} />
          </div>
          <div>
            <div className="modal-title" style={{ color: meta.colorB }}>{agent.name}</div>
            <div className="modal-subtitle">{meta.role} · assembled by HR</div>
          </div>
        </div>
        <button className="modal-close" onClick={onClose}>✕</button>
      </div>
      <div className="modal-body">
        {sections.length > 0 ? (
          <div className="spawned-sections">
            {sections.map((s, i) => (
              <div key={i} className="spawned-section">
                {s.heading && <div className="spawned-section-heading" style={{ color: meta.colorB }}>{s.heading}</div>}
                <div className="spawned-section-body">{s.body}</div>
              </div>
            ))}
          </div>
        ) : (
          <pre className="modal-artifact">{agent.systemPrompt || 'No system prompt captured.'}</pre>
        )}
      </div>
    </>
  )
}

// ─── Clarification Modal Panel ────────────────────────────────────────────────

function ClarificationModalPanel({ clarification, onAnswer, onClose }: {
  clarification: Clarification; onAnswer: (a: string) => void; onClose: () => void
}) {
  const meta = AGENT_META[clarification.agent]
  const [answer, setAnswer] = useState('')
  const submit = () => { if (!answer.trim()) return; onAnswer(answer.trim()); setAnswer('') }
  const onKey = (e: React.KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submit() }

  return (
    <>
      <div className="modal-header" style={{ '--seat-color': meta.color, '--seat-color-b': meta.colorB } as React.CSSProperties}>
        <div className="modal-header-left">
          <div className="modal-agent-orb" style={{ background: `radial-gradient(circle at 35% 35%, ${meta.colorB}, ${meta.color})` }}>
            <AgentFace agent={clarification.agent} />
          </div>
          <div>
            <div className="modal-title" style={{ color: meta.colorB }}>{meta.label} has a question</div>
            <div className="modal-subtitle">Round {clarification.round} of {clarification.maxRounds} · Your answer helps the agents plan better</div>
          </div>
        </div>
        <button className="modal-close" onClick={onClose}>✕</button>
      </div>
      <div className="modal-body">
        <div className="clar-questions">
          {clarification.question.split('\n').filter(Boolean).map((l, i) => (
            <div key={i} className="clar-question-line">{l}</div>
          ))}
        </div>
        <textarea
          className="clar-answer-input"
          placeholder="Type your answer here... (⌘↵ to send)"
          value={answer}
          onChange={e => setAnswer(e.target.value)}
          onKeyDown={onKey}
          autoFocus
          style={{ '--seat-color': meta.color } as React.CSSProperties}
        />
        <div className="clar-footer">
          <button className="clar-skip" onClick={() => onAnswer('')}>Skip — proceed without answering</button>
          <button className="clar-send" onClick={submit} disabled={!answer.trim()} style={{ background: `linear-gradient(135deg, ${meta.color}, ${meta.colorB})` }}>
            Send answer <kbd>⌘↵</kbd>
          </button>
        </div>
      </div>
    </>
  )
}

// ─── Landing ──────────────────────────────────────────────────────────────────

type Creds = { azureKey: string; azureEndpoint: string; azureDeploymentFull: string; azureApiVersion: string; anthropicKey: string }

function LandingView({
  story, setStory, projectName, setProjectName, creds, setCreds, onSubmit, loading,
}: {
  story: string; setStory: (s: string) => void
  projectName: string; setProjectName: (s: string) => void
  creds: Creds; setCreds: (c: Creds) => void
  onSubmit: () => void; loading: boolean
}) {
  const onKey = (e: React.KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') onSubmit() }
  const set = (k: keyof Creds) => (e: React.ChangeEvent<HTMLInputElement>) => setCreds({ ...creds, [k]: e.target.value })

  // All 5 fields filled (either real creds or admin password shortcut)
  const credsReady = Object.values(creds).every(v => v.trim().length > 0)

  return (
    <div className="land">
      <div className="land-dots" />
      <div className="land-inner">
        <div className="land-brand">
          <img src="/entourage_icon.png" alt="" className="land-logo-icon" />
          <span className="land-logo-wordmark">entourage</span>
        </div>

        <p className="land-sub">Describe what you want to build. Your AI war room handles the rest.</p>

        <div className="land-form">
          <input
            className="land-name"
            placeholder="Project name (optional)"
            value={projectName}
            onChange={e => setProjectName(e.target.value)}
            onKeyDown={onKey}
          />
          <div className="land-wrap">
            <textarea
              className="land-area"
              placeholder="Build a habit tracker with streaks, charts, and social sharing..."
              value={story}
              onChange={e => setStory(e.target.value)}
              onKeyDown={onKey}
              autoFocus
            />
            <button className="land-go" onClick={onSubmit} disabled={!story.trim() || !credsReady || loading}>
              {loading ? <span className="spin" /> : <>Assemble team <kbd>⌘↵</kbd></>}
            </button>
          </div>

          <div className="land-chain">
            {(['engineering_manager','product_owner','architect','project_lead','hr'] as PipelineAgent[]).map((a, i, arr) => (
              <span key={a}>
                <span className="land-chain-agent" style={{ color: AGENT_META[a].colorB }} title={AGENT_META[a].roleDesc}>{AGENT_META[a].label}</span>
                {i < arr.length - 1 && <span className="land-chain-arrow"> → </span>}
              </span>
            ))}
          </div>

          <div className="land-creds">
            <p className="land-creds-label">API credentials</p>
            <p className="land-creds-hint">Enter your API keys below — or paste your admin password into all five fields to use saved credentials.</p>
            <div className="land-creds-grid">
              <div className="land-cred-row">
                <label>Azure OpenAI Key</label>
                <input type="password" placeholder="Azure API key" value={creds.azureKey} onChange={set('azureKey')} autoComplete="off" />
              </div>
              <div className="land-cred-row">
                <label>Azure Endpoint</label>
                <input type="text" placeholder="https://your-resource.openai.azure.com" value={creds.azureEndpoint} onChange={set('azureEndpoint')} autoComplete="off" />
              </div>
              <div className="land-cred-row">
                <label>Azure Deployment</label>
                <input type="text" placeholder="gpt-4o, gpt-4.1, etc." value={creds.azureDeploymentFull} onChange={set('azureDeploymentFull')} autoComplete="off" />
              </div>
              <div className="land-cred-row">
                <label>Azure API Version</label>
                <input type="text" placeholder="2024-12-01-preview" value={creds.azureApiVersion} onChange={set('azureApiVersion')} autoComplete="off" />
              </div>
              <div className="land-cred-row">
                <label>Anthropic API Key</label>
                <input type="password" placeholder="sk-ant-..." value={creds.anthropicKey} onChange={set('anthropicKey')} autoComplete="off" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function friendlySetupMsg(raw: string): string {
  const r = raw.toLowerCase()
  if (r.includes('venv') || r.includes('virtual env')) return 'Setting up Python environment'
  if (r.includes('npm install') || r.includes('node_modules')) return 'Installing frontend packages'
  if (r.includes('pip install') || r.includes('installing package')) return 'Installing backend packages'
  if (r.includes('scaffold') || r.includes('writing')) return 'Scaffolding project structure'
  if (r.includes('starting') && r.includes('server')) return 'Starting development server'
  if (r.includes('built') || r.includes('compiled') || r.includes('vite')) return 'Building frontend'
  if (r.includes('ready') || r.includes('running')) return 'Environment ready'
  return raw.length > 80 ? raw.slice(0, 80) + '…' : raw
}

function parseSystemPrompt(hrOutput: string, agentName: string): string {
  const blocks = hrOutput.split(/^---\s*$/m)
  for (const block of blocks) {
    if (!block.includes('SPAWN_AGENT:')) continue
    const nameMatch = block.match(/^name:\s*(.+)$/m)
    if (!nameMatch || nameMatch[1].trim() !== agentName) continue
    const promptStart = block.indexOf('system_prompt: |')
    if (promptStart === -1) continue
    return block.slice(promptStart + 'system_prompt: |'.length).replace(/^\n/, '').trimEnd()
  }
  return ''
}

interface PromptSection { heading: string; body: string }

function parsePromptSections(prompt: string): PromptSection[] {
  if (!prompt) return []
  const lines = prompt.split('\n')
  const sections: PromptSection[] = []
  let current: PromptSection | null = null
  for (const line of lines) {
    const isHeading = /^(You are|Your role|Your responsibilities|Tech stack|Output format|Coding standards|Project context|Tools|Languages|Frameworks)/i.test(line.trim())
    if (isHeading && line.trim()) {
      if (current) sections.push(current)
      current = { heading: '', body: line.trim() }
    } else if (current) {
      current.body += '\n' + line
    } else {
      current = { heading: '', body: line }
    }
  }
  if (current) sections.push(current)
  return sections.filter(s => s.body.trim()).map(s => ({ heading: s.heading, body: s.body.trim() }))
}
