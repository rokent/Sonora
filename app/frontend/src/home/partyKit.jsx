// Small pieces the party screens share: where on the party page we are, the
// sheet a question sits in, the "move this room?" question itself, a volume
// slider that does not flood the speakers, and the rooms of each system.
import React, { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { BASE, withBase } from '../lib/base.js'
import { orderedHouseholds, twoLineMetadata } from '../lib/format.js'
import { nowPlayingArt } from '../components/Art.jsx'
import { useSystem } from '../lib/store.jsx'
import { mediaKeyOf } from '../lib/videoSync.js'
import { homeApi } from './api.js'

// -- where we are -------------------------------------------------------------
//
// The party screens live in the hash (/party#/p/<id>), not in the path: the
// page's scripts are named relative to it (./assets/...), so a deeper path
// such as /party/p/<id> would ask for /party/p/assets/... and get the page
// shell back instead of a script on a reload. The hash never leaves the
// browser, so every screen reloads from /party as it is.

const listeners = new Set()
const subscribe = (fn) => {
  listeners.add(fn)
  window.addEventListener('popstate', fn)
  window.addEventListener('hashchange', fn)
  return () => {
    listeners.delete(fn)
    window.removeEventListener('popstate', fn)
    window.removeEventListener('hashchange', fn)
  }
}
const localPath = () => {
  const path = window.location.pathname.replace(/\/+$/, '')
  const local = BASE && path.startsWith(BASE) ? path.slice(BASE.length) : path
  const hash = window.location.hash || ''
  if (local === '/party' && hash.startsWith('#/')) return `/party${hash.slice(1).replace(/\/+$/, '')}`
  return local
}

/** The party screen being shown, as a path: "/party", "/party/p/abc". */
export function usePartyPath() {
  return useSyncExternalStore(subscribe, localPath)
}

/** The address of a party screen: "/party/p/abc" -> "/party#/p/abc". */
export function partyHref(path) {
  const sub = path.startsWith('/party') ? path.slice('/party'.length) : path
  return withBase('/party') + (sub && sub !== '/' ? `#${sub}` : '')
}

/** Open another party screen without reloading the page. */
export function go(path, { replace = false } = {}) {
  const url = partyHref(path)
  if (replace) window.history.replaceState(null, '', url)
  else window.history.pushState(null, '', url)
  window.scrollTo?.(0, 0)
  for (const fn of listeners) fn()
}

/** A link to a party screen that behaves like one but stays on the page. */
export function PartyLink({ to, className, children, ...rest }) {
  return (
    <a
      href={partyHref(to)}
      className={className}
      onClick={(event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return
        event.preventDefault()
        go(to)
      }}
      {...rest}
    >{children}</a>
  )
}

// -- errors -------------------------------------------------------------------

const ERROR_KEYS = {
  mixed_household: 'home.error.mixedHousehold',
  no_speakers: 'home.error.noSpeakers',
  not_yours: 'home.error.notYours',
  parties_unavailable: 'home.error.unavailable',
  not_ready: 'home.error.unavailable',
  forbidden: 'home.error.forbidden',
  not_found: 'home.error.notFound',
  speaker_taken: 'home.error.taken',
  tv_unreachable: 'home.tv.unreachable',
  tv_needs_pairing: 'home.tv.needsPairing',
  tv_failed: 'home.tv.failed',
  bad_link: 'home.youtube.error.badLink',
  no_audio: 'home.youtube.error.noAudio',
  youtube_failed: 'home.youtube.error.failed',
  bad_query: 'home.youtube.error.badQuery',
  slow_down: 'home.youtube.error.slowDown',
  unsupported_type: 'home.files.error.type',
  too_large: 'home.files.error.tooLarge',
  party_full: 'home.files.error.partyFull',
  disk_full: 'home.files.error.diskFull',
  empty: 'home.files.error.empty',
  network: 'home.files.error.network',
}

/** Words for a failed party request. */
export function errorText(t, error) {
  const key = ERROR_KEYS[error?.kind] || ERROR_KEYS[error?.body?.error]
  if (key) return t(key)
  if (error?.kind === 'invalid' && error?.body?.message) return error.body.message
  return t('home.error.generic')
}

// -- the sheet ----------------------------------------------------------------

/** A panel over the page for one question. Escape or the backdrop closes it. */
// Open sheets, oldest first: Escape closes only the topmost one, so a move
// question over a preset sheet closes before the preset sheet does.
const openSheets = []

export function Sheet({ title, onClose, children, actions }) {
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const me = {}
    openSheets.push(me)
    const key = (event) => {
      if (event.key !== 'Escape' || openSheets[openSheets.length - 1] !== me) return
      event.stopImmediatePropagation()
      close.current?.()
    }
    window.addEventListener('keydown', key)
    return () => {
      window.removeEventListener('keydown', key)
      const at = openSheets.indexOf(me)
      if (at >= 0) openSheets.splice(at, 1)
    }
  }, [])
  return (
    <div className="hp-sheet" role="presentation" onClick={(event) => { if (event.target === event.currentTarget) onClose?.() }}>
      <div className="hp-sheet__panel" role="dialog" aria-modal="true" aria-label={title}>
        {title && <h2 className="hp-sheet__title">{title}</h2>}
        <div className="hp-sheet__body">{children}</div>
        {actions && <div className="hp-sheet__actions">{actions}</div>}
      </div>
    </div>
  )
}

