// The party page: the hub at /party and the screens under it. Guests see
// this and nothing else; signed-in people reach it from the controller.
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import { withBase } from '../lib/base.js'
import { homeApi } from './api.js'
import { useMe } from './useMe.js'
import { PartyLink, errorText, go, usePartyPath } from './partyKit.jsx'
import PartyList from './PartyList.jsx'
import PartyView from './PartyView.jsx'
import StartParty from './StartParty.jsx'
import PresetEditor from './PresetEditor.jsx'
import GuestPass from './GuestPass.jsx'
import HaTokens from './HaTokens.jsx'
import TvSettings from './TvSettings.jsx'
import DisplayLinks from './DisplayLinks.jsx'
import './home.css'

/** Parties and presets, refetched whenever the server says they changed. */
function useParties() {
  const { partiesEpoch } = useSystem()
  const [data, setData] = useState({ loading: true, parties: [], presets: [], error: null })
  const latest = useRef(0)
  // Resolves once the list is in, so a screen that expected a party it
  // cannot find yet can ask again before saying it has gone.
  const reload = useCallback(() => {
    const ticket = ++latest.current
    return homeApi.parties()
      .then((r) => { if (ticket === latest.current) setData({ loading: false, parties: r?.parties || [], presets: r?.presets || [], error: null }) })
      .catch((error) => { if (ticket === latest.current) setData((prev) => ({ ...prev, loading: false, error })) })
  }, [])
  useEffect(() => { reload() }, [partiesEpoch, reload])
  return { ...data, reload }
}

const TV_RESULT_MS = 8000

/** What became of a TV show that ran in the background, for a few seconds. */
function useTvResult(isUser) {
  const { lastTvResult } = useSystem()
  const opened = useRef(Date.now())
  const [shown, setShown] = useState(null)
  useEffect(() => {
    if (!isUser || !lastTvResult || lastTvResult.at < opened.current) return undefined
    setShown(lastTvResult)
    const timer = setTimeout(() => setShown(null), TV_RESULT_MS)
    return () => clearTimeout(timer)
  }, [lastTvResult, isUser])
  return shown
}

export default function PartyApp() {
  const { t } = useI18n()
  const me = useMe()
  const path = usePartyPath()
  const data = useParties()
  const isGuest = me.principal?.kind === 'guest'
  // With sign-in switched off everyone is the household.
  const isUser = me.principal?.kind === 'user' || (!me.loading && !me.auth && !isGuest)
  const tvResult = useTvResult(isUser)

  const sub = path.replace(/^\/party/, '') || '/'
  let screen = null
  let back = sub !== '/'
  if (me.loading) {
    back = false
  } else if (sub.startsWith('/p/')) {
    screen = <PartyView id={decodeURIComponent(sub.slice(3))} data={data} isUser={isUser} />
  } else if (sub === '/new' && isUser) {
    screen = <StartParty data={data} />
  } else if (sub === '/preset/new' && isUser) {
    screen = <PresetEditor data={data} />
  } else if (sub.startsWith('/preset/') && isUser) {
    screen = <PresetEditor data={data} presetId={decodeURIComponent(sub.slice(8))} />
  } else if (sub === '/guest-pass' && isUser) {
    screen = <GuestPass />
  } else if (sub === '/ha-tokens' && isUser) {
    screen = <HaTokens />
  } else if (sub === '/tvs' && isUser) {
    screen = <TvSettings />
  } else if (sub === '/displays' && isUser) {
    screen = <DisplayLinks data={data} />
  } else {
    back = false
    screen = <PartyList data={data} isUser={isUser} />
  }

  return (
    <div className="hp">
      <header className="hp-top">
        {back
          ? <button type="button" className="hp-top__back" onClick={() => go('/party')}>{t('common.back')}</button>
          : (isUser ? <a className="hp-top__back" href={withBase('/')}>{t('home.nav.controller')}</a> : <span />)}
        <PartyLink to="/party" className="hp-top__title">{t('home.parties.title')}</PartyLink>
        <span className="hp-top__who">{me.principal?.name || ''}</span>
      </header>
      <main className="hp-main">
        {tvResult && (
          <div className={tvResult.ok ? 'hp-banner' : 'hp-banner hp-alert'} role={tvResult.ok ? 'status' : 'alert'}>
            {tvResult.ok ? t('home.tv.resultOk') : errorText(t, { body: { error: tvResult.error } })}
          </div>
        )}
        {data.error && !data.loading && <p className="hp-alert" role="alert">{errorText(t, data.error)}</p>}
        {screen}
      </main>
    </div>
  )
}
