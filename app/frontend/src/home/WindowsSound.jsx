// Windows sound (the Scream network sound card): each Windows PC that sends
// sound shows up here by its address, and a household member picks where it
// plays: a running party, a room, or nowhere. Sound from a PC with no place
// set is ignored.
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import { homeApi } from './api.js'
import { errorText, roomsBySystem } from './partyKit.jsx'

const POLL_MS = 5000

const targetValue = (target) => (target ? `${target.kind}:${target.id}` : '')

function parseValue(value) {
  const at = value.indexOf(':')
  return at > 0 ? { kind: value.slice(0, at), id: value.slice(at + 1) } : null
}

export default function WindowsSound({ data }) {
  const { t } = useI18n()
  const { zones, households } = useSystem()
  const [sources, setSources] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const systems = useMemo(() => roomsBySystem(households, zones), [households, zones])
  const parties = (data.parties || []).filter((p) => (p.speakers || []).length)

  const load = useCallback(() => homeApi.screamSources()
    .then((r) => { setSources(Array.isArray(r) ? r : []); setError('') })
    .catch((exc) => { setSources((prev) => prev || []); setError(errorText(t, exc)) }), [t])
  useEffect(() => {
    load()
    const timer = setInterval(load, POLL_MS)
    return () => clearInterval(timer)
  }, [load])

  async function map(ip, value) {
    setBusy(ip)
    setError('')
    try {
      await homeApi.mapScream(ip, parseValue(value))
      await load()
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy('')
  }

  const heard = (seconds) => {
    if (!seconds) return t('home.windows.notHeard')
    const ago = Math.max(0, Math.round(Date.now() / 1000 - seconds))
    if (ago < 60) return t('home.windows.heardNow')
    if (ago < 3600) return t('home.windows.heardMinutes', { count: Math.round(ago / 60) })
    return t('home.windows.heardHours', { count: Math.round(ago / 3600) })
  }

  const known = (value) => !value || parties.some((p) => `party:${p.id}` === value)
    || systems.some((s) => s.rooms.some((z) => `zone:${z.uuid}` === value))

  return (
    <section className="hp-section">
      <h1 className="hp-h1">{t('home.windows.title')}</h1>
      <p className="hp-muted">{t('home.windows.help')}</p>
      {error && <p className="hp-bad" role="alert">{error}</p>}
      {sources === null && <p className="hp-muted">{t('home.loading')}</p>}
      {sources && !sources.length && <p className="hp-muted">{t('home.windows.none')}</p>}
      <ul className="hp-list">
        {(sources || []).map((source) => {
          const value = targetValue(source.target)
          const selectId = `hp-scream-${source.ip.replace(/[^a-zA-Z0-9]/g, '-')}`
          return (
            <li key={source.ip} className="hp-card hp-stack">
              <span className="hp-block"><strong>{source.ip}</strong></span>
              <span className="hp-muted hp-small hp-block">
                {heard(source.last_seen)}
                {source.format ? ` · ${Math.round(source.format.rate / 100) / 10} kHz, ${source.format.channels} ch` : ''}
              </span>
              <label className="hp-label" htmlFor={selectId}>{t('home.windows.playsOn')}</label>
              <select id={selectId} className="hp-input" value={value} disabled={busy === source.ip}
                      onChange={(e) => map(source.ip, e.target.value)}>
                <option value="">{t('home.windows.nowhere')}</option>
                {!known(value) && <option value={value}>{t('home.windows.gone')}</option>}
                {parties.length > 0 && (
                  <optgroup label={t('home.windows.parties')}>
                    {parties.map((p) => <option key={p.id} value={`party:${p.id}`}>{p.name}</option>)}
                  </optgroup>
                )}
                {systems.filter((s) => s.rooms.length).map((s) => (
                  <optgroup key={s.id} label={t('home.rooms.system', { system: s.label })}>
                    {s.rooms.map((z) => <option key={z.uuid} value={`zone:${z.uuid}`}>{z.name}</option>)}
                  </optgroup>
                ))}
              </select>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
