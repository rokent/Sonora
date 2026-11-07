// The MilkDrop visualizer itself: Butterchurn on a WebGL canvas. Loaded only
// through Visualizer.jsx's React.lazy, so neither Butterchurn nor its presets
// are in the main bundle.
//
// Its audio is picked each animation frame, best first:
//   1. exact: the party's own audio, analysed on the server (/ws/visual),
//      when the server can (config `visuals`, or the socket not closing
//      4001) and the track passes through it (isRelayed). Frames are lined
//      up with the speaker's clock by visualFrames.js; for live sound
//      (`live`: the party plays Sonora's /api/home/live/ stream) they are
//      drawn a fixed delay after they arrive instead (visualFrames "live"
//      alignment), the offset moving that delay;
//   2. microphone: only on a secure page, after a tap on "Use microphone";
//   3. generic: a slow synthetic waveform pulsing at 120 BPM.
// While the speaker is paused (and no microphone listens) it is fed silence.
//
// A lost WebGL context (a TV browser reclaiming the GPU, a driver reset) is
// kept restorable (preventDefault on webglcontextlost); when the browser
// restores it the canvas is replaced and Butterchurn built again on it. The
// audio context, and with it a microphone already granted, lives across that.
import React, { useEffect, useRef, useState } from 'react'
import butterchurnModule from 'butterchurn'
import buildPresets from 'virtual:butterchurn-presets'
import { useI18n } from '../i18n/index.jsx'
import { request } from '../lib/api.js'
import { socketUrl } from '../lib/base.js'
import {
  LEVELS_SAMPLES, LIVE_QUEUE_MAX, createFrameQueue, decodeFrame, frameTarget, liveStamper, silence,
  syntheticWave, upsample,
} from './visualFrames.js'
import './visualizer.css'

const butterchurn = butterchurnModule?.createVisualizer ? butterchurnModule : butterchurnModule.default

const PRESET_SECONDS = 30
const BLEND_SECONDS = 2.7
/** Drawing size cap: a TV at 4K still renders at most this wide. */
const MAX_WIDTH = 1920
/** /ws/visual close codes (backend home/analysis.py). */
const CLOSE_DISABLED = 4001      // no ffmpeg: never tried again this session
const CLOSE_UNKNOWN_PARTY = 4004 // looked for again slowly
const CLOSE_TOO_SLOW = 4008      // retried with its own, growing backoff
const CLOSE_NOT_ALLOWED = 1008   // not tried again for this party
const BACKOFF_MIN = 2000
const BACKOFF_MAX = 30000
const SLOW_MAX = 60000
const UNKNOWN_PARTY_WAIT = 15000
/** A socket counts as healthy (its backoff starts over) once it has been
 * open this long, or has delivered this many frames (one second's worth). */
const HEALTHY_OPEN = 10000
const HEALTHY_FRAMES = 30

// The server said it has no ffmpeg: no page needs to ask again until reload.
let exactDisabled = false

// The presets, built once Butterchurn has put its helpers on window (it does
// so when its module is evaluated, before any of this runs).
let presetsCache = null
const presetsNow = () => presetsCache || (presetsCache = buildPresets(window))

function shuffled(list) {
  const out = list.slice()
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[out[i], out[j]] = [out[j], out[i]]
  }
  return out
}

function webgl2Supported() {
  try { return !!document.createElement('canvas').getContext('webgl2') } catch { return false }
}

