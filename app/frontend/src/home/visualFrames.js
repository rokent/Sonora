// The visualizer's audio, as plain data: server frames lined up with the
// speaker's clock, and the stand-ins used when there are none.
//
// /ws/visual sends `{"t": <track seconds of the first sample>, "w": <base64
// of 512 unsigned 8-bit samples at 22050 Hz>}` up to 30 times a second. The
// server decodes ahead of the speaker (it leads by its own start-up time), so
// frames are queued here and each animation frame takes the newest one whose
// time the speaker has reached. One the speaker is already well past is stale
// -- a hiccup on the socket, or the old track after a skip -- and is dropped
// rather than drawn late.
//
// Live sound (a party playing Sonora's /api/home/live/ stream: AirPlay, the
// Windows sound card, a browser tab) has no track position to match: the
// speaker buffers the stream for a few seconds and reports no useful time.
// There the alignment is "live": each frame is stamped with when it arrived
// (liveStamper), and drawn a fixed delay later (LIVE_DELAY_SECONDS, moved by
// the same offset the video uses). Approximate, but in step once the delay
// matches the speaker's buffer.
//
// No DOM and no React, so it runs under node in lib/visualFrames.check.mjs.

export const FRAME_SAMPLES = 512
export const FRAME_RATE = 22050
/** What Butterchurn's audio buffers hold (its fftSize). */
export const LEVELS_SAMPLES = 1024
/** How far behind the speaker a frame may be and still be drawn, seconds. */
export const STALE_SECONDS = 0.25
/** Frames held at most, the oldest dropped first: 6 s at 30 a second.
 *
 * The assumption behind the size: frames should arrive about when the
 * speaker reaches them, since ffmpeg runs at real time (-re) from the
 * speaker's position. What can put them ahead is the start-up latency the
 * server leads by after a restart, capped at 5 s there (MAX_LEAD in
 * backend/home/analysis.py). The queue covers that with a second to spare;
 * a server leading by more would lose its oldest frames here, which show
 * up as the generic animation until the speaker catches up. */
export const QUEUE_MAX = 180
/** Live sound: how long after a frame arrives it is drawn, seconds. About
 * what a Sonos speaker buffers of an MP3 radio stream before playing it. */
export const LIVE_DELAY_SECONDS = 3
/** Live sound: frames held at most. The delay can reach 3 s minus the
 * offset's -5 s, 8 s, which is 240 frames at 30 a second; this keeps 10 s. */
export const LIVE_QUEUE_MAX = 300
/** Live stamps may run ahead of the arrival clock by this much while a
 * burst is spread out; more means the frames came in faster than real
 * time, and the stamps go back to the arrival time. */
const LIVE_SPREAD_MAX = 1

