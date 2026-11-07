// TV mode: one party, full screen, on a TV browser, a Home Assistant card or
// any device. Theme-independent like the party page. It follows a party
// (tvParty.js pickParty) and shows its video, a MilkDrop visualizer, or the
// cover with the room names and, when allowed, synced lyrics.
//
// Who decides what is shown:
// - a display (a TV signed in with a display link) gets its settings from
//   GET /api/displays/me: the party (null = any), the mode and lyrics;
// - a signed-in person may open /tv?party=<id>&mode=<video|visualizer|cover>
//   (and /tv?display=<id> to see what a link shows); ?mode= wins, and a small
//   overlay lets them turn lyrics on.
// A deleted display link answers 401 display_removed (or the sign-in route
// sends /tv?error=removed): the page says so and stops asking.
//
// When the party ends the page says "No party playing" and keeps listening:
// the store's partiesEpoch (pushed on /ws) refetches the list, and a slow
// poll covers a missed push. A screen for any party then follows the next
// one that starts.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useSystem } from '../lib/store.jsx'
import { withBase } from '../lib/base.js'
import { OFFSET_KEY, clampOffset, mediaKeyOf, transportAnchor } from '../lib/videoSync.js'
import { useFollowLine, useLyrics, useLyricsLine } from '../lib/lyrics.js'
import Art from '../components/Art.jsx'
import { homeApi } from './api.js'
import { useMe } from './useMe.js'
import { currentVideo, partyNowPlaying, partyRooms, usePartyQueue } from './partyKit.jsx'
import { pickMode, pickParty } from './tvParty.js'
import { isLiveUri } from './visualFrames.js'
import VideoWindow from './VideoWindow.jsx'
import Visualizer from './Visualizer.jsx'
import './tv.css'

const IDLE_MS = 3000
// How long the last party stays on screen after it leaves the list, so a
// refetch racing a restart (or a party ended and started again at once)
// does not flash "No party playing".
const GONE_GRACE_MS = 5000
const PARTIES_POLL_MS = 30000
const DISPLAY_POLL_MS = 60000

const removedError = (error) => error?.status === 401 && error?.kind === 'display_removed'

function readQuery() {
  const q = new URLSearchParams(window.location.search)
  return {
    display: q.get('display') || '',
    party: q.get('party') || '',
    mode: q.get('mode') || '',
    removed: q.get('error') === 'removed',
  }
}

/** The display link's settings, re-read now and then so a deleted link is noticed. */
function useDisplay({ isDisplay, id, enabled, onRemoved }) {
  // `done` once the first answer (or failure) is in, so the page does not
  // show the wrong party or mode for a moment before the link's arrive.
  const [state, setState] = useState({ done: false, link: null })
  const removed = useRef(onRemoved)
  removed.current = onRemoved
  useEffect(() => {
    if (!enabled) return undefined
    let live = true
    const read = () => {
      homeApi.displayMe(isDisplay ? '' : id)
        .then((link) => { if (live) setState({ done: true, link }) })
        .catch((error) => {
          if (!live) return
          if (removedError(error) || (!isDisplay && error?.status === 404)) removed.current()
          // Anything else (a restart, a dropped connection): keep what we had.
          setState((prev) => ({ ...prev, done: true }))
        })
    }
    read()
    const timer = setInterval(read, DISPLAY_POLL_MS)
    return () => { live = false; clearInterval(timer) }
  }, [enabled, isDisplay, id])
  return { link: enabled ? state.link : null, loading: enabled && !state.done }
}

/** Running parties, refetched on every push and on a slow timer. */
function useTvParties({ enabled, onRemoved }) {
  const { partiesEpoch } = useSystem()
  const [state, setState] = useState({ loading: true, parties: [], error: null })
  const [tick, setTick] = useState(0)
  const removed = useRef(onRemoved)
  removed.current = onRemoved
  useEffect(() => {
    if (!enabled) return undefined
    const timer = setInterval(() => setTick((n) => n + 1), PARTIES_POLL_MS)
    return () => clearInterval(timer)
  }, [enabled])
  useEffect(() => {
    if (!enabled) return undefined
    let live = true
    homeApi.parties()
      .then((r) => { if (live) setState({ loading: false, parties: r?.parties || [], error: null }) })
      .catch((error) => {
        if (!live) return
        if (removedError(error)) removed.current()
        setState((prev) => ({ ...prev, loading: false, error }))
      })
    return () => { live = false }
  }, [enabled, partiesEpoch, tick])
  return state
}

