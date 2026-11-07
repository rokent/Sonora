// node frontend/src/lib/visualFrames.check.mjs
// The visualizer's frame queue (home/visualFrames.js) lives with the home
// code; its check lives here, where test_js_checks.py runs every check.
import {
  FRAME_SAMPLES, LEVELS_SAMPLES, LIVE_DELAY_SECONDS, LIVE_QUEUE_MAX, createFrameQueue, decodeFrame,
  frameTarget, isLiveUri, liveDelay, liveStamper, pulse, silence, speakerTime, syntheticWave, upsample,
} from '../home/visualFrames.js'

let bad = 0
const is = (got, want, why) => {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) { bad++; console.log(`FAIL  ${why}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`) }
  else console.log(`ok    ${why}`)
}
const near = (got, want, why, eps = 1e-6) => is(Math.abs(got - want) < eps, true, `${why} (${got} ~ ${want})`)

const bytes = (v) => new Uint8Array(FRAME_SAMPLES).fill(v)
const b64 = (u8) => Buffer.from(u8).toString('base64')
const f = (t, v = 128) => ({ t, w: bytes(v) })

// speakerTime
near(speakerTime({ positionSeconds: 10, positionAt: 1000, playing: true }, 3500), 12.5, 'playing: the report plus its age')
near(speakerTime({ positionSeconds: 10, positionAt: 1000, playing: false }, 3500), 10, 'paused: exactly the report')
near(speakerTime({ positionSeconds: 10, positionAt: 5000, playing: true }, 3500), 10, 'a report from the future does not run backwards')

// decodeFrame
const good = decodeFrame(JSON.stringify({ t: 1.5, w: b64(bytes(7)) }))
is(good && [good.t, good.w.length, good.w[0]], [1.5, 512, 7], 'a frame decodes to its time and 512 samples')
is(decodeFrame(JSON.stringify({ t: 1, w: b64(new Uint8Array(10)) })), null, 'a short frame is refused')
is(decodeFrame('not json'), null, 'garbage is refused')
is(decodeFrame(JSON.stringify({ t: -1, w: b64(bytes(1)) })), null, 'a negative time is refused')
is(decodeFrame(JSON.stringify({ t: 1, w: '%%%' })), null, 'bad base64 is refused')

// alignment
let q = createFrameQueue()
for (let i = 0; i < 10; i++) q.push(f(10 + i / 30, i))
is(q.take(9.9), null, 'nothing yet when the speaker has not reached the first frame')
is(q.size, 10, 'and every frame is kept for later')
let got = q.take(10 + 4.5 / 30)
is(got && got.w[0], 4, 'the newest frame the speaker has reached is taken')
is(q.size, 6, 'the older ones are dropped')
got = q.take(10 + 4.6 / 30)
is(got && got.w[0], 4, 'the same frame holds until the next is due')
got = q.take(10 + 9 / 30 + 0.1)
is(got && got.w[0], 9, 'the last frame, a little late, still draws')

// offset: the caller adds it to the speaker time
q = createFrameQueue()
for (let i = 0; i < 10; i++) q.push(f(20 + i / 30, i))
const offset = 0.1
got = q.take(speakerTime({ positionSeconds: 20, positionAt: 0, playing: false }) + offset)
is(got && got.w[0], 3, 'speaker time plus the offset picks the frame 0.1 s on')

// stale
q = createFrameQueue()
q.push(f(5)); q.push(f(5.03))
is(q.take(6), null, 'a frame the speaker is a second past is stale and not drawn')
is(q.size, 0, 'and it is dropped')

// seek / skip
q = createFrameQueue()
q.push(f(100, 1)); q.push(f(100.03, 2)); q.push(f(0.5, 3))
is(q.size, 1, 'a frame earlier than the last means the server started over: the old ones go')
got = q.take(0.5)
is(got && got.w[0], 3, 'and the new position draws at once')
q.clear()
is(q.size, 0, 'clear empties the queue')

// cap
q = createFrameQueue({ max: 5 })
for (let i = 0; i < 9; i++) q.push(f(i))
is(q.size, 5, 'the queue holds at most its cap, the oldest going first')
is(q.take(4)?.t, 4, 'and the oldest kept is the fifth from last')

// live sound: which tracks
is(isLiveUri('x-rincon-mp3radio://10.0.0.2:8000/api/home/live/k3y_-1.mp3'), true, 'a live stream URI is live')
is(isLiveUri('http://10.0.0.2:8000/api/home/live/abc.mp3?x=1'), true, 'with a query too')
is(isLiveUri('http://10.0.0.2:8000/api/home/media/abcdefghijklmnop.m4a'), false, 'a YouTube relay is not live')
is(isLiveUri('http://10.0.0.2:8000/api/home/live/'), false, 'the bare prefix is not a stream')
is(isLiveUri(''), false, 'nothing is not live')

