import { useState, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './index.css'
import App from './App'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import { AuthProvider, useAuth } from './lib/AuthContext'
import { setAuthHeaders } from './api/client'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 2000 } },
})

// root router — decides which view to show based on auth state
function Root() {
  const { user, loading } = useAuth()

  // keep api client auth headers in sync with the current user
  useEffect(() => {
    setAuthHeaders(user?.id ?? null, user?.email ?? null)
  }, [user])
  // activeProject: null = show dashboard, string = show war room for that project
  const [activeProject, setActiveProject] = useState<string | null>(() => {
    // restore from URL on first load
    return new URLSearchParams(window.location.search).get('p')
  })
  const [newProject, setNewProject] = useState(false)

  if (loading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', background: 'var(--bg)' }}>
        <span className="spin" style={{ width: 32, height: 32, borderWidth: 3 }} />
      </div>
    )
  }

  if (!user) return <LoginPage />

  // show war room if a project is active or user clicked "new project"
  if (activeProject || newProject) {
    return (
      <App
        initialProjectId={activeProject}
        onBackToDashboard={() => { setActiveProject(null); setNewProject(false); window.history.replaceState(null, '', '/') }}
        savedCreds={user.user_metadata?.saved_creds || {}}
      />
    )
  }

  return (
    <DashboardPage
      onOpenProject={id => { setActiveProject(id); window.history.replaceState(null, '', `/?p=${id}`) }}
      onNewProject={() => setNewProject(true)}
    />
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <AuthProvider>
      <Root />
    </AuthProvider>
  </QueryClientProvider>,
)