/** True for three seconds after the last pointer, touch or key. */
function useActive() {
  const [active, setActive] = useState(true)
  useEffect(() => {
    let timer = setTimeout(() => setActive(false), IDLE_MS)
    const wake = () => {
      setActive(true)
      clearTimeout(timer)
      timer = setTimeout(() => setActive(false), IDLE_MS)
    }
    const events = ['pointermove', 'pointerdown', 'keydown', 'touchstart', 'wheel']
    for (const name of events) window.addEventListener(name, wake, { passive: true })
    return () => {
      clearTimeout(timer)
      for (const name of events) window.removeEventListener(name, wake)
    }
  }, [])
  return active
}

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null
}

function FullscreenButton() {
  const { t } = useI18n()
  const [on, setOn] = useState(() => !!fullscreenElement())
  useEffect(() => {
    const change = () => setOn(!!fullscreenElement())
    document.addEventListener('fullscreenchange', change)
    document.addEventListener('webkitfullscreenchange', change)
    return () => {
      document.removeEventListener('fullscreenchange', change)
      document.removeEventListener('webkitfullscreenchange', change)
    }
  }, [])
  const root = document.documentElement
  const enter = root.requestFullscreen || root.webkitRequestFullscreen
  if (!enter) return null
  const toggle = () => {
    try {
      if (fullscreenElement()) (document.exitFullscreen || document.webkitExitFullscreen)?.call(document)
      else enter.call(root)?.catch?.(() => {})
    } catch { /* refused: nothing to do */ }
  }
  return (
    <button type="button" className="tv-button" onClick={toggle}>
      {on ? t('home.tvPage.exitFullscreen') : t('home.tvPage.fullscreen')}
    </button>
  )
}

/** A message in the middle of the black screen. */
function Message({ title, text, children }) {
  return (
    <div className="tv-message" role="status">
      <p className="tv-message__title">{title}</p>
      {text && <p className="tv-message__text">{text}</p>}
      {children}
    </div>
  )
}

/** Timed lyrics, the line being sung kept in the middle. */
function TimedLyrics({ transport, lines }) {
  const { t } = useI18n()
  const index = useLyricsLine(transport, lines)
  const box = useRef(null)
  useFollowLine(box, index)
  return (
    <div className="tv-lyrics" ref={box} tabIndex={-1}>
      {lines.map(([, words], i) => (
        <p key={i} data-line={i} className={i === index ? 'tv-lyrics__line tv-lyrics__line--now' : 'tv-lyrics__line'}>
          {words || '\u00a0'}
        </p>
      ))}
      <p className="tv-lyrics__credit">{t('lyrics.credit')}</p>
    </div>
  )
}

/** Lyrics from LRCLIB, as the themes' big players look them up (lib/lyrics.js). */
function Lyrics({ transport, agreedByLink }) {
  const { t } = useI18n()
  const lyrics = useLyrics(transport, true)
  const agree = lyrics.state === 'notice' ? lyrics.agree : null
  // The link's maker accepted the LRCLIB notice when ticking lyrics.
  useEffect(() => { if (agree && agreedByLink) agree() }, [agree, agreedByLink])

  if (lyrics.state === 'off') return null
  if (lyrics.state === 'notice') {
    if (agreedByLink) return null
    return (
      <div className="tv-lyrics tv-lyrics--notice">
        <p className="tv-lyrics__status">{t('lyrics.notice')}</p>
        <button type="button" className="tv-button" onClick={lyrics.agree}>{t('lyrics.agree')}</button>
      </div>
    )
  }
  if (lyrics.state === 'timed') return <TimedLyrics transport={transport} lines={lyrics.lines} />
  if (lyrics.state === 'plain') {
    return (
      <div className="tv-lyrics">
        <p className="tv-lyrics__plain">{lyrics.text}</p>
        <p className="tv-lyrics__credit">{t('lyrics.untimed')} {t('lyrics.credit')}</p>
      </div>
    )
  }
  const status = { loading: 'lyrics.loading', none: 'lyrics.none', instrumental: 'lyrics.instrumental', error: 'lyrics.error' }[lyrics.state]
  return status ? <div className="tv-lyrics"><p className="tv-lyrics__status">{t(status)}</p></div> : null
}

