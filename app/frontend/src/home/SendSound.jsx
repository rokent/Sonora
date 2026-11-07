// Send a browser tab's sound to the party. The browser asks which tab (or
// screen) to share; only its audio is used: Chrome insists on video for a
// tab share, so the video track is stopped at once. The audio is recorded as
// Opus in WebM, a chunk every 250 ms, and sent over /ws/tabsound, where the
// server decodes it and plays it on the party's speakers a few seconds late.
//
// Sharing needs a secure page (https): on the plain-HTTP address the button
// is replaced by a hint. Browsers without tab-audio sharing (phones, Safari)
// get a hint too.
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { socketUrl } from '../lib/base.js'

const MIME = 'audio/webm;codecs=opus'
const BITRATE = 160000
const SLICE_MS = 250

/** Words for how the server closed the socket (tabsound.py's codes). */
const CLOSE_KEYS = {
  1008: 'home.send.error.forbidden',
  4400: 'home.send.error.target',
  4403: 'home.send.error.forbidden',
  4404: 'home.send.error.target',
  4409: 'home.send.error.replaced',
  4410: 'home.send.error.partyEnded',
  4413: 'home.send.error.stopped',
  4429: 'home.send.error.tooMany',
  4500: 'home.send.error.stopped',
  4503: 'home.send.error.unavailable',
}

function canShare() {
  return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getDisplayMedia
    && typeof window.MediaRecorder !== 'undefined'
    && (!window.MediaRecorder.isTypeSupported || window.MediaRecorder.isTypeSupported(MIME))
}

/** `target` is "party:<id>" or "zone:<uuid>". */
export default function SendSound({ target }) {
  const { t } = useI18n()
  // idle | asking | connecting | waiting | live
  const [phase, setPhase] = useState('idle')
  const [error, setError] = useState('')
  const run = useRef(null)

  const stop = useCallback((message = '') => {
    const current = run.current
    run.current = null
    if (current) {
      current.stopped = true
      try { if (current.recorder && current.recorder.state !== 'inactive') current.recorder.stop() } catch { /* already stopped */ }
      for (const track of current.stream?.getTracks() || []) {
        try { track.stop() } catch { /* already stopped */ }
      }
      try { current.socket?.close(1000) } catch { /* already closed */ }
    }
    setPhase('idle')
    setError(message)
  }, [])

  useEffect(() => () => { stop() }, [stop])
  // A new target (another party) ends what was sent to the old one.
  useEffect(() => { stop() }, [target, stop])

  async function start() {
    if (run.current) return
    setError('')
    setPhase('asking')
    const current = { stopped: false }
    run.current = current
    let stream
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({ audio: true, video: true })
    } catch (exc) {
      if (run.current !== current) return
      // Cancelling the picker is not an error worth words.
      stop(exc?.name === 'NotAllowedError' || exc?.name === 'AbortError' ? '' : t('home.send.error.unavailable'))
      return
    }
    if (run.current !== current) {
      for (const track of stream.getTracks()) track.stop()
      return
    }
    current.stream = stream
    for (const track of stream.getVideoTracks()) track.stop()
    const audio = stream.getAudioTracks()
    if (!audio.length) {
      stop(t('home.send.error.noAudio'))
      return
    }
    // The browser's own "Stop sharing" bar ends the track.
    for (const track of audio) track.addEventListener('ended', () => { if (run.current === current) stop() })

    let recorder
    try {
      recorder = new MediaRecorder(new MediaStream(audio), { mimeType: MIME, audioBitsPerSecond: BITRATE })
    } catch {
      stop(t('home.send.unsupported'))
      return
    }
    current.recorder = recorder
    setPhase('connecting')
    const socket = new WebSocket(socketUrl(`/ws/tabsound?target=${encodeURIComponent(target)}`))
    socket.binaryType = 'arraybuffer'
    current.socket = socket
    recorder.ondataavailable = (event) => {
      if (event.data?.size && socket.readyState === WebSocket.OPEN) socket.send(event.data)
    }
    socket.onopen = () => {
      if (run.current !== current) return
      try {
        recorder.start(SLICE_MS)
      } catch {
        stop(t('home.send.unsupported'))
      }
    }
    socket.onmessage = (event) => {
      if (run.current !== current || typeof event.data !== 'string') return
      try {
        const state = JSON.parse(event.data)?.state
        if (state === 'live' || state === 'waiting') setPhase(state)
      } catch { /* not ours */ }
    }
    socket.onclose = (event) => {
      if (run.current !== current) return
      stop(t(CLOSE_KEYS[event.code] || 'home.send.error.lost'))
    }
  }

  if (typeof window !== 'undefined' && !window.isSecureContext) {
    return <p className="hp-muted hp-small">{t('home.send.needsHttps')}</p>
  }
  if (!canShare()) {
    return <p className="hp-muted hp-small">{t('home.send.unsupported')}</p>
  }

  const active = phase !== 'idle'
  return (
    <div className={`hp-send${active ? ' hp-card' : ''}`}>
      {active ? (
        <div className="hp-row">
          <span className="hp-grow" role="status">
            <span className="hp-block">{t('home.send.sending')}</span>
            <span className="hp-muted hp-small hp-block">
              {phase === 'live' ? t('home.send.live') : phase === 'waiting' ? t('home.send.waiting') : t('home.send.connecting')}
            </span>
          </span>
          <button type="button" className="hp-btn hp-btn--danger" onClick={() => stop()}>{t('home.send.stop')}</button>
        </div>
      ) : (
        <>
          <button type="button" className="hp-btn hp-btn--wide" onClick={start}>{t('home.send.start')}</button>
          <p className="hp-muted hp-small">{t('home.send.help')}</p>
        </>
      )}
      {error && <p className="hp-bad" role="alert">{error}</p>}
    </div>
  )
}
