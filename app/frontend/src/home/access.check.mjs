// node frontend/src/home/access.check.mjs
import { joinScreen, loginTarget, showPartiesButton, controllerRedirect } from './access.js'

let bad = 0
const is = (got, want, why) => {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) { bad++; console.log(`FAIL  ${why}\n      got  ${JSON.stringify(got)}\n      want ${JSON.stringify(want)}`) }
  else console.log(`ok    ${why}`)
}

is(joinScreen({ token: 'abc', expired: false, principalKind: 'user', rememberedName: '' }), 'signedIn',
   'a signed-in member opening a guest link is not made a guest')
is(joinScreen({ token: 'abc', expired: false, principalKind: null, rememberedName: 'Ana' }), 'form',
   'a guest link with a token asks for a name')
is(joinScreen({ token: 'abc', expired: false, principalKind: 'guest', rememberedName: 'Ana' }), 'form',
   'a guest with a fresh link can join again')
is(joinScreen({ token: '', expired: true, principalKind: null, rememberedName: 'Ana' }), 'expired',
   'a guest whose session ran out is told to ask the host')
is(joinScreen({ token: '', expired: false, principalKind: null, rememberedName: 'Ana' }), 'expired',
   'a remembered guest without a token is told to ask the host')
is(joinScreen({ token: '', expired: true, principalKind: null, rememberedName: '' }), 'login',
   'no guest ever on this device: the household sign-in')
is(joinScreen({ token: '', expired: false, principalKind: null, rememberedName: '' }), 'badLink',
   'a bare /join is a bad link')
is(loginTarget('/party'), '/auth/login?next=%2Fparty', 'sign-in comes back to the party page')
is(loginTarget('//evil.example'), '/auth/login?next=%2Fparty', 'a foreign next is not followed')
is(loginTarget(null), '/auth/login?next=%2Fparty', 'no next: the party page')
is(showPartiesButton({ auth: false, principalKind: 'user' }), true, 'auth off: everyone gets Parties')
is(showPartiesButton({ auth: false, principalKind: null }), true, 'auth off, no principal read: Parties all the same')
is(showPartiesButton({ auth: true, principalKind: 'user' }), true, 'a signed-in member gets Parties')
is(showPartiesButton({ auth: true, principalKind: 'display' }), false, 'a display does not')
is(controllerRedirect({ principalKind: 'display' }), '/tv', 'a display opening / goes to /tv')
is(controllerRedirect({ principalKind: 'user' }), null, 'a member stays on the controller')
is(controllerRedirect({ principalKind: null }), null, 'nobody known: no redirect')

if (bad) { console.log(`\n${bad} failed`); process.exit(1) }
console.log('\nall passed')