// live sound: the delay
is(LIVE_DELAY_SECONDS, 3, 'the live delay defaults to 3 s')
is(liveDelay(), 3, 'no offset: the default delay')
is(liveDelay(1), 2, 'a positive offset draws earlier')
is(liveDelay(-2), 5, 'a negative one later')
is(liveDelay(5), 0, 'never before the frame arrived')
is(liveDelay('junk'), 3, 'a junk offset is no offset')
is(LIVE_QUEUE_MAX >= (3 + 5) * 30, true, 'the live queue holds the longest delay')

// live sound: target ignores the speaker
near(frameTarget({ mode: 'live', speaker: { positionSeconds: 999, positionAt: 0, playing: true }, now: 10000 }),
  7, 'live: arrival clock minus the delay, whatever the speaker says')
near(frameTarget({ mode: 'live', offsetSeconds: 1, now: 10000 }), 8, 'live: the offset moves the delay')
near(frameTarget({ mode: 'track', speaker: { positionSeconds: 20, positionAt: 0, playing: false }, offsetSeconds: 0.5, now: 10000 }),
  20.5, 'track: speaker time plus the offset')

// live sound: stamps by arrival, t ignored for matching
let stamp = liveStamper()
is(stamp(f(1234.5), 100).t, 100, 'a live frame is stamped with its arrival, not its own t')
q = createFrameQueue({ max: LIVE_QUEUE_MAX })
stamp = liveStamper()
for (let i = 0; i < 30; i++) q.push(stamp(f(50 + i / 30, i), 10 + i / 30))
is(q.take(frameTarget({ mode: 'live', now: 12900 })), null, 'nothing drawn before the delay has passed')
got = q.take(frameTarget({ mode: 'live', now: 13000 + 10 * 1000 / 30 + 1 }))
is(got && got.w[0], 10, 'the frame that arrived 3 s ago is drawn')
got = q.take(frameTarget({ mode: 'live', offsetSeconds: -0.5, now: 13500 + 20 * 1000 / 30 + 1 }))
is(got && got.w[0], 20, 'a negative offset draws the same frame half a second later')

// live sound: a burst is spread by the frames' own spacing
stamp = liveStamper()
const burst = [0, 1, 2, 3].map((i) => stamp(f(7 + i / 30, i), 20))
near(burst[3].t - burst[0].t, 0.1, 'four frames arriving together are spread over their 0.1 s')
is(burst.every((fr, i) => i === 0 || fr.t > burst[i - 1].t), true, 'and stay in order')
q = createFrameQueue({ max: LIVE_QUEUE_MAX })
burst.forEach((fr) => q.push(fr))
got = q.take(20 + 1.5 / 30)
is(got && got.w[0], 1, 'so the second of the burst still gets its turn')

// live sound: stamps never run backwards, and a far-ahead spread snaps back
stamp = liveStamper()
const a1 = stamp(f(0, 1), 5)
const a2 = stamp(f(100, 2), 5.01)      // t jumped: not spread
const a3 = stamp(f(0, 3), 5.02)        // t went back (the source restarted)
is([a1.t, a2.t, a3.t], [5, 5.01, 5.02], 'jumps in t do not move the stamps')
stamp = liveStamper()
let s1 = null
for (let i = 0; i < 60; i++) s1 = stamp(f(i * 0.03, i), 1)  // 60 frames in one instant: 1.8 s of sound
is(s1.t >= 1 && s1.t <= 2, true, 'a burst is spread at most a second ahead of its arrival')
q = createFrameQueue({ max: LIVE_QUEUE_MAX })
stamp = liveStamper()
for (let i = 0; i < 60; i++) q.push(stamp(f(i * 0.03, i), 1))
is(q.size, 60, 'and no live frame is taken for a restart')

// upsample, silence, synthetic
const ramp = new Uint8Array(FRAME_SAMPLES).map((_, i) => i % 256)
const up = upsample(new Uint8Array(FRAME_SAMPLES).fill(200))
is([up.length, up[0], up[1023]], [LEVELS_SAMPLES, 200, 200], 'upsampling a flat frame stays flat at 1024 samples')
const up2 = upsample(ramp)
is([up2[0], up2[1023]], [0, ramp[511]], 'upsampling keeps the ends')
is(silence()[500], 128, 'silence sits at the midpoint')
is(pulse(0) > 0.99 && pulse(0.49) < 0.25, true, 'the pulse peaks on the beat and falls before the next')
near(pulse(0.1), pulse(0.6), 'and repeats every half second (120 BPM)')
const span = (w) => Math.max(...w) - Math.min(...w)
is(span(syntheticWave(0)) > span(syntheticWave(0.45)), true, 'the synthetic wave is louder on the beat')
is(syntheticWave(1.3).every((v) => v >= 0 && v <= 255), true, 'the synthetic wave stays in byte range')

if (bad) { console.log(`\n${bad} failed`); process.exit(1) }
console.log('\nall passed')
