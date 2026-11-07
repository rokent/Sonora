// One party: what it is playing and the buttons for it, the volume of the
// whole party and of each room, its queue with who added what, and adding
// songs. The household can also change its rooms, end it, or keep it as a
// preset.
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import Art from '../components/Art.jsx'
import * as Icon from '../components/Icons.jsx'
import { homeApi } from './api.js'
import AddSongs from './AddSongs.jsx'
import ShowOnTv from './ShowOnTv.jsx'
import { useMe } from './useMe.js'
import {
  RoomChecklist, Sheet, VolumeSlider, errorText, go, holders, partyNowPlaying, partyRooms,
  roomsBySystem, useMoveAsk,
} from './partyKit.jsx'

const NOTICE_MS = 6000

/** "Kitchen left the party", for a few seconds, when the server says so. */
function useLeftNotice(partyId) {
  const { lastPartyNotice, zones } = useSystem()
  const opened = useRef(Date.now())
  const [shown, setShown] = useState(null)
  useEffect(() => {
    const n = lastPartyNotice
    if (!n || n.party !== partyId || n.event !== 'left' || n.at < opened.current) return undefined
    setShown(n)
    const timer = setTimeout(() => setShown(null), NOTICE_MS)
    return () => clearTimeout(timer)
  }, [lastPartyNotice, partyId])
  return shown ? { ...shown, room: zones[shown.zone]?.name || '' } : null
}