/** Whether a speaker's track URI is Sonora's live sound stream. */
export function isLiveUri(uri) {
  return /\/api\/home\/live\/[^/?#\s]+/.test(uri || '')
}

/** The delay for live frames with the viewer's offset, seconds: a positive
 * offset puts the picture earlier (as it does for the video), never before
 * the frame arrived. */
export function liveDelay(offsetSeconds = 0) {
  return Math.max(0, LIVE_DELAY_SECONDS - (Number(offsetSeconds) || 0))
}

/**
 * Stamps live frames with arrival time (seconds on the caller's clock) in
 * place of `t`, so a frame queue orders and picks them by arrival. Frames
 * that arrive together (one network read carrying several) are spread out
 * by their own spacing in `t` rather than stacked on one instant, so each
 * still gets drawn; `t` decides nothing else.
 */
export function liveStamper() {
  let last = null // { t, at }: the previous frame's own time and its stamp
  return function stamp(frame, arrivedSeconds) {
    if (!frame) return null
    let at = arrivedSeconds
    if (last) {
      const step = frame.t - last.t
      if (step > 0 && step < 0.5) {
        const spread = last.at + step
        if (spread > at && spread - at <= LIVE_SPREAD_MAX) at = spread
      }
      // Never earlier than the previous stamp: the queue would take that
      // for a restart and drop what it holds.
      at = Math.max(at, last.at)
    }
    last = { t: frame.t, at }
    return { t: at, w: frame.w }
  }
}

/** The time a queue is asked for: in "live" mode the arrival clock minus
 * the live delay; otherwise the speaker's track time plus the offset.
 * `now` is a ms epoch, as Date.now() gives. */
export function frameTarget({ mode = 'track', speaker, offsetSeconds = 0, now = Date.now() }) {
  if (mode === 'live') return now / 1000 - liveDelay(offsetSeconds)
  return speakerTime(speaker || {}, now) + (Number(offsetSeconds) || 0)
}

/** The speaker's position now, in seconds: the last report plus its age
 * while playing. `positionAt` is a ms epoch, as Date.now() gives. */
export function speakerTime({ positionSeconds, positionAt, playing }, now = Date.now()) {
  const base = Number(positionSeconds) || 0
  if (!playing || !positionAt) return Math.max(0, base)
  return Math.max(0, base + Math.max(0, now - positionAt) / 1000)
}

function base64Bytes(text) {
  const bin = atob(text)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/** A socket message as `{t, w: Uint8Array(512)}`, or null when it is not a
 * well-formed frame. */
export function decodeFrame(message) {
  let data = message
  if (typeof message === 'string') {
    try { data = JSON.parse(message) } catch { return null }
  }
  if (!data || typeof data !== 'object') return null
  const t = Number(data.t)
  if (!Number.isFinite(t) || t < 0 || typeof data.w !== 'string') return null
  let w
  try { w = base64Bytes(data.w) } catch { return null }
  if (w.length !== FRAME_SAMPLES) return null
  return { t, w }
}

/** A queue of decoded frames in track-time order. */
export function createFrameQueue({ max = QUEUE_MAX, stale = STALE_SECONDS } = {}) {
  let frames = []
  return {
    get size() { return frames.length },
    /** Add a frame. One older than the newest held means the server started
     * over (a seek or a skip): what was held belongs to the old position. */
    push(frame) {
      if (!frame) return
      const last = frames[frames.length - 1]
      if (last && frame.t < last.t) frames = []
      frames.push(frame)
      if (frames.length > max) frames.splice(0, frames.length - max)
    },
    /** The frame for track time `target` (speaker time plus the offset), or
     * null when none fits. Everything older than the frame returned goes,
     * and so does a frame the target is more than `stale` seconds past.
     * Frames still ahead of the target stay for later. */
    take(target) {
      let pick = -1
      for (let i = 0; i < frames.length; i++) {
        if (frames[i].t <= target) pick = i
        else break
      }
      if (pick < 0) return null
      const frame = frames[pick]
      frames.splice(0, pick)
      if (target - frame.t > stale) {
        frames.shift()
        return null
      }
      return frame
    },
    clear() { frames = [] },
  }
}

/** 512 samples at 22050 Hz stretched to Butterchurn's 1024 by linear
 * interpolation: about the same 23 ms at a 44.1/48 kHz audio context. */
export function upsample(samples, out = new Uint8Array(LEVELS_SAMPLES)) {
  const n = samples.length
  const ratio = (n - 1) / (out.length - 1)
  for (let i = 0; i < out.length; i++) {
    const x = i * ratio
    const j = Math.floor(x)
    const f = x - j
    const a = samples[j]
    const b = samples[Math.min(n - 1, j + 1)]
    out[i] = Math.round(a + (b - a) * f)
  }
  return out
}

/** Silence: every sample at the midpoint. */
export function silence(out = new Uint8Array(LEVELS_SAMPLES)) {
  out.fill(128)
  return out
}

const BEAT = 60 / 120

/** The pulse of the generic animation at `time` seconds: 1 on each beat of
 * 120 BPM, decaying towards a floor of 0.15 before the next. */
export function pulse(time) {
  const phase = ((time % BEAT) + BEAT) % BEAT / BEAT
  return 0.15 + 0.85 * Math.exp(-phase * 6)
}

/** A slow synthetic waveform for when there is nothing to listen to: a low
 * tone and a slower drifting one, their amplitude following pulse(). */
export function syntheticWave(time, out = new Uint8Array(LEVELS_SAMPLES)) {
  const amp = 100 * pulse(time)
  const drift = 0.5 + 0.5 * Math.sin(time * 0.37)
  for (let i = 0; i < out.length; i++) {
    const x = i / out.length
    const v = Math.sin(2 * Math.PI * (3 * x + time * 0.8)) * (0.7 - 0.3 * drift)
      + Math.sin(2 * Math.PI * (11 * x - time * 0.3)) * (0.3 * drift)
    out[i] = Math.max(0, Math.min(255, Math.round(128 + amp * v)))
  }
  return out
}
