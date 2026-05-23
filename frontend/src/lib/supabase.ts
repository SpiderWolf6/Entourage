// supabase client — initialized from env vars injected at build time.
// VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY must be set in .env (local)
// or as fly.io secrets (production).

import { createClient } from '@supabase/supabase-js'

const supabaseUrl  = import.meta.env.VITE_SUPABASE_URL  as string
const supabaseAnon = import.meta.env.VITE_SUPABASE_ANON_KEY as string

if (!supabaseUrl || !supabaseAnon) {
  console.warn('Supabase env vars not set — auth will be unavailable.')
}

export const supabase = createClient(supabaseUrl || '', supabaseAnon || '', {
  auth: {
    detectSessionInUrl: true,
    persistSession: true,
    autoRefreshToken: true,
  }
})

export type SupabaseUser = {
  id: string
  email: string
  full_name: string
  avatar_url?: string
}
