// The hub: parties that are running, and for the household its presets
// (tap one, then Start) and the way to a new party, the guest pass and Home
// Assistant's tokens.
import React, { useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import Art from '../components/Art.jsx'
import { homeApi } from './api.js'
import { useMe } from './useMe.js'
import { PartyLink, Sheet, errorText, go, partyNowPlaying, partyRooms, useMoveAsk } from './partyKit.jsx'
import { announceTvResult } from './tvKit.js'

function PartyCard({ party, me, reload }) {
  const { t } = useI18n()
  const { zones } = useSystem()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const now = partyNowPlaying(party, zones, t)
  const rooms = partyRooms(party, zones)
  const member = (party.members || []).some((m) => m.id === me?.id) || party.owner === me?.id
  const paused = !(party.speakers || []).length

  async function open() {
    setBusy(true)
    setError('')
    try {
      if (!member) {
        await homeApi.joinParty(party.id)
        // Now a member, so the queue can name this guest's songs as theirs.
        reload?.()
      }
      go(`/party/p/${encodeURIComponent(party.id)}`)
    } catch (exc) {
      setError(errorText(t, exc))
      setBusy(false)
    }
  }

  return (
    <li className="hp-card hp-party">
      <div className="hp-party__art"><Art src={now.art} size={56} /></div>
      <div className="hp-party__text">
        <div className="hp-party__name">{party.name}</div>
        <div className="hp-muted hp-ellipsis">
          {paused ? t('home.party.paused') : rooms.join(', ')}
        </div>
        {now.line1 && <div className="hp-ellipsis">{now.line1}{now.line2 ? ` · ${now.line2}` : ''}</div>}
        <div className="hp-muted hp-small">{t('home.party.owner', { name: party.owner_name || '' })}</div>
        {error && <div className="hp-bad" role="alert">{error}</div>}
      </div>
      <button type="button" className="hp-btn hp-btn--primary" disabled={busy} onClick={open}>
        {member ? t('home.party.open') : t('home.party.join')}
      </button>
    </li>
  )
}

function PresetSheet({ preset, onClose }) {
  const { t } = useI18n()
  const { zones } = useSystem()
  const [dialog, runMove] = useMoveAsk()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const rooms = (preset.speakers || []).map((uuid) => zones[uuid]?.name || uuid)

  async function start() {
    setBusy(true)
    setError('')
    try {
      const party = await runMove((extra) => homeApi.startPreset(preset.id, extra))
      if (!party) { setBusy(false); return }
      if (party.tv_error) announceTvResult({ party: party.id, ok: false, error: party.tv_error })
      go(`/party/p/${encodeURIComponent(party.id)}`)
    } catch (exc) {
      setError(errorText(t, exc))
      setBusy(false)
    }
  }

  return (
    <>
      <Sheet
        title={preset.name}
        onClose={onClose}
        actions={(
          <>
            <button type="button" className="hp-btn" onClick={onClose}>{t('common.cancel')}</button>
            <button type="button" className="hp-btn hp-btn--primary" disabled={busy} onClick={start} autoFocus>{t('home.preset.start')}</button>
          </>
        )}
      >
        <p className="hp-text">{rooms.join(', ')}</p>
        {preset.music?.title && <p className="hp-muted">{t('home.preset.music', { title: preset.music.title })}</p>}
        {error && <p className="hp-bad" role="alert">{error}</p>}
      </Sheet>
      {dialog}
    </>
  )
}

export default function PartyList({ data, isUser }) {
  const { t } = useI18n()
  const { zones } = useSystem()
  const me = useMe()
  const [picked, setPicked] = useState(null)
  const parties = data.parties || []
  const presets = data.presets || []

  return (
    <>
      <section className="hp-section">
        <h1 className="hp-h1">{t('home.hub.running')}</h1>
        {data.loading && !parties.length && <p className="hp-muted">{t('home.loading')}</p>}
        {!data.loading && !parties.length && (
          <p className="hp-muted">{isUser ? t('home.hub.noneUser') : t('home.hub.noneGuest')}</p>
        )}
        <ul className="hp-list">
          {parties.map((party) => <PartyCard key={party.id} party={party} me={me.principal} reload={data.reload} />)}
        </ul>
      </section>

      {isUser && (
        <>
          <section className="hp-section">
            <div className="hp-row">
              <h2 className="hp-h2">{t('home.hub.presets')}</h2>
              <PartyLink to="/party/preset/new" className="hp-btn hp-btn--ghost">{t('home.preset.new')}</PartyLink>
            </div>
            {!presets.length && <p className="hp-muted">{t('home.hub.noPresets')}</p>}
            <ul className="hp-grid">
              {presets.map((preset) => (
                <li key={preset.id} className="hp-card hp-preset">
                  <button type="button" className="hp-preset__go" onClick={() => setPicked(preset)}>
                    <span className="hp-preset__name">{preset.name}</span>
                    <span className="hp-muted hp-small hp-ellipsis">
                      {(preset.speakers || []).map((uuid) => zones[uuid]?.name).filter(Boolean).join(', ')}
                    </span>
                  </button>
                  <PartyLink to={`/party/preset/${encodeURIComponent(preset.id)}`} className="hp-preset__edit hp-small">{t('home.preset.edit')}</PartyLink>
                </li>
              ))}
            </ul>
          </section>

          <section className="hp-section hp-actions">
            <PartyLink to="/party/new" className="hp-btn hp-btn--primary hp-btn--wide">{t('home.hub.newParty')}</PartyLink>
            <PartyLink to="/party/guest-pass" className="hp-btn hp-btn--wide">{t('home.hub.guestPass')}</PartyLink>
            <PartyLink to="/party/ha-tokens" className="hp-btn hp-btn--wide">{t('home.hub.haTokens')}</PartyLink>
            <PartyLink to="/party/tvs" className="hp-btn hp-btn--wide">{t('home.hub.tvs')}</PartyLink>
            <PartyLink to="/party/displays" className="hp-btn hp-btn--wide">{t('home.hub.displays')}</PartyLink>
            <PartyLink to="/party/windows-sound" className="hp-btn hp-btn--wide">{t('home.hub.windowsSound')}</PartyLink>
            <PartyLink to="/party/airplay" className="hp-btn hp-btn--wide">{t('home.hub.airplay')}</PartyLink>
          </section>
        </>
      )}
      {picked && <PresetSheet preset={picked} onClose={() => setPicked(null)} />}
    </>
  )
}
