// node frontend/src/lib/visualFrames.check.mjs
// The visualizer's frame queue (home/visualFrames.js) lives with the home
// code; its check lives here, where test_js_checks.py runs every check.
import {
  FRAME_SAMPLES, LEVELS_SAMPLES, createFrameQueue, decodeFrame, pulse,
  silence, speakerTime, syntheticWave, upsample,
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
