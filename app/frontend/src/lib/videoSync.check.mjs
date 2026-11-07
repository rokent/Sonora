// node frontend/src/lib/videoSync.check.mjs
import {
  clampOffset, mediaKeyOf, playerCommand, positionAtTime, readPlayerMessage, shouldSeek, transportAnchor,
} from './videoSync.js'

let bad = 0
const is = (got, want, why) => {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) { bad++; console.log(`FAIL  ${why}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`) }
  else console.log(`ok    ${why}`)
}

is(mediaKeyOf('http://10.0.0.5:8000/api/home/media/abcdefghijklmnopQRST_-12.m4a'), 'abcdefghijklmnopQRST_-12', 'a relay URL gives its key')
is(mediaKeyOf('x-sonos-spotify:spotify%3atrack%3a1'), null, 'a Spotify track is not ours')
is(mediaKeyOf('http://h/api/home/media/short.m4a'), null, 'a too-short key is not ours')

is(transportAnchor({ state: 'PAUSED_PLAYBACK', rel_time: '0:01:05', received_at: 100 }),
  { positionSeconds: 65, positionAt: 100, playing: false }, 'a paused speaker is where it says')
is(transportAnchor({ state: 'PLAYING', rel_time: '0:00:10', position_age: 2, received_at: 50 }),
  { positionSeconds: 12.5, positionAt: 50, playing: true }, 'a playing one adds the reading age and half a second')

is(positionAtTime({ positionSeconds: 10, positionAt: 1000, playing: true }, 4000), 13, 'playing: three seconds later is three seconds on')
is(positionAtTime({ positionSeconds: 10, positionAt: 1000, playing: false }, 4000), 10, 'paused: it stays put')
is(positionAtTime({ positionSeconds: 10, positionAt: 5000, playing: true }, 4000), 10, 'a reading from the future does not move it back')

is(clampOffset(9), 5, 'offset is at most +5')
is(clampOffset(-7.3), -5, 'offset is at least -5')
is(clampOffset(1.13), 1.25, 'offset lands on a quarter second')
is(clampOffset('junk'), 0, 'junk offset is 0')
is(Object.is(clampOffset(-0.1), -0), false, 'a tiny negative offset is plain 0')

is(shouldSeek({ target: 20, videoTime: 21, lastSeekAt: -Infinity, now: 10000 }), false, 'within 1.5 s: leave it')
is(shouldSeek({ target: 20, videoTime: 22, lastSeekAt: -Infinity, now: 10000 }), true, 'over 1.5 s: seek')
is(shouldSeek({ target: 20, videoTime: 17, lastSeekAt: 6000, now: 10000 }), false, 'but not within 5 s of the last seek')
is(shouldSeek({ target: 20, videoTime: 17, lastSeekAt: 5000, now: 10000 }), true, 'five seconds later it may')
is(shouldSeek({ target: 20, videoTime: undefined, lastSeekAt: undefined, now: 0 }), true, 'an unknown video time is sought')

is(JSON.parse(playerCommand('seekTo', [12.5, true])), { event: 'command', func: 'seekTo', args: [12.5, true] }, 'seek command shape')
is(readPlayerMessage('{"event":"infoDelivery","info":{"currentTime":3.2,"playerState":1}}'), { time: 3.2, state: 1 }, 'infoDelivery is read')
is(readPlayerMessage('{"event":"onReady","info":null}'), { ready: true }, 'onReady is read')
is(readPlayerMessage('not json'), null, 'other messages are ignored')
is(readPlayerMessage({ event: 'infoDelivery', info: { volume: 0 } }), {}, 'infoDelivery without time is empty')

if (bad) { console.log(`${bad} failed`); process.exit(1) }