function Cover({ party, zones, lyricsOn, agreedByLink }) {
  const { t } = useI18n()
  const now = partyNowPlaying(party, zones, t)
  const rooms = partyRooms(party, zones)
  const transport = now.lead?.transport || null
  return (
    <div className={lyricsOn ? 'tv-cover tv-cover--lyrics' : 'tv-cover'}>
      <div className="tv-cover__main">
        <div className="tv-cover__art"><Art src={now.art} size={640} /></div>
        <div className="tv-cover__text">
          <p className="tv-cover__title">{now.line1 || t('home.tvPage.nothingPlaying')}</p>
          {now.line2 && <p className="tv-cover__artist">{now.line2}</p>}
          {rooms.length > 0 && <p className="tv-cover__rooms">{rooms.join(' · ')}</p>}
        </div>
      </div>
      {lyricsOn && <Lyrics transport={transport} agreedByLink={agreedByLink} />}
    </div>
  )
}

// The canvas and the embed change only with the speaker's anchor; the store
// pushes every room's changes, which must not re-render them.
const TvVideo = React.memo(VideoWindow)
const TvVisualizer = React.memo(Visualizer)

/** The speaker's clock for the video (performance.now) and the visualizer (Date.now). */
function useAnchor(transport) {
  const fallbackAt = useRef(performance.now())
  return useMemo(() => {
    const anchor = transportAnchor(transport, fallbackAt.current)
    // Visualizer.jsx wants a ms epoch: the same instant on Date.now()'s clock.
    const epochAt = Date.now() - (performance.now() - anchor.positionAt)
    return { ...anchor, epochAt }
  }, [transport?.rel_time, transport?.received_at, transport?.state, transport?.position_age]) // eslint-disable-line react-hooks/exhaustive-deps
}

/** The picture offset set with the video's slider on this device (a TV's
 * own picture delay); the visualizer uses it too. */
function useStoredOffset() {
  const read = () => {
    try { return clampOffset(window.localStorage.getItem(OFFSET_KEY) ?? 0) } catch { return 0 }
  }
  const [offset, setOffset] = useState(read)
  useEffect(() => {
    const onStorage = (event) => { if (event.key === OFFSET_KEY) setOffset(read()) }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])
  return offset
}

function Stage({ party, mode, lyricsOn, agreedByLink, onVideo }) {
  const { zones } = useSystem()
  const lead = zones[party.speakers?.[0]] || null
  const transport = lead?.transport || null
  const anchor = useAnchor(transport)
  const queueState = usePartyQueue(mode === 'video' ? party : null)
  const trackUri = transport?.track_uri || ''
  const liveSound = isLiveUri(trackUri)
  const offset = useStoredOffset()

  const video = mode === 'video' ? currentVideo(transport, queueState.queue) : null
  const videoId = video?.video_id || ''
  useEffect(() => { onVideo(!!videoId) }, [videoId, onVideo])
  useEffect(() => () => onVideo(false), [onVideo])

  if (videoId) {
    return <TvVideo videoId={videoId} positionSeconds={anchor.positionSeconds}
                    positionAt={anchor.positionAt} playing={anchor.playing} fullscreen />
  }
  if (mode === 'visualizer') {
    return (
      <div className="tv-visualizer">
        <TvVisualizer partyId={party.id} trackUri={trackUri} positionSeconds={anchor.positionSeconds}
                    positionAt={anchor.epochAt} playing={anchor.playing}
                    isRelayed={liveSound || !!mediaKeyOf(trackUri)} live={liveSound} offsetSeconds={offset} />
      </div>
    )
  }
  return <Cover party={party} zones={zones} lyricsOn={lyricsOn} agreedByLink={agreedByLink} />
}

