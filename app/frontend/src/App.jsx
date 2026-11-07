import React, { Suspense, lazy, useEffect, useState } from 'react'
import { useTheme } from './lib/theme.jsx'
import { useSystem } from './lib/store.jsx'
import Splash from './components/Splash.jsx'
import LayoutShell from './parts/LayoutShell.jsx'
// Loaded on demand, so the party page (and its QR library) stays out of
// the themes' bundle.
const PartyApp = lazy(() => import('./home/PartyApp.jsx'))
import { withBase } from './lib/base.js'
import { useMe } from './home/useMe.js'
import { controllerRedirect, showPartiesButton } from './home/access.js'
import { useI18n } from './i18n/index.jsx'
import './home/home.css'

// How long the first load may take before "Looking for speakers" is shown.
// The snapshot normally arrives well inside a second, and flashing that
// screen on every page load read as a fault; a system
// that really is still being discovered gets the screen after this pause.
const LOOKING_GRACE_MS = 1500

export default function App() {
  const me = useMe()
  const isDisplay = me.principal?.kind === 'display'
  // A TV screen signed in with a display link has TV mode and nothing else.
  useEffect(() => { if (isDisplay) window.location.replace(withBase('/tv')) }, [isDisplay])
  // Until we know who is looking, a guest must not glimpse the theme shell.
  if (me.loading) return <div style={{ minHeight: '100%', background: 'var(--t-bg, #f6f6f7)' }} />
  if (isDisplay) return null
  // A guest has the party screens and nothing else.
  if (me.principal?.kind === 'guest') return <Suspense fallback={null}><PartyApp /></Suspense>
  // A TV's display session has no controller: it shows TV mode.
  const away = controllerRedirect({ principalKind: me.principal?.kind || null })
  if (away) return <Redirect to={away} />
  return <Household partiesLink={showPartiesButton({ auth: me.auth, principalKind: me.principal?.kind || null })} />
}

function Redirect({ to }) {
  useEffect(() => { window.location.replace(withBase(to)) }, [to])
  return <div style={{ minHeight: '100%', background: 'var(--t-bg, #f6f6f7)' }} />
}

function PartiesButton() {
  const { t } = useI18n()
  return <a className="home-parties-button" href={withBase('/party')}>{t('home.parties.button')}</a>
}

function Household({ partiesLink }) {
  const { theme } = useTheme()
  const { loading, error, zoneList } = useSystem()
  const [pastGrace, setPastGrace] = useState(false)
  useEffect(() => {
    if (!loading) return undefined
    const timer = setTimeout(() => setPastGrace(true), LOOKING_GRACE_MS)
    return () => clearTimeout(timer)
  }, [loading])

  // Every theme renders the same failure and empty states, because a person
  // whose speakers have not appeared needs the same explanation regardless of
  // which skin they picked.
  if (error && zoneList.length === 0) {
    return <Splash kind="error" detail={error} />
  }
  if (loading) {
    // The theme's own background and nothing else until the grace period
    // is over; the interface then appears without a screen in between.
    return pastGrace ? <Splash kind="loading" /> : <div style={{ minHeight: '100%', background: 'var(--t-bg, #f6f6f7)' }} />
  }
  if (zoneList.length === 0) return <Splash kind="empty" />

  // A theme installed from a file always carries its own layout, since a
  // package cannot bring code; LayoutShell draws it and supplies the overlays a
  // package cannot. Resolved here rather than in lib/theme.jsx, which would
  // have to import it and close an import cycle back onto itself.
  const Shell = theme.Shell
  return (
    <>
      {theme.layout ? <LayoutShell layout={theme.layout} theme={theme} /> : <Shell />}
      {partiesLink && <PartiesButton />}
    </>
  )
}
