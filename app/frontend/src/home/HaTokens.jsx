// Tokens for Home Assistant: make one (its secret is shown once, here, and
// never again), see which exist, and delete one to cut that access off.
import React, { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { homeApi } from './api.js'
import { Sheet, errorText } from './partyKit.jsx'

export default function HaTokens() {
  const { t, language } = useI18n()
  const [tokens, setTokens] = useState(null)
  const [label, setLabel] = useState('')
  const [made, setMade] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [doomed, setDoomed] = useState(null)
  const [copied, setCopied] = useState(false)

  const load = useCallback(() => {
    homeApi.haTokens()
      .then((r) => setTokens(Array.isArray(r) ? r : r?.tokens || []))
      .catch((exc) => { setTokens([]); setError(errorText(t, exc)) })
  }, [t])
  useEffect(() => { load() }, [load])

  async function create(event) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const r = await homeApi.createHaToken(label.trim())
      setMade({ id: r?.id, token: r?.token || '', label: label.trim() })
      setLabel('')
      load()
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy(false)
  }

  async function remove() {
    const target = doomed
    setDoomed(null)
    setBusy(true)
    try {
      await homeApi.deleteHaToken(target.id)
      if (made?.id === target.id) setMade(null)
      load()
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy(false)
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(made.token)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* selectable below */ }
  }

  const when = (seconds) => {
    if (!seconds) return ''
    try { return new Date(seconds * 1000).toLocaleDateString(language || undefined) } catch { return '' }
  }

  return (
    <section className="hp-section">
      <h1 className="hp-h1">{t('home.ha.title')}</h1>
      <p className="hp-muted">{t('home.ha.help')}</p>

      <form className="hp-inline" onSubmit={create}>
        <label className="hp-label hp-sr" htmlFor="hp-ha-label">{t('home.ha.label')}</label>
        <input id="hp-ha-label" className="hp-input" value={label} maxLength={60}
               placeholder={t('home.ha.label')} onChange={(e) => setLabel(e.target.value)} />
        <button type="submit" className="hp-btn hp-btn--primary" disabled={busy}>{t('home.ha.create')}</button>
      </form>

      {made?.token && (
        <div className="hp-card hp-secret" role="status">
          <p className="hp-text"><strong>{t('home.ha.once')}</strong></p>
          <code className="hp-select hp-code">{made.token}</code>
          <div className="hp-row">
            <button type="button" className="hp-btn" onClick={copy}>{copied ? t('home.guest.copied') : t('home.ha.copy')}</button>
            <button type="button" className="hp-btn hp-btn--ghost" onClick={() => setMade(null)}>{t('home.ha.done')}</button>
          </div>
        </div>
      )}

      {error && <p className="hp-bad" role="alert">{error}</p>}

      {tokens === null && <p className="hp-muted">{t('home.loading')}</p>}
      {tokens && !tokens.length && <p className="hp-muted">{t('home.ha.none')}</p>}
      <ul className="hp-list">
        {(tokens || []).map((token) => (
          <li key={token.id} className="hp-card hp-row">
            <span className="hp-grow">
              <span className="hp-block">{token.label || t('home.ha.unnamed')}</span>
              {token.created ? <span className="hp-muted hp-small">{t('home.ha.created', { date: when(token.created) })}</span> : null}
            </span>
            <button type="button" className="hp-btn hp-btn--danger" disabled={busy} onClick={() => setDoomed(token)}>{t('home.ha.delete')}</button>
          </li>
        ))}
      </ul>

      {doomed && (
        <Sheet
          title={t('home.ha.deleteTitle', { name: doomed.label || t('home.ha.unnamed') })}
          onClose={() => setDoomed(null)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setDoomed(null)}>{t('common.cancel')}</button>
              <button type="button" className="hp-btn hp-btn--danger" onClick={remove}>{t('home.ha.delete')}</button>
            </>
          )}
        >
          <p className="hp-text">{t('home.ha.deleteHelp')}</p>
        </Sheet>
      )}
    </section>
  )
}
