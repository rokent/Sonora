// Which party a TV screen follows. Pure, so it can be checked without a page
// (lib/tvParty.check.mjs).
//
// A party is running while it has rooms; an ended party leaves the list. A
// screen meant for one party shows that party or nothing ("No party
// playing"), and keeps listening. A screen for any party keeps the one it
// already shows while that one runs, so a second party starting does not
// pull the picture away; otherwise it takes the first party that is playing,
// then the first that is paused.
import { TV_MODES } from './tvKit.js'

const running = (party) => !!party?.id && Array.isArray(party.speakers) && party.speakers.length > 0

/**
 * -> the party to show, or null.
 * `wanted`: a party id, or null/'' for any party. `current`: the id shown now.
 */
export function pickParty(parties, wanted, current = null) {
  const live = (Array.isArray(parties) ? parties : []).filter(running)
  if (wanted) return live.find((p) => p.id === wanted) || null
  if (current) {
    const same = live.find((p) => p.id === current)
    if (same) return same
  }
  return live.find((p) => p.state !== 'paused') || live[0] || null
}

/** The mode to show: a valid one asked for, else the link's, else cover. */
export function pickMode(asked, fromLink) {
  if (TV_MODES.includes(asked)) return asked
  if (TV_MODES.includes(fromLink)) return fromLink
  return 'cover'
}
