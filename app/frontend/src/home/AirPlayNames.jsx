// AirPlay names: how to send sound from an iPhone, iPad or Mac, and the
// names Sonora shows in the AirPlay list right now (one per running party,
// one per room). Copies without AirPlay say so.
import React, { useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import { homeApi } from './api.js'
import { errorText } from './partyKit.jsx'

export default function AirPlayNames() {
  const { t } = useI18n()
  const { partiesEpoch } = useSystem()
  // null while loading; {missing: true} when the server has no AirPlay.
  const [state, setState] = useState(null)

  useEffect(() => {
    let live = true
    homeApi.airplayReceivers()
      .then((list) => { if (live) setState({ list: Array.isArray(list) ? list : [] }) })
      .catch((exc) => {
        if (!live) return
        setState(exc?.status === 404 ? { missing: true } : { error: errorText(t, exc) })
      })
    return () => { live = false }
  }, [partiesEpoch, t])

  const parties = (state?.list || []).filter((r) => r?.target?.kind === 'party')
  const rooms = (state?.list || []).filter((r) => r?.target?.kind !== 'party')
  const names = (list) => (
    <ul className="hp-list">
      {list.map((r) => (
        <li key={`${r.target?.kind}:${r.target?.id}`} className="hp-card"><strong className="hp-select">{r.name}</strong></li>
      ))}
    </ul>
  )

  return (
    <section className="hp-section">
      <h1 className="hp-h1">{t('home.airplay.title')}</h1>
      <p className="hp-text">{t('home.airplay.help')}</p>
      {state === null && <p className="hp-muted">{t('home.loading')}</p>}
      {state?.missing && <p className="hp-muted">{t('home.airplay.unavailable')}</p>}
      {state?.error && <p className="hp-bad" role="alert">{state.error}</p>}
      {state?.list && !state.list.length && <p className="hp-muted">{t('home.airplay.none')}</p>}
      {parties.length > 0 && (
        <>
          <h2 className="hp-h2">{t('home.airplay.parties')}</h2>
          {names(parties)}
        </>
      )}
      {rooms.length > 0 && (
        <>
          <h2 className="hp-h2">{t('home.airplay.rooms')}</h2>
          {names(rooms)}
        </>
      )}
    </section>
  )
}
