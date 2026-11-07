// The video of the YouTube item the speakers are playing, muted, kept in
// step with them. Used in a party's page and (fullscreen) on the TV page.
//
// The page's policy is script-src 'self', so the YouTube iframe API script
// cannot load. The embed is driven the way that script drives it underneath:
// JSON commands sent with postMessage ({event:'command', func, args}) and its
// infoDelivery replies (current time, player state) after a 'listening'
// message. The decisions live in lib/videoSync.js.
//
// Props: videoId; positionSeconds + positionAt (the speaker's position and the
// performance.now() reading it was taken at, in ms); playing; fullscreen.
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import {
  OFFSET_KEY, OFFSET_MAX, OFFSET_MIN, OFFSET_STEP, clampOffset, playerCommand, positionAtTime,
  readPlayerMessage, shouldSeek,
} from '../lib/videoSync.js'
import './home.css'

const ORIGIN = 'https://www.youtube-nocookie.com'
const TICK_MS = 500
const LISTEN_MS = 1000
const PLAYING = 1

function readOffset() {
  try { return clampOffset(window.localStorage.getItem(OFFSET_KEY) ?? 0) } catch { return 0 }
}

function saveOffset(value) {
  try { window.localStorage.setItem(OFFSET_KEY, String(value)) } catch { /* private window */ }
}

function embedUrl(videoId) {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  return `${ORIGIN}/embed/${encodeURIComponent(videoId)}?enablejsapi=1&mute=1&controls=0&playsinline=1`
    + `&rel=0&origin=${encodeURIComponent(origin)}`
}

function Player({ videoId, positionSeconds, positionAt, playing, offset, title }) {
  const frame = useRef(null)
  const speaker = useRef({ positionSeconds, positionAt, playing })
  speaker.current = { positionSeconds, positionAt, playing }
  const shift = useRef(offset)
  shift.current = offset
  // What we know of the embed: loaded, ready, the last time it reported (or
  // the time we last sent it to), the last seek, the play state last asked.
  const player = useRef({ loaded: false, ready: false, video: null, lastSeekAt: -Infinity, asked: null })

  const send = useCallback((func, args) => {
    frame.current?.contentWindow?.postMessage(playerCommand(func, args), ORIGIN)
  }, [])

  const tick = useCallback(() => {
    const p = player.current
    if (!p.loaded) return
    const now = performance.now()
    const want = speaker.current.playing
    const target = Math.max(0, positionAtTime(speaker.current, now) + shift.current)
    const v = p.video
    const videoTime = v ? v.time + (v.playing ? (now - v.at) / 1000 : 0) : undefined
    if (shouldSeek({ target, videoTime, lastSeekAt: p.lastSeekAt, now })) {
      send('seekTo', [target, true])
      p.lastSeekAt = now
      p.video = { time: target, at: now, playing: want }
    }
    if (p.asked !== want) {
      send('mute')
      send(want ? 'playVideo' : 'pauseVideo')
      p.asked = want
    }
  }, [send])

  // Replies from this embed only.
  useEffect(() => {
    const onMessage = (event) => {
      if (event.origin !== ORIGIN || event.source !== frame.current?.contentWindow) return
      const msg = readPlayerMessage(event.data)
      if (!msg) return
      const p = player.current
      if (msg.ready && !p.ready) {
        // Commands sent before the player was ready were dropped: start over.
        p.ready = true
        p.lastSeekAt = -Infinity
        p.asked = null
        p.video = null
        tick()
        return
      }
      if (Number.isFinite(msg.time)) {
        const state = Number.isFinite(msg.state) ? msg.state : null
        p.video = { time: msg.time, at: performance.now(), playing: state == null ? !!p.video?.playing : state === PLAYING }
      } else if (Number.isFinite(msg.state) && p.video) {
        p.video = { ...p.video, playing: msg.state === PLAYING }
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [tick])

  // Ask the embed to report until it says it is ready, as the API script does.
  useEffect(() => {
    const timer = setInterval(() => {
      const p = player.current
      if (!p.loaded || p.ready) return
      frame.current?.contentWindow?.postMessage(JSON.stringify({ event: 'listening', id: 1, channel: 'widget' }), ORIGIN)
    }, LISTEN_MS)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    const timer = setInterval(tick, TICK_MS)
    return () => clearInterval(timer)
  }, [tick])
  // A play/pause or a new speaker reading is acted on at once (still bound by
  // the seek gap); a new offset may seek straight away.
  useEffect(() => { tick() }, [playing, positionSeconds, positionAt, tick])
  useEffect(() => { player.current.lastSeekAt = -Infinity; tick() }, [offset, tick])

  const onLoad = () => {
    const p = player.current
    p.loaded = true
    frame.current?.contentWindow?.postMessage(JSON.stringify({ event: 'listening', id: 1, channel: 'widget' }), ORIGIN)
    tick()
  }

  return (
    <iframe ref={frame} className="hp-video__frame" title={title} src={embedUrl(videoId)} onLoad={onLoad}
            allow="autoplay; encrypted-media; picture-in-picture" referrerPolicy="strict-origin-when-cross-origin" />
  )
}

export default function VideoWindow({ videoId, positionSeconds, positionAt, playing, fullscreen = false }) {
  const { t } = useI18n()
  const [offset, setOffset] = useState(readOffset)

  // The TV page has no slider: keep up with one moved on this device.
  useEffect(() => {
    const onStorage = (event) => { if (event.key === OFFSET_KEY) setOffset(readOffset()) }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const change = (value) => {
    const next = clampOffset(value)
    setOffset(next)
    saveOffset(next)
  }

  if (!videoId) return null
  return (
    <div className={fullscreen ? 'hp-video hp-video--full' : 'hp-video'}>
      <div className="hp-video__box">
        <Player key={videoId} videoId={videoId} positionSeconds={positionSeconds} positionAt={positionAt}
                playing={!!playing} offset={offset} title={t('home.video.title')} />
      </div>
      {!fullscreen && (
        <div className="hp-vol hp-vol--video">
          <label className="hp-vol__name" htmlFor="hp-video-offset">{t('home.video.offset')}</label>
          <input id="hp-video-offset" className="hp-range" type="range" min={OFFSET_MIN} max={OFFSET_MAX}
                 step={OFFSET_STEP} value={offset} onChange={(e) => change(e.target.value)} />
          <span className="hp-vol__level">{t('home.video.seconds', { seconds: offset > 0 ? `+${offset}` : String(offset) })}</span>
        </div>
      )}
    </div>
  )
}
