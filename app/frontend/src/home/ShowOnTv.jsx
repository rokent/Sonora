// "Show on TV": a button and the sheet behind it, for a running party.
import React, { useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { homeApi } from './api.js'
import { Sheet, errorText } from './partyKit.jsx'
import { TV_MODES, modeKey } from './tvKit.js'
import { PartyLink } from './partyKit.jsx'

export default function ShowOnTv({ party }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [tvs, setTvs] = useState(null)
  const [tvId, setTvId] = useState('')
  const [mode, setMode] = useState('cover')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  useEffect(() => {
    if (!open) return undefined
    let live = true
    setError('')
    setDone(false)
    homeApi.tvs()
      .then((r) => {
        if (!live) return
        const list = r?.tvs || []
        setTvs(list)
        setTvId((prev) => (list.some((tv) => tv.id === prev) ? prev : list[0]?.id || ''))
        const first = list.find((tv) => tv.id === (list[0]?.id))
        if (first?.mode_default) setMode(first.mode_default)
      })
      .catch((exc) => { if (live) { setTvs([]); setError(errorText(t, exc)) } })
    return () => { live = false }
  }, [open, t])

  async function show() {
    if (!tvId || busy) return
    setBusy(true)
    setError('')
    setDone(false)
    try {
      await homeApi.showOnTv(tvId, { party_id: party.id, mode })
      setDone(true)
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy(false)
  }

  return (
    <>
      <button type="button" className="hp-btn hp-btn--wide" onClick={() => setOpen(true)}>{t('home.tv.showOnTv')}</button>
      {open && (
        <Sheet
          title={t('home.tv.showOnTv')}
          onClose={() => setOpen(false)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setOpen(false)}>{t('common.close')}</button>
              {!!tvs?.length && (
                <button type="button" className="hp-btn hp-btn--primary" disabled={busy || !tvId} onClick={show}>
                  {busy ? t('home.tv.showing') : t('home.tv.show')}
                </button>
              )}
            </>
          )}
        >
          {tvs && !tvs.length && (
            <p className="hp-text">{t('home.tv.none')} <PartyLink to="/party/tvs">{t('home.tv.manage')}</PartyLink></p>
          )}
          {!!tvs?.length && (
            <>
              <label className="hp-label" htmlFor="hp-showtv-tv">{t('home.tv.which')}</label>
              <select id="hp-showtv-tv" className="hp-input" value={tvId} onChange={(e) => setTvId(e.target.value)}>
                {tvs.map((tv) => <option key={tv.id} value={tv.id}>{tv.name}</option>)}
              </select>
              <label className="hp-label" htmlFor="hp-showtv-mode">{t('home.tv.mode')}</label>
              <select id="hp-showtv-mode" className="hp-input" value={mode} onChange={(e) => setMode(e.target.value)}>
                {TV_MODES.map((m) => <option key={m} value={m}>{t(modeKey(m))}</option>)}
              </select>
            </>
          )}
          {!tvs && <p className="hp-muted">{t('home.loading')}</p>}
          {done && <p className="hp-text" role="status">{t('home.tv.sent')}</p>}
          {error && <p className="hp-bad" role="alert">{error}</p>}
        </Sheet>
      )}
    </>
  )
}
