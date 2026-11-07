// The household's Samsung TVs: add one, let this app in ("pair"), try it,
// remove it.
import React, { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { homeApi } from './api.js'
import { Sheet, errorText } from './partyKit.jsx'

export default function TvSettings() {
  const { t } = useI18n()
  const [tvs, setTvs] = useState(null)
  const [form, setForm] = useState({ name: '', host: '', mac: '' })
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [removing, setRemoving] = useState(null)

  const load = useCallback(() => homeApi.tvs()
    .then((r) => setTvs(r?.tvs || []))
    .catch((exc) => { setTvs([]); setError(errorText(t, exc)) }), [t])
  useEffect(() => { load() }, [load])

  async function run(key, call, done) {
    setBusy(key)
    setError('')
    setInfo('')
    try {
      await call()
      if (done) setInfo(done)
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy('')
    load()
  }

  // A test needs something to show: the first running party.
  async function showNow(tv) {
    const found = await homeApi.parties()
    const party = (found?.parties || [])[0]
    if (!party) throw Object.assign(new Error('no party'), { kind: 'invalid', body: { error: 'invalid', message: t('home.tv.testNeedsParty') } })
    await homeApi.showOnTv(tv.id, { party_id: party.id })
  }

  async function add(event) {
    event.preventDefault()
    if (!form.name.trim() || !form.host.trim() || busy) return
    const body = { name: form.name.trim(), host: form.host.trim(), ...(form.mac.trim() ? { mac: form.mac.trim() } : {}) }
    await run('add', () => homeApi.addTv(body))
    setForm({ name: '', host: '', mac: '' })
  }

  const field = (key) => ({ value: form[key], onChange: (e) => setForm((prev) => ({ ...prev, [key]: e.target.value })) })

  return (
    <section className="hp-section">
      <h1 className="hp-h1">{t('home.tv.title')}</h1>
      <p className="hp-muted">{t('home.tv.help')}</p>
      {tvs && !tvs.length && <p className="hp-muted">{t('home.tv.none')}</p>}
      <ul className="hp-list">
        {(tvs || []).map((tv) => (
          <li key={tv.id} className="hp-card hp-stack">
            <div className="hp-party__name">{tv.name}</div>
            <div className="hp-muted hp-small">
              {tv.host}{tv.mac ? ` · ${tv.mac}` : ''} · {tv.paired ? t('home.tv.paired') : t('home.tv.notPaired')}
            </div>
            {!tv.paired && <p className="hp-muted hp-small">{t('home.tv.pairHelp')}</p>}
            <div className="hp-actions hp-actions--row">
              <button type="button" className="hp-btn hp-btn--small" disabled={!!busy}
                      onClick={() => run(`pair:${tv.id}`, () => homeApi.pairTv(tv.id), t('home.tv.pairedDone', { name: tv.name }))}>
                {busy === `pair:${tv.id}` ? t('home.tv.pairing') : t('home.tv.pair')}
              </button>
              <button type="button" className="hp-btn hp-btn--small" disabled={!!busy}
                      onClick={() => run(`test:${tv.id}`, () => showNow(tv), t('home.tv.shown', { name: tv.name }))}>
                {t('home.tv.showNow')}
              </button>
              <button type="button" className="hp-btn hp-btn--small hp-btn--danger" disabled={!!busy} onClick={() => setRemoving(tv)}>
                {t('home.tv.remove')}
              </button>
            </div>
          </li>
        ))}
      </ul>
      {info && <p className="hp-text" role="status">{info}</p>}
      {error && <p className="hp-bad" role="alert">{error}</p>}

      <h2 className="hp-h2">{t('home.tv.add')}</h2>
      <form className="hp-stack" onSubmit={add}>
        <label className="hp-label" htmlFor="hp-tv-name">{t('home.tv.name')}</label>
        <input id="hp-tv-name" className="hp-input" maxLength={60} {...field('name')} />
        <label className="hp-label" htmlFor="hp-tv-host">{t('home.tv.host')}</label>
        <input id="hp-tv-host" className="hp-input" inputMode="decimal" autoCapitalize="none" autoCorrect="off"
               placeholder="192.168.1.50" {...field('host')} />
        <label className="hp-label" htmlFor="hp-tv-mac">{t('home.tv.mac')}</label>
        <input id="hp-tv-mac" className="hp-input" autoCapitalize="none" autoCorrect="off"
               placeholder="aa:bb:cc:dd:ee:ff" {...field('mac')} />
        <p className="hp-muted hp-small">{t('home.tv.macHelp')}</p>
        <button type="submit" className="hp-btn hp-btn--primary hp-btn--wide"
                disabled={!!busy || !form.name.trim() || !form.host.trim()}>{t('home.tv.addButton')}</button>
      </form>

      {removing && (
        <Sheet
          title={t('home.tv.removeTitle', { name: removing.name })}
          onClose={() => setRemoving(null)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setRemoving(null)}>{t('common.cancel')}</button>
              <button type="button" className="hp-btn hp-btn--danger" onClick={() => { const tv = removing; setRemoving(null); run('rm', () => homeApi.deleteTv(tv.id)) }}>
                {t('home.tv.remove')}
              </button>
            </>
          )}
        >
          <p className="hp-text">{t('home.tv.removeHelp')}</p>
        </Sheet>
      )}
    </section>
  )
}
