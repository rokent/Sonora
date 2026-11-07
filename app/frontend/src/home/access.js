// Who sees which screen, kept apart from React so it can be checked on its
// own (access.check.mjs).

/** Where this device remembers the first name a guest joined with. */
export const GUEST_NAME_KEY = 'sonora.guestName'

/**
 * What the join page shows.
 *  - 'signedIn': a household member opened a guest link; they are not
 *    turned into a guest, they are pointed at the party page.
 *  - 'form': a guest link with its token: ask for a first name.
 *  - 'expired': no token, but this device joined as a guest before (its
 *    session ran out or the pass was changed): ask the host for a new link.
 *  - 'login': sent here from the party page with no session and no guest
 *    ever on this device: off to the household sign-in.
 *  - 'badLink': anything else.
 */
export function joinScreen({ token, expired, principalKind, rememberedName }) {
  if (principalKind === 'user') return 'signedIn'
  if (token) return 'form'
  if (rememberedName) return 'expired'
  if (expired) return 'login'
  return 'badLink'
}

/** Where the join page sends a household member to sign in. */
export function loginTarget(next) {
  const safe = typeof next === 'string' && next.startsWith('/') && !next.startsWith('//') ? next : '/party'
  return `/auth/login?next=${encodeURIComponent(safe)}`
}

/** Whether the controller shows the Parties button: to a signed-in member,
 *  and to everyone when sign-in is off (everyone is the household then). */
export function showPartiesButton({ auth, principalKind }) {
  return !auth || principalKind === 'user'
}

/** A TV's display session has no use for the controller: it goes to /tv. */
export function controllerRedirect({ principalKind }) {
  return principalKind === 'display' ? '/tv' : null
}