// -- "move this room to your party?" ------------------------------------------

/**
 * Run a party request that may find a room already in another party.
 *
 * `run(call)` calls `call({})`; on 409 speaker_taken it asks "Move {room}
 * from {party} to yours?" and, on yes, calls `call({take: true})`. It
 * resolves with the answer, or null when the person said no. Other errors
 * reject as they came.
 */
export function useMoveAsk() {
  const { t } = useI18n()
  const [ask, setAsk] = useState(null)
  const run = useCallback(async (call) => {
    try {
      return await call({})
    } catch (error) {
      if (error?.status !== 409 || error?.kind !== 'speaker_taken') throw error
      const yes = await new Promise((resolve) => setAsk({ body: error.body || {}, resolve }))
      if (!yes) return null
      return call({ take: true })
    }
  }, [])
  const close = (answer) => { ask?.resolve(answer); setAsk(null) }
  const dialog = ask ? (
    <Sheet
      title={t('home.move.title')}
      onClose={() => close(false)}
      actions={(
        <>
          <button type="button" className="hp-btn" onClick={() => close(false)}>{t('common.cancel')}</button>
          <button type="button" className="hp-btn hp-btn--primary" onClick={() => close(true)}>{t('home.move.confirm')}</button>
        </>
      )}
    >
      <p className="hp-text">{t('home.move.question', { room: ask.body.room || '', party: ask.body.party_name || '' })}</p>
    </Sheet>
  ) : null
  return [dialog, run]
}

// -- volume -------------------------------------------------------------------

/**
 * A 0..100 slider. It shows the dragged level at once and sends at most one
 * change per `every` ms, with the last one always sent; between drags it
 * follows `value` from the speakers.
 */
export function VolumeSlider({ value, onSend, label, disabled = false, every = 150 }) {
  const [held, setHeld] = useState(null)
  const last = useRef(0)
  const timer = useRef(null)
  const pending = useRef(null)
  const release = useRef(null)
  useEffect(() => () => { clearTimeout(timer.current); clearTimeout(release.current) }, [])
  const flush = () => {
    timer.current = null
    if (pending.current == null) return
    last.current = Date.now()
    const level = pending.current
    pending.current = null
    Promise.resolve(onSend(level)).catch(() => {})
  }
  const change = (level) => {
    setHeld(level)
    pending.current = level
    clearTimeout(release.current)
    // The speakers' own report takes over a moment after the last change.
    release.current = setTimeout(() => setHeld(null), 2500)
    if (timer.current) return
    const wait = Math.max(0, every - (Date.now() - last.current))
    timer.current = setTimeout(flush, wait)
  }
  const shown = held ?? (Number.isFinite(value) ? value : 0)
  return (
    <input
      type="range"
      className="hp-range"
      min={0}
      max={100}
      step={1}
      value={shown}
      disabled={disabled}
      aria-label={label}
      onChange={(event) => change(Number(event.target.value))}
    />
  )
}

// -- rooms by system ----------------------------------------------------------

/**
 * Each system's rooms, labelled by generation ("S1", "S2"; numbered when a
 * house has two of the same): [{id, label, rooms: [zone]}]. The browser room
 * belongs to no system and is never offered.
 */
export function roomsBySystem(households, zones) {
  const ordered = orderedHouseholds(households || [])
  const counts = {}
  for (const h of ordered) counts[h.generation || ''] = (counts[h.generation || ''] || 0) + 1
  const seen = {}
  return ordered.map((h) => {
    const gen = h.generation || ''
    seen[gen] = (seen[gen] || 0) + 1
    const label = (gen || '?') + (counts[gen] > 1 ? ` (${seen[gen]})` : '')
    const rooms = (h.zone_uuids || []).map((uuid) => zones?.[uuid]).filter((z) => z && !z.local)
      .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
    return { id: h.id, label, rooms }
  })
}

