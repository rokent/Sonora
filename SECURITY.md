# Security

Let's be safe!

## On Logins

**By default Sonora has no login. Anyone who can reach it on the network in a browser can control your speakers.**

That's a decision, not an oversight.  This is how Sonos' official apps behave (a huge convenience for the user), so Sonora does the same.

This fork can switch logins on. Set `SONORA_OIDC_ISSUER` and Sonora asks people to sign in through your Authentik (or another OIDC provider). Setup is in [docs/authentik.md](docs/authentik.md). With logins on:

- **Users** sign in with their own account. A session lasts 30 days. You can limit access to one group with `SONORA_OIDC_GROUP`. The group is checked only at sign-in: removing someone from the group does not end a session they already have. To sign everyone out at once, delete `<data dir>/home/secret.key` and restart Sonora.
- **Guests** use a secret link (`/join?token=...`), usually shown as a QR code. A guest session lasts 12 hours and can do less than a user. Making a new link ends all guest sessions that used the old one.
- **Home Assistant** uses a token. Keep it secret like a password.

If `SONORA_OIDC_ISSUER` is not set, logins are off, nobody is asked to sign in, and everyone is treated as "Local". That is the same as upstream.

Only set `SONORA_TRUST_PROXY=1` when Sonora is behind a reverse proxy you control. Otherwise it may trust forwarded headers that anyone could fake.

## Environments

Sonora is intended to be run on your own trusted local network.  Putting it out on the public internet is not recommended.

If you'd like to control your Sonos speakers with Sonora while away from home, please do so over a VPN.

## Other websites

No login doesn't mean any website can drive Sonora through your browser. Sonora refuses a change that another site asks your browser to make, and it answers only to its own names: addresses, `localhost`, local names like `sonora.local`, and the machine's own name. That stops a page from pointing a domain of its own at your Sonora (DNS rebinding).

If you reach Sonora by some other name, through a reverse proxy or a tunnel, add that name to `SONORA_ALLOWED_HOSTS`.

Installed themes are style and data only. A theme can't run code, and it can't load anything from elsewhere: its stylesheet may only use `data:` URLs.

## What the Network check shows

The Network check reads four status pages from each speaker (its radio, its network interfaces, its bridge and its Ethernet ports) and shows only numbers worked out from them: channel, signal, errors, response times. The pages themselves are never shown, logged or saved. Everything else it lists, like room names and speaker addresses, is already visible to anyone who can open Sonora.

## Reporting a vulnerability

Use GitHub's private reporting: open the repository's **Security** tab and choose **Report a vulnerability**, or go straight to
<https://github.com/Bretteroo/Sonora/security/advisories/new>. Your report will be hidden from public view.

I want Sonora to be a safe and trusted part of your homelab.  Thanks for reporting!
