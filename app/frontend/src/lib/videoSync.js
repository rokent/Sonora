// Keeping a muted YouTube embed in step with the speaker playing the same
// video's audio. Pure decisions only, so they can be checked without a page:
// the component (home/VideoWindow.jsx) owns the iframe and the clock.
//
// The speaker plays our relay of the video's own audio file, so its track
// position is the video time; the viewer can nudge the picture by an offset
// (a TV's own picture delay, a slow phone).

export const DRIFT_LIMIT_S = 1.5
export const SEEK_GAP_MS = 5000
export const OFFSET_MIN = -5
export const OFFSET_MAX = 5
export const OFFSET_STEP = 0.25
export const OFFSET_KEY = 'sonora.videoOffset'

/** Our relay URL for a YouTube item, as the speaker reports it: its key.
 * The item is .m4a, or .aac with SONORA_YOUTUBE_FORMAT=aac (backend
 * home/media.py). */
const MEDIA_URI = /\/api\/home\/media\/([A-Za-z0-9_-]{16,64})\.(?:m4a|aac)(?:$|[?#])/
export function mediaKeyOf(uri) {
  const m = MEDIA_URI.exec(uri || '')
  return m ? m[1] : null
}

/** H:MM:SS (or M:SS) to seconds; anything else is 0. */
function seconds(value) {
  if (!value || typeof value !== 'string') return 0
  const parts = value.split(':').map(Number)
  if (parts.some(Number.isNaN)) return 0
  return parts.reduce((total, part) => total * 60 + part, 0)
}

/**
 * A speaker's transport state as an anchor for a clock: the position at the
 * moment `positionAt` (a performance.now() reading, in ms) and whether it is
 * moving. Mirrors lib/format.js positionNow({ exact: true }): the reported
 * whole second, its age when sent, and half a second for the truncation.
 */
export function transportAnchor(tr, fallbackAt = 0) {
  const playing = tr?.state === 'PLAYING' || tr?.state === 'TRANSITIONING'
  const base = seconds(tr?.rel_time)
  const age = playing ? (Number(tr?.position_age) || 0) + 0.5 : 0
  return { positionSeconds: Math.max(0, base + age), positionAt: tr?.received_at || fallbackAt, playing }
}

/** Where the speaker is at `now` (ms, same clock as positionAt). */
export function positionAtTime({ positionSeconds, positionAt, playing }, now) {
  const base = Number(positionSeconds) || 0
  if (!playing || !Number.isFinite(positionAt)) return Math.max(0, base)
  return Math.max(0, base + Math.max(0, now - positionAt) / 1000)
}

/** An offset as the slider allows it: within ±5 s, on a quarter second. */
export function clampOffset(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 0
  const stepped = Math.round(n / OFFSET_STEP) * OFFSET_STEP
  return Math.min(OFFSET_MAX, Math.max(OFFSET_MIN, stepped)) || 0
}

/**
 * Seek the video now? Only when it is more than 1.5 s from where it should
 * be, and not within 5 s of the last seek (a seek takes a moment to land, and
 * re-seeking while it does only makes the picture stutter). A video whose
 * time is unknown is sought as soon as the gap allows.
 */
export function shouldSeek({ target, videoTime, lastSeekAt, now }) {
  if (!Number.isFinite(target)) return false
  if (Number.isFinite(lastSeekAt) && now - lastSeekAt < SEEK_GAP_MS) return false
  if (!Number.isFinite(videoTime)) return true
  return Math.abs(videoTime - target) > DRIFT_LIMIT_S
}

/** One command for the embed's postMessage interface (no iframe API script). */
export function playerCommand(func, args = []) {
  return JSON.stringify({ event: 'command', func, args })
}

/**
 * What the embed told us, from one message event's data: `{ time, state }`
 * from an infoDelivery (either may be missing), `{ ready: true }` for
 * onReady, otherwise null. YouTube sends JSON strings.
 */
export function readPlayerMessage(data) {
  let msg = data
  if (typeof msg === 'string') {
    try { msg = JSON.parse(msg) } catch { return null }
  }
  if (!msg || typeof msg !== 'object') return null
  if (msg.event === 'onReady') return { ready: true }
  if (msg.event === 'infoDelivery' && msg.info && typeof msg.info === 'object') {
    const out = {}
    if (Number.isFinite(msg.info.currentTime)) out.time = msg.info.currentTime
    if (Number.isFinite(msg.info.playerState)) out.state = msg.info.playerState
    return out
  }
  if (msg.event === 'onStateChange' && Number.isFinite(msg.info)) return { state: msg.info }
  return null
}