function Queue({ party }) {
  const { t } = useI18n()
  const { queueEpochs, partiesEpoch } = useSystem()
  const lead = party.speakers?.[0] || ''
  const [queue, setQueue] = useState(null)
  const [error, setError] = useState('')
  const [nudge, setNudge] = useState(0)
  const [busy, setBusy] = useState(null)
  const epoch = queueEpochs[lead] || 0
  const me = useMe().principal
  // Who is a guest: the party's members say, and a guest viewer is one.
  const guestIds = useMemo(() => {
    const ids = new Set((party.members || []).filter((m) => m.kind === 'guest').map((m) => m.id))
    if (me?.kind === 'guest' && me.id) ids.add(me.id)
    return ids
  }, [party.members, me])

  useEffect(() => {
    if (!lead) { setQueue({ items: [], start: 0 }); return undefined }
    let live = true
    homeApi.partyQueue(party.id)
      .then((r) => { if (live) { setQueue(r); setError('') } })
      .catch((exc) => { if (live) setError(errorText(t, exc)) })
    return () => { live = false }
  }, [party.id, lead, epoch, partiesEpoch, nudge, t])

  async function remove(index) {
    setBusy(index)
    setError('')
    try {
      await homeApi.dequeue(party.id, index)
      setNudge((n) => n + 1)
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy(null)
  }

  if (!queue && !error) return <p className="hp-muted">{t('home.loading')}</p>
  const items = queue?.items || []
  const start = queue?.start || 0
  return (
    <>
      {error && <p className="hp-bad" role="alert">{error}</p>}
      {queue && !items.length && <p className="hp-muted">{t('home.queue.empty')}</p>}
      <ol className="hp-list hp-queue">
        {items.map((item, i) => {
          const index = start + i + 1
          const by = item.added_by
          const who = by ? (guestIds.has(by.id) ? t('home.queue.byGuest', { name: by.name }) : t('home.queue.by', { name: by.name })) : ''
          return (
            <li key={`${index}:${item.uri || item.id || ''}`} className="hp-song">
              <span className="hp-song__art"><Art src={item.art} size={40} fallback="track" /></span>
              <span className="hp-song__text">
                <span className="hp-ellipsis hp-block">{item.title}</span>
                <span className="hp-muted hp-small hp-ellipsis hp-block">
                  {[item.artist, who].filter(Boolean).join(' · ')}
                </span>
              </span>
              {item.can_remove && (
                <button type="button" className="hp-btn hp-btn--small hp-btn--ghost" disabled={busy === index}
                        aria-label={t('home.queue.remove', { title: item.title || '' })}
                        onClick={() => remove(index)}>
                  <Icon.Close />
                </button>
              )}
            </li>
          )
        })}
      </ol>
      {queue?.has_more && <p className="hp-muted hp-small">{t('home.queue.more', { count: (queue.total || 0) - items.length })}</p>}
    </>
  )
}

function Rooms({ party, parties }) {
  const { t } = useI18n()
  const { zones, households } = useSystem()
  const systems = useMemo(() => roomsBySystem(households, zones), [households, zones])
  const held = useMemo(() => holders(parties), [parties])
  const [dialog, runMove] = useMoveAsk()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function toggle(uuid, on) {
    setBusy(true)
    setError('')
    try {
      if (on) await runMove((extra) => homeApi.addSpeaker(party.id, { zone: uuid, ...extra }))
      else await homeApi.removeSpeaker(party.id, uuid)
    } catch (exc) {
      setError(errorText(t, exc))
    }
    setBusy(false)
  }

  return (
    <>
      <RoomChecklist systems={systems} picked={party.speakers || []} onToggle={toggle}
                     held={held} ownPartyId={party.id} busy={busy} />
      {error && <p className="hp-bad" role="alert">{error}</p>}
      {dialog}
    </>
  )
}

export default function PartyView({ id, data, isUser }) {
  const { t } = useI18n()
  const { zones } = useSystem()
  const party = (data.parties || []).find((p) => p.id === id) || null
  const notice = useLeftNotice(id)
  const [tab, setTab] = useState('queue')
  const [error, setError] = useState('')
  const [ask, setAsk] = useState(null)
  const [presetName, setPresetName] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  // A party just started may not be in the list yet: ask once more before
  // saying it has ended.
  const [confirmed, setConfirmed] = useState(null)
  const { reload } = data
  useEffect(() => {
    if (party || data.loading || confirmed === id) return undefined
    let live = true
    Promise.resolve(reload?.()).finally(() => { if (live) setConfirmed(id) })
    return () => { live = false }
  }, [party, data.loading, confirmed, id, reload])

  if (!party) {
    if (data.loading || confirmed !== id) return <p className="hp-muted">{t('home.loading')}</p>
    return (
      <section className="hp-section">
        <p className="hp-text">{t('home.party.gone')}</p>
        <button type="button" className="hp-btn hp-btn--wide" onClick={() => go('/party', { replace: true })}>{t('home.party.toHub')}</button>
      </section>
    )
  }

  const now = partyNowPlaying(party, zones, t)
  const lead = now.lead
  const speakers = party.speakers || []
  const paused = !speakers.length

  const act = async (fn) => {
    setError('')
    try { await fn() } catch (exc) { setError(errorText(t, exc)) }
  }

  async function endParty() {
    setBusy(true)
    try {
      await homeApi.endParty(party.id)
      setAsk(null)
      data.reload?.()
      go('/party', { replace: true })
    } catch (exc) {
      setError(errorText(t, exc))
      setBusy(false)
      setAsk(null)
    }
  }

  async function savePreset() {
    const name = presetName.trim()
    if (!name) return
    setBusy(true)
    try {
      const volumes = {}
      for (const uuid of speakers) if (Number.isFinite(zones[uuid]?.volume)) volumes[uuid] = zones[uuid].volume
      await homeApi.savePreset({ name, speakers, volumes })
      setAsk(null)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
      data.reload?.()
    } catch (exc) {
      setError(errorText(t, exc))
      setAsk(null)
    }
    setBusy(false)
  }

  return (
    <>
      {notice && <div className="hp-banner" role="status">{t('home.party.left', { room: notice.room || t('home.party.aRoom') })}</div>}
      {saved && <div className="hp-banner" role="status">{t('home.preset.saved')}</div>}

      <section className="hp-section">
        <h1 className="hp-h1">{party.name}</h1>
        <p className="hp-muted hp-small">
          {paused ? t('home.party.paused') : partyRooms(party, zones).join(', ')}
        </p>
      </section>

      <section className="hp-card hp-now">
        <div className="hp-now__art"><Art src={now.art} size={96} /></div>
        <div className="hp-now__text">
          <div className="hp-now__title hp-ellipsis">{now.line1 || t('home.party.nothing')}</div>
          {now.line2 && <div className="hp-muted hp-ellipsis">{now.line2}</div>}
          <div className="hp-transport">
            <button type="button" className="hp-icon" disabled={paused} aria-label={t('home.transport.previous')}
                    onClick={() => act(() => homeApi.transport(party.id, 'previous'))}><Icon.Prev /></button>
            <button type="button" className="hp-icon hp-icon--big" disabled={paused}
                    aria-label={now.playing ? t('home.transport.pause') : t('home.transport.play')}
                    onClick={() => act(() => homeApi.transport(party.id, now.playing ? 'pause' : 'play'))}>
              {now.playing ? <Icon.Pause /> : <Icon.Play />}
            </button>
            <button type="button" className="hp-icon" disabled={paused} aria-label={t('home.transport.next')}
                    onClick={() => act(() => homeApi.transport(party.id, 'next'))}><Icon.Next /></button>
          </div>
        </div>
      </section>
      {error && <p className="hp-bad" role="alert">{error}</p>}

      {!paused && (
        <section className="hp-section">
          <div className="hp-vol hp-vol--group">
            <span className="hp-vol__name">{t('home.volume.party')}</span>
            <VolumeSlider value={lead?.group_volume ?? lead?.volume} label={t('home.volume.party')}
                          onSend={(level) => homeApi.volume(party.id, { level })} />
            <span className="hp-vol__level">{lead?.group_volume ?? lead?.volume ?? ''}</span>
          </div>
          {speakers.length > 1 && (
            <ul className="hp-list">
              {speakers.map((uuid) => (
                <li key={uuid} className="hp-vol">
                  <span className="hp-vol__name">{zones[uuid]?.name || uuid}</span>
                  <VolumeSlider value={zones[uuid]?.volume} label={t('home.volume.room', { room: zones[uuid]?.name || '' })}
                                onSend={(level) => homeApi.volume(party.id, { zone: uuid, level })} />
                  <span className="hp-vol__level">{zones[uuid]?.volume ?? ''}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <div className="hp-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'queue'} className="hp-tab" onClick={() => setTab('queue')}>{t('home.tab.queue')}</button>
        {!paused && <button type="button" role="tab" aria-selected={tab === 'add'} className="hp-tab" onClick={() => setTab('add')}>{t('home.tab.add')}</button>}
        {isUser && <button type="button" role="tab" aria-selected={tab === 'rooms'} className="hp-tab" onClick={() => setTab('rooms')}>{t('home.tab.rooms')}</button>}
      </div>
      <section className="hp-section">
        {tab === 'queue' && <Queue party={party} />}
        {tab === 'add' && !paused && <AddSongs party={party} />}
        {tab === 'rooms' && isUser && <Rooms party={party} parties={data.parties} />}
      </section>

      {isUser && (
        <section className="hp-section hp-actions">
          <ShowOnTv party={party} />
          <button type="button" className="hp-btn hp-btn--wide" disabled={!speakers.length}
                  onClick={() => { setPresetName(party.name || ''); setAsk('preset') }}>{t('home.party.saveAsPreset')}</button>
          <button type="button" className="hp-btn hp-btn--danger hp-btn--wide" onClick={() => setAsk('end')}>{t('home.party.end')}</button>
        </section>
      )}

      {ask === 'end' && (
        <Sheet
          title={t('home.party.endTitle', { name: party.name })}
          onClose={() => setAsk(null)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setAsk(null)}>{t('common.cancel')}</button>
              <button type="button" className="hp-btn hp-btn--danger" disabled={busy} onClick={endParty}>{t('home.party.end')}</button>
            </>
          )}
        >
          <p className="hp-text">{t('home.party.endHelp')}</p>
        </Sheet>
      )}
      {ask === 'preset' && (
        <Sheet
          title={t('home.party.saveAsPreset')}
          onClose={() => setAsk(null)}
          actions={(
            <>
              <button type="button" className="hp-btn" onClick={() => setAsk(null)}>{t('common.cancel')}</button>
              <button type="button" className="hp-btn hp-btn--primary" disabled={busy || !presetName.trim()} onClick={savePreset}>{t('common.save')}</button>
            </>
          )}
        >
          <label className="hp-label" htmlFor="hp-save-preset">{t('home.preset.name')}</label>
          <input id="hp-save-preset" className="hp-input" value={presetName} maxLength={60} autoFocus
                 onChange={(e) => setPresetName(e.target.value)}
                 onKeyDown={(e) => { if (e.key === 'Enter') savePreset() }} />
        </Sheet>
      )}
    </>
  )
}