export default function TvApp() {
  const { t } = useI18n()
  const me = useMe()
  const query = useMemo(readQuery, [])
  const active = useActive()
  const [removed, setRemoved] = useState(query.removed)
  const onRemoved = useCallback(() => setRemoved(true), [])

  const kind = me.principal?.kind || null
  const isDisplay = kind === 'display'
  // With sign-in switched off everyone is the household.
  const isUser = kind === 'user' || (!me.loading && !me.auth && kind !== 'guest' && !isDisplay)
  const canWatch = isDisplay || isUser || kind === 'guest'
  const ready = !me.loading && !removed

  const display = useDisplay({
    isDisplay, id: query.display, enabled: ready && (isDisplay || (isUser && !!query.display)), onRemoved,
  })
  const parties = useTvParties({ enabled: ready && canWatch, onRemoved })

  const link = display.link
  const wanted = isDisplay ? (link?.party || null) : (query.party || link?.party || null)
  const mode = pickMode(isDisplay ? null : query.mode, link?.mode)
  const [lyricsTicked, setLyricsTicked] = useState(false)
  const lyricsOn = !!link?.lyrics || (isUser && lyricsTicked)

  // Stay with the party on screen while it runs (a screen for any party).
  const shownId = useRef(null)
  const picked = pickParty(parties.parties, wanted, shownId.current)
  // The last party shown, held for a moment after it leaves the list.
  const [held, setHeld] = useState(null)
  useEffect(() => { if (picked) setHeld(picked) }, [picked])
  useEffect(() => {
    if (picked || !held) return undefined
    const timer = setTimeout(() => setHeld(null), GONE_GRACE_MS)
    return () => clearTimeout(timer)
  }, [picked, held])
  const party = picked || held
  useEffect(() => { shownId.current = picked?.id || null }, [picked])
  const [showingVideo, setShowingVideo] = useState(false)

  let body
  if (removed) {
    body = <Message title={t('home.tvPage.removedTitle')} text={t('home.tvPage.removedHelp')} />
  } else if (me.loading || (display.loading && !link) || (parties.loading && canWatch)) {
    body = null
  } else if (!canWatch) {
    const next = `/tv${window.location.search}`
    body = (
      <Message title={t('home.tvPage.signInTitle')} text={t('home.tvPage.signInHelp')}>
        <a className="tv-button" href={withBase(`/auth/login?next=${encodeURIComponent(next)}`)}>{t('home.tvPage.signIn')}</a>
      </Message>
    )
  } else if (!party) {
    body = <Message title={t('home.tvPage.noParty')} text={t('home.tvPage.noPartyHelp')} />
  } else {
    body = <Stage key={party.id} party={party} mode={mode} lyricsOn={lyricsOn} agreedByLink={isDisplay && !!link?.lyrics}
                  onVideo={setShowingVideo} />
  }

  // The lyrics switch is for signed-in people on the cover (the video falls
  // back to it when the track has none); a display's link decides for it.
  // A signed-in person on a lyrics link sees the LRCLIB notice like anyone
  // else: only a display takes the link's maker's consent as its own.
  const lyricsSwitch = isUser && !link?.lyrics && party && !removed && mode !== 'visualizer' && !showingVideo

  return (
    <div className={active ? 'tv' : 'tv tv--idle'} aria-label={t('home.tvPage.label')}>
      {body}
      <div className="tv-chrome">
        {lyricsSwitch && (
          <label className="tv-button tv-check">
            <input type="checkbox" checked={lyricsTicked} onChange={(e) => setLyricsTicked(e.target.checked)} />
            <span>{t('lyrics.show')}</span>
          </label>
        )}
        <FullscreenButton />
      </div>
    </div>
  )
}
