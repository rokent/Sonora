// A new party: a name, the rooms (one system only), a starting volume for
// each, then Start. A room in another party asks before it moves.
import React, { useMemo, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import { homeApi } from './api.js'
import { useMe } from './useMe.js'
import { RoomChecklist, VolumeSlider, errorText, go, holders, roomsBySystem, useMoveAsk } from './partyKit.jsx'

export default function StartParty({ data }) {
  const { t } = useI18n()
  const { zones, households } = useSystem()
  const me = useMe()
  const systems = useMemo(() => roomsBySystem(households, zones), [households, zones])
  const held = useMemo(() => holders(data.parties), [data.parties])
  const [name, setName] = useState('')
  const [picked, setPicked] = useState([])
  const [volumes, setVolumes] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [dialog, runMove] = useMoveAsk()

  function toggle(uuid, on) {
    setPicked((prev) => (on ? [...prev, uuid] : prev.filter((u) => u !== uuid)))
    if (on && volumes[uuid] == null) setVolumes((prev) => ({ ...prev, [uuid]: zones[uuid]?.volume ?? 20 }))
  }

  async function start() {
    if (!picked.length || busy) return
    setBusy(true)
    setError('')
    try {
      const body = { speakers: picked, ...(name.trim() ? { name: name.trim() } : {}) }
      const party = await runMove((extra) => homeApi.startParty({ ...body, ...extra }))
      if (!party) { setBusy(false); return }
      // The start call takes no volumes; each room is set once it is in.
      await Promise.all(picked.filter((uuid) => volumes[uuid] != null && (party.speakers || []).includes(uuid))
        .map((uuid) => homeApi.volume(party.id, { zone: uuid, level: volumes[uuid] }).catch(() => null)))
      go(`/party/p/${encodeURIComponent(party.id)}`, { replace: true })
    } catch (exc) {
      setError(errorText(t, exc))
      setBusy(false)
    }
  }

  const defaultName = me.principal?.name ? t('home.start.defaultName', { name: me.principal.name }) : ''

  return (
    <section className="hp-section">
      <h1 className="hp-h1">{t('home.start.title')}</h1>
      <label className="hp-label" htmlFor="hp-party-name">{t('home.start.name')}</label>
      <input id="hp-party-name" className="hp-input" value={name} maxLength={60}
             placeholder={defaultName} onChange={(e) => setName(e.target.value)} />

      <h2 className="hp-h2">{t('home.start.rooms')}</h2>
      {!systems.some((s) => s.rooms.length) && <p className="hp-muted">{t('home.rooms.none')}</p>}
      <RoomChecklist systems={systems} picked={picked} onToggle={toggle} held={held} busy={busy} />

      {picked.length > 0 && (
        <>
          <h2 className="hp-h2">{t('home.start.volumes')}</h2>
          <ul className="hp-list">
            {picked.map((uuid) => (
              <li key={uuid} className="hp-vol">
                <span className="hp-vol__name">{zones[uuid]?.name || uuid}</span>
                <VolumeSlider
                  value={volumes[uuid]}
                  label={t('home.volume.room', { room: zones[uuid]?.name || '' })}
                  every={0}
                  onSend={(level) => setVolumes((prev) => ({ ...prev, [uuid]: level }))}
                />
                <span className="hp-vol__level">{volumes[uuid] ?? ''}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {error && <p className="hp-bad" role="alert">{error}</p>}
      <button type="button" className="hp-btn hp-btn--primary hp-btn--wide" disabled={!picked.length || busy} onClick={start}>
        {t('home.start.go')}
      </button>
      {dialog}
    </section>
  )
}