/** The id of the system holding a room, or null. */
export function systemOf(households, uuid) {
  return (households || []).find((h) => (h.zone_uuids || []).includes(uuid))?.id ?? null
}

/** Which party holds each room: {uuid: party}. */
export function holders(parties) {
  const out = {}
  for (const p of parties || []) for (const uuid of p.speakers || []) out[uuid] = p
  return out
}

/** A room picker grouped by system; rooms of another system than the picked
 * ones are disabled. `picked` is an array of uuids. */
export function RoomChecklist({ systems, picked, onToggle, held = {}, ownPartyId = null, busy = false }) {
  const { t } = useI18n()
  const pickedSystem = picked.length
    ? systems.find((s) => s.rooms.some((z) => picked.includes(z.uuid)))?.id ?? null
    : null
  return (
    <div className="hp-systems">
      {systems.filter((s) => s.rooms.length).map((system) => {
        const other = pickedSystem && pickedSystem !== system.id
        return (
          <fieldset key={system.id} className="hp-system" disabled={busy}>
            <legend className="hp-system__label">
              {t('home.rooms.system', { system: system.label })}
              {other && <span className="hp-muted"> · {t('home.rooms.otherSystem')}</span>}
            </legend>
            {system.rooms.map((zone) => {
              const on = picked.includes(zone.uuid)
              const holder = held[zone.uuid]
              const elsewhere = holder && holder.id !== ownPartyId
              return (
                <label key={zone.uuid} className={`hp-check${other ? ' hp-check--off' : ''}`}>
                  <input type="checkbox" checked={on} disabled={!!other}
                         onChange={() => onToggle(zone.uuid, !on)} />
                  <span className="hp-check__name">{zone.name}</span>
                  {elsewhere && <span className="hp-muted hp-check__hint">{t('home.rooms.inParty', { party: holder.name })}</span>}
                </label>
              )
            })}
          </fieldset>
        )
      })}
    </div>
  )
}

// -- what a party is playing --------------------------------------------------

/** The party's lead room and what it is playing: {lead, line1, line2, art, playing}. */
export function partyNowPlaying(party, zones, t) {
  const lead = zones?.[party?.speakers?.[0]] || null
  const transport = lead?.transport || null
  const { line1, line2 } = twoLineMetadata(transport, t)
  return {
    lead,
    line1,
    line2,
    art: lead ? nowPlayingArt(lead.host, transport, { zone: lead.uuid }) : '',
    playing: transport?.state === 'PLAYING' || transport?.state === 'TRANSITIONING',
  }
}

/** The party's room names, lead first. */
export function partyRooms(party, zones) {
  return (party?.speakers || []).map((uuid) => zones?.[uuid]?.name).filter(Boolean)
}

// -- the queue and its video (the party page and the TV page) -----------------

/** The party's queue, fetched again whenever the speakers say it changed. */
export function usePartyQueue(party) {
  const { t } = useI18n()
  const { queueEpochs, partiesEpoch } = useSystem()
  const lead = party?.speakers?.[0] || ''
  const partyId = party?.id || ''
  const [queue, setQueue] = useState(null)
  const [error, setError] = useState('')
  const [nudge, setNudge] = useState(0)
  const epoch = queueEpochs[lead] || 0

  useEffect(() => {
    if (!partyId) return undefined
    if (!lead) { setQueue({ items: [], start: 0 }); return undefined }
    let live = true
    homeApi.partyQueue(partyId)
      .then((r) => { if (live) { setQueue(r); setError('') } })
      .catch((exc) => { if (live) setError(errorText(t, exc)) })
    return () => { live = false }
  }, [partyId, lead, epoch, partiesEpoch, nudge, t])

  return { queue, error, setError, reload: () => setNudge((n) => n + 1) }
}

/** The YouTube video the speakers are playing, if they are playing one of ours. */
export function currentVideo(transport, queue) {
  const uri = transport?.track_uri || ''
  if (!mediaKeyOf(uri)) return null
  const items = queue?.items || []
  const byUri = items.find((item) => item.uri === uri && item.video_id)
  if (byUri) return byUri
  const at = (transport?.track_number || 0) - 1 - (queue?.start || 0)
  const byNumber = items[at]
  return byNumber?.video_id ? byNumber : null
}
