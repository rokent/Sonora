// node frontend/src/lib/tvParty.check.mjs
//
// The party a TV screen follows (home/tvParty.js): a wanted party or none,
// any party with stickiness, and an ended party giving way to "No party
// playing" until a new one starts (Review Focus #1).
import { pickMode, pickParty } from '../home/tvParty.js'

let failures = 0
const is = (got, want, what) => {
  if (got !== want) { failures += 1; console.log(`FAIL ${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`) }
}
const id = (p) => (p ? p.id : null)

const a = { id: 'a', speakers: ['RINCON_1'], state: 'playing' }
const b = { id: 'b', speakers: ['RINCON_2'], state: 'playing' }
const pausedC = { id: 'c', speakers: ['RINCON_3'], state: 'paused' }
const empty = { id: 'e', speakers: [], state: 'playing' }

// Nothing to show.
is(pickParty([], null), null, 'no parties, any party')
is(pickParty(undefined, null), null, 'no list at all')
is(pickParty(null, 'a'), null, 'no list, wanted party')
is(pickParty([empty], null), null, 'a party with no rooms is not running')

// A wanted party.
is(id(pickParty([a, b], 'b')), 'b', 'the wanted party')
is(pickParty([a, b], 'x'), null, 'a wanted party that is not running shows nothing')
is(pickParty([{ ...empty, id: 'w' }], 'w'), null, 'a wanted party with no rooms shows nothing')
is(id(pickParty([a, b], 'b', 'a')), 'b', 'the wanted party wins over the current one')

// Any party.
is(id(pickParty([a, b], null)), 'a', 'any party: the first running')
is(id(pickParty([a, b], '')), 'a', 'an empty wanted id means any party')
is(id(pickParty([pausedC, b], null)), 'b', 'any party: a playing party before a paused one')
is(id(pickParty([pausedC], null)), 'c', 'any party: a paused party when none plays')
is(id(pickParty([empty, b], null)), 'b', 'any party skips one with no rooms')
is(id(pickParty([a, b], null, 'b')), 'b', 'any party keeps the one shown while it runs')
is(id(pickParty([a, pausedC], null, 'c')), 'c', 'even when it is paused')

// The party ends while the screen is open, then a new one starts.
let shown = pickParty([a], null)
is(id(shown), 'a', 'following party a')
shown = pickParty([], null, id(shown))
is(shown, null, 'party a ended: no party playing')
shown = pickParty([b], null, id(shown))
is(id(shown), 'b', 'a new party starts: the screen follows it')
shown = pickParty([b], null, 'a')
is(id(shown), 'b', 'a stale current id gives way to the running party')

// A link for one party: it ends, and a new party does not take over.
is(pickParty([b], 'a', 'a'), null, 'the wanted party ended: nothing, even with another running')

// Modes.
is(pickMode('video', 'cover'), 'video', 'an asked mode wins')
is(pickMode('nonsense', 'visualizer'), 'visualizer', 'an unknown asked mode falls back to the link')
is(pickMode(null, null), 'cover', 'nothing asked, nothing set: cover')
is(pickMode(undefined, 'bogus'), 'cover', 'a bad link mode: cover')

if (failures) process.exit(1)
console.log('tvParty: ok')
