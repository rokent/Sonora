// The MilkDrop visualizer itself: Butterchurn on a WebGL canvas. Loaded only
// through Visualizer.jsx's React.lazy, so neither Butterchurn nor its presets
// are in the main bundle.
//
// Its audio is picked each animation frame, best first:
//   1. exact: the party's own audio, analysed on the server (/ws/visual),
//      when the server can (config `visuals`, or the socket not closing
//      4001) and the track passes through it (isRelayed). Frames are lined
//      up with the speaker's clock by visualFrames.js;
//   2. microphone: only on a secure page, after a tap on "Use microphone";
//   3. generic: a slow synthetic waveform pulsing at 120 BPM.
// While the speaker is paused (and no microphone listens) it is fed silence.
import React, { useEffect, useRef, useState } from 'react'
import butterchurnModule from 'butterchurn'
import presets from 'virtual:butterchurn-presets'
import { useI18n } from '../i18n/index.jsx'
import { request } from '../lib/api.js'
import { socketUrl } from '../lib/base.js'
import {
  LEVELS_SAMPLES, createFrameQueue, decodeFrame, silence, speakerTime, syntheticWave, upsample,
} from './visualFrames.js'
import './visualizer.css'

const butterchurn = butterchurnModule?.createVisualizer ? butterchurnModule : butterchurnModule.default

const PRESET_SECONDS = 30
const BLEND_SECONDS = 2.7
/** Drawing size cap: a TV at 4K still renders at most this wide. */
const MAX_WIDTH = 1920
/** Close codes after which the socket is not tried again for this party. */
const CLOSE_DISABLED = 4001
const CLOSE_UNKNOWN_PARTY = 4004
const CLOSE_NOT_ALLOWED = 1008

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
  partyId, trackUri, positionSeconds, positionAt, playing, isRelayed, offsetSeconds = 0,
}) {
  const { t } = useI18n()
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const vizRef = useRef(null)
  const audioRef = useRef(null)
  const micRef = useRef(null)
  const queueRef = useRef(null)
  if (!queueRef.current) queueRef.current = createFrameQueue()
  const nextRef = useRef(() => {})
  const [unsupported, setUnsupported] = useState(false)
  const [exactOff, setExactOff] = useState(false)
  const [exactLive, setExactLive] = useState(false)
  const [mic, setMic] = useState('off') // off | on | denied

  // What the animation loop reads, current without restarting it.
  const live = useRef({})
  live.current = { positionSeconds, positionAt, playing, isRelayed, offsetSeconds, exactLive }

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
    const queue = queueRef.current
    let socket = null
    let timer = null
    let stopped = false
    let backoff = 2000
    const connect = () => {
      socket = new WebSocket(socketUrl(`/ws/visual?party=${encodeURIComponent(partyId)}`))
      socket.onopen = () => { backoff = 2000; setExactLive(true) }
      socket.onmessage = (event) => queue.push(decodeFrame(event.data))
      socket.onclose = (event) => {
        setExactLive(false)
        queue.clear()
        if (stopped) return
        if (event.code === CLOSE_DISABLED) { setExactOff(true); return }
        if (event.code === CLOSE_NOT_ALLOWED) return
        // An unknown party may be one about to start: look again slowly.
        const wait = event.code === CLOSE_UNKNOWN_PARTY ? 15000 : backoff
        backoff = Math.min(30000, backoff * 2)
        timer = setTimeout(connect, wait)
      }
    }
    connect()
    return () => {
      stopped = true
      clearTimeout(timer)
      if (socket) { socket.onclose = null; socket.close() }
      setExactLive(false)
      queue.clear()
    }
  }, [partyId, isRelayed, exactOff])

  // A new track: whatever was queued belongs to the old one.
  useEffect(() => { queueRef.current.clear() }, [trackUri])

  // Butterchurn, its presets, and the animation loop.
  useEffect(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap) return undefined
    const Context = window.AudioContext || window.webkitAudioContext
    if (!webgl2Supported() || !Context || !butterchurn) { setUnsupported(true); return undefined }

    let viz
    const audio = new Context()
    audioRef.current = audio
    const size = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      let width = Math.max(1, Math.round(wrap.clientWidth * ratio))
      let height = Math.max(1, Math.round(wrap.clientHeight * ratio))
      if (width > MAX_WIDTH) { height = Math.round(height * MAX_WIDTH / width); width = MAX_WIDTH }
      return { width, height }
    }
    try {
      const { width, height } = size()
      canvas.width = width
      canvas.height = height
      viz = butterchurn.createVisualizer(audio, canvas, { width, height, pixelRatio: 1 })
    } catch {
      setUnsupported(true)
      audio.close().catch(() => {})
      return undefined
    }
    vizRef.current = viz

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
    const draw = () => {
      raf = requestAnimationFrame(draw)
      const now = live.current
      let frame = null
      if (now.exactLive && now.isRelayed && now.playing) {
        frame = queueRef.current.take(speakerTime(now) + (Number(now.offsetSeconds) || 0))
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
      } catch { /* a lost context: the next frame tries again */ }
    }
    raf = requestAnimationFrame(draw)

    return () => {
      cancelAnimationFrame(raf)
      clearInterval(cycle)
      observer.disconnect()
      nextRef.current = () => {}
      const mine = micRef.current
      if (mine) {
        try { viz.disconnectAudio(mine.source) } catch { /* gone */ }
        mine.stream.getTracks().forEach((track) => track.stop())
        micRef.current = null
      }
      vizRef.current = null
      audioRef.current = null
      audio.close().catch(() => {})
      // Free the GPU now rather than whenever the canvas is collected: a TV
      // browser has little to spare.
      try { canvas.getContext('webgl2')?.getExtension('WEBGL_lose_context')?.loseContext() } catch { /* fine */ }
    }
  }, [])

  async function startMicrophone() {
    const viz = vizRef.current
    const audio = audioRef.current
    if (!viz || !audio || micRef.current) return
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (vizRef.current !== viz) { stream.getTracks().forEach((track) => track.stop()); return }
      await audio.resume().catch(() => {})
      const source = audio.createMediaStreamSource(stream)
      viz.connectAudio(source)
      micRef.current = { stream, source }
      setMic('on')
    } catch {
      setMic('denied')
    }
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