export default function VisualizerCanvas({
  partyId, trackUri, positionSeconds, positionAt, playing, isRelayed, live: liveSound = false,
  offsetSeconds = 0,
}) {
  const { t } = useI18n()
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const vizRef = useRef(null)
  const audioRef = useRef(null)
  const micRef = useRef(null)
  const micPending = useRef(false)
  const mounted = useRef(false)
  const queueRef = useRef(null)
  if (!queueRef.current) queueRef.current = createFrameQueue()
  // Live sound's frames, stamped with their arrival (visualFrames.js).
  const liveQueueRef = useRef(null)
  if (!liveQueueRef.current) liveQueueRef.current = createFrameQueue({ max: LIVE_QUEUE_MAX })
  const stampRef = useRef(null)
  if (!stampRef.current) stampRef.current = liveStamper()
  const clearQueues = () => {
    queueRef.current.clear()
    liveQueueRef.current.clear()
    stampRef.current = liveStamper()
  }
  const nextRef = useRef(() => {})
  const [generation, setGeneration] = useState(0) // a new canvas after a context restore
  const [unsupported, setUnsupported] = useState(false)
  const [exactOff, setExactOff] = useState(exactDisabled)
  const [exactLive, setExactLive] = useState(false)
  const [mic, setMic] = useState('off') // off | on | denied

  // What the animation loop reads, current without restarting it.
  const live = useRef({})
  live.current = { positionSeconds, positionAt, playing, isRelayed, offsetSeconds, exactLive, liveSound }

  // The server says up front when it cannot analyse; an older one that does
  // not say is found out by the socket closing 4001.
  useEffect(() => {
    let alive = true
    request('/api/home/config')
      .then((config) => { if (alive && config?.visuals === 'approximate') setExactOff(true) })
      .catch(() => { /* try the socket anyway */ })
    return () => { alive = false }
  }, [])

  // The server's frames, for a relayed track.
  useEffect(() => {
    if (!partyId || !isRelayed || exactOff) return undefined
    let socket = null
    let timer = null
    let stopped = false
    // backoff: for drops in general, back to the minimum only once the
    // socket proved itself (30 frames arrived, or it stayed open 10 s), so
    // one that sends a frame and drops does not retry every 2 s. slow:
    // for 4008, which a healthy-looking socket can hit again and again, so
    // nothing resets it short of the effect starting over.
    let backoff = BACKOFF_MIN
    let slow = BACKOFF_MIN
    const connect = () => {
      let openedAt = 0
      let frames = 0
      socket = new WebSocket(socketUrl(`/ws/visual?party=${encodeURIComponent(partyId)}`))
      socket.onopen = () => { openedAt = Date.now(); setExactLive(true) }
      socket.onmessage = (event) => {
        const frame = decodeFrame(event.data)
        if (!frame) return
        if (++frames === HEALTHY_FRAMES) backoff = BACKOFF_MIN
        if (live.current.liveSound) liveQueueRef.current.push(stampRef.current(frame, Date.now() / 1000))
        else queueRef.current.push(frame)
      }
      socket.onclose = (event) => {
        setExactLive(false)
        clearQueues()
        if (stopped) return
        if (event.code === CLOSE_DISABLED) { exactDisabled = true; setExactOff(true); return }
        if (event.code === CLOSE_NOT_ALLOWED) return
        let wait
        if (event.code === CLOSE_TOO_SLOW) {
          wait = slow
          slow = Math.min(SLOW_MAX, slow * 2)
        } else if (event.code === CLOSE_UNKNOWN_PARTY) {
          // A party about to start: look again slowly.
          wait = UNKNOWN_PARTY_WAIT
        } else {
          if (openedAt && Date.now() - openedAt > HEALTHY_OPEN) backoff = BACKOFF_MIN
          wait = backoff
          backoff = Math.min(BACKOFF_MAX, backoff * 2)
        }
        timer = setTimeout(connect, wait)
      }
    }
    connect()
    return () => {
      stopped = true
      clearTimeout(timer)
      if (socket) { socket.onclose = null; socket.close() }
      setExactLive(false)
      clearQueues()
    }
  }, [partyId, isRelayed, exactOff]) // eslint-disable-line react-hooks/exhaustive-deps

  // A new track: whatever was queued belongs to the old one.
  useEffect(() => { clearQueues() }, [trackUri, liveSound]) // eslint-disable-line react-hooks/exhaustive-deps

  // The audio context, for the life of the component: Butterchurn needs one,
  // and a microphone hangs off it.
  useEffect(() => {
    mounted.current = true
    const Context = window.AudioContext || window.webkitAudioContext
    const audio = Context ? new Context() : null
    audioRef.current = audio
    return () => {
      mounted.current = false
      const mine = micRef.current
      micRef.current = null
      if (mine) mine.stream.getTracks().forEach((track) => { track.onended = null; track.stop() })
      audioRef.current = null
      if (audio) audio.close().catch(() => {})
    }
  }, [])

  // Butterchurn on the current canvas, its presets, and the animation loop.
  useEffect(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    const audio = audioRef.current
    if (!canvas || !wrap) return undefined
    if (!webgl2Supported() || !audio || !butterchurn) { setUnsupported(true); return undefined }

    let viz
    const size = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      let width = Math.max(1, Math.round(wrap.clientWidth * ratio))
      let height = Math.max(1, Math.round(wrap.clientHeight * ratio))
      if (width > MAX_WIDTH) { height = Math.round(height * MAX_WIDTH / width); width = MAX_WIDTH }
      return { width, height }
    }
    let presets
    try {
      presets = presetsNow()
      const { width, height } = size()
      canvas.width = width
      canvas.height = height
      viz = butterchurn.createVisualizer(audio, canvas, { width, height, pixelRatio: 1 })
    } catch {
      setUnsupported(true)
      return undefined
    }
    vizRef.current = viz
    // A microphone granted before a context restore carries on.
    if (micRef.current) { try { viz.connectAudio(micRef.current.source) } catch { /* generic */ } }

    const names = shuffled(Object.keys(presets))
    let index = 0
    const load = (blend) => {
      try { viz.loadPreset(presets[names[index % names.length]], blend) } catch { /* the next one */ }
    }
    load(0)
    let cycle = null
    const restartCycle = () => {
      clearInterval(cycle)
      cycle = setInterval(() => { index += 1; load(BLEND_SECONDS) }, PRESET_SECONDS * 1000)
    }
    restartCycle()
    nextRef.current = () => { index += 1; load(BLEND_SECONDS / 2); restartCycle() }

    const observer = new ResizeObserver(() => {
      const { width, height } = size()
      if (canvas.width === width && canvas.height === height) return
      canvas.width = width
      canvas.height = height
      viz.setRendererSize(width, height)
    })
    observer.observe(wrap)

    const levels = new Uint8Array(LEVELS_SAMPLES)
    const audioLevels = { timeByteArray: levels, timeByteArrayL: levels, timeByteArrayR: levels }
    let raf = 0
    let lost = false
    const draw = () => {
      if (lost) return
      raf = requestAnimationFrame(draw)
      const now = live.current
      let frame = null
      if (now.exactLive && now.isRelayed && now.playing) {
        const mode = now.liveSound ? 'live' : 'track'
        const queue = now.liveSound ? liveQueueRef.current : queueRef.current
        frame = queue.take(frameTarget({ mode, speaker: now, offsetSeconds: now.offsetSeconds, now: Date.now() }))
      }
      try {
        if (frame) {
          upsample(frame.w, levels)
          viz.render({ audioLevels })
        } else if (micRef.current) {
          viz.render()
        } else {
          if (now.playing) syntheticWave(performance.now() / 1000, levels)
          else silence(levels)
          viz.render({ audioLevels })
        }
      } catch { /* one bad frame; the next is drawn as usual */ }
    }
    raf = requestAnimationFrame(draw)

    // Lost: stop drawing and ask for the context back. Restored: the
    // renderer's textures and programs are gone, so build it all again on a
    // fresh canvas (a new key) rather than patch this one up.
    const onLost = (event) => {
      event.preventDefault()
      lost = true
      cancelAnimationFrame(raf)
      clearInterval(cycle)
    }
    const onRestored = () => setGeneration((g) => g + 1)
    canvas.addEventListener('webglcontextlost', onLost)
    canvas.addEventListener('webglcontextrestored', onRestored)

    return () => {
      canvas.removeEventListener('webglcontextlost', onLost)
      canvas.removeEventListener('webglcontextrestored', onRestored)
      cancelAnimationFrame(raf)
      clearInterval(cycle)
      observer.disconnect()
      nextRef.current = () => {}
      if (micRef.current) { try { viz.disconnectAudio(micRef.current.source) } catch { /* gone */ } }
      vizRef.current = null
      // Free the GPU now rather than whenever the canvas is collected: a TV
      // browser has little to spare.
      if (!lost) {
        try { canvas.getContext('webgl2')?.getExtension('WEBGL_lose_context')?.loseContext() } catch { /* fine */ }
      }
    }
  }, [generation])

  // The microphone ended from outside (unplugged, permission revoked): back
  // to the generic animation, and the button back for another try.
  function stopMicrophone() {
    const mine = micRef.current
    if (!mine) return
    micRef.current = null
    try { vizRef.current?.disconnectAudio(mine.source) } catch { /* gone */ }
    mine.stream.getTracks().forEach((track) => { track.onended = null; track.stop() })
    if (mounted.current) setMic('off')
  }

  function startMicrophone() {
    const audio = audioRef.current
    if (!audio || micRef.current || micPending.current) return
    micPending.current = true
    // resume() inside the tap itself: some browsers allow it only there.
    const resumed = audio.resume().catch(() => {})
    navigator.mediaDevices.getUserMedia({ audio: true }).then(async (stream) => {
      try {
        await resumed
        if (!mounted.current || audioRef.current !== audio) throw new Error('gone')
        const source = audio.createMediaStreamSource(stream)
        micRef.current = { stream, source }
        vizRef.current?.connectAudio(source)
        stream.getAudioTracks().forEach((track) => { track.onended = stopMicrophone })
        setMic('on')
      } catch {
        if (micRef.current?.stream === stream) micRef.current = null
        stream.getTracks().forEach((track) => track.stop())
        if (mounted.current) setMic('denied')
      }
    }, () => {
      if (mounted.current) setMic('denied')
    }).finally(() => { micPending.current = false })
  }

  if (unsupported) {
    return <div className="home-visualizer__message" role="status">{t('home.visualizer.unsupported')}</div>
  }

  const canMic = typeof window !== 'undefined' && window.isSecureContext
    && !!navigator.mediaDevices?.getUserMedia
  const showMic = canMic && mic === 'off' && !(exactLive && isRelayed)

  return (
    <div className="home-visualizer__stage" ref={wrapRef}>
      <canvas
        key={generation}
        ref={canvasRef}
        className="home-visualizer__canvas"
        role="img"
        aria-label={t('home.visualizer.label')}
        onClick={() => nextRef.current()}
      />
      <div className="home-visualizer__controls">
        {showMic && (
          <button type="button" className="home-visualizer__button" onClick={startMicrophone}>
            {t('home.visualizer.useMic')}
          </button>
        )}
        {mic === 'denied' && <span className="home-visualizer__note" role="status">{t('home.visualizer.micDenied')}</span>}
        <button type="button" className="home-visualizer__button" onClick={() => nextRef.current()}>
          {t('home.visualizer.nextPreset')}
        </button>
      </div>
    </div>
  )
}
