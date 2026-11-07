# Signing in with Authentik

Sonora Home can ask people to sign in using Authentik, or any other OpenID Connect (OIDC) provider. This guide uses Authentik.

If you skip this, Sonora stays login-free, just like upstream.

## 1. Create the provider in Authentik

1. In Authentik, go to **Applications > Providers** and create an **OAuth2/OpenID Provider**.
2. Set **Client type** to **Confidential**.
3. Copy the **Client ID** and **Client Secret**. You need them below.
4. Under **Redirect URIs/Origins**, add both addresses (strict matching):
   - `https://<proxy-host>/auth/callback` (the name you use through your reverse proxy)
   - `http://apps-vm:50205/auth/callback` (direct access on your network)
5. Under **Scopes**, select `openid`, `profile`, `email` and `groups`. Sonora asks for exactly these.
6. Save, then create an **Application** that uses this provider. Its slug becomes part of the issuer address.

## 2. Limit who can sign in (optional)

Make a group in Authentik, for example `sonora-users`, and add the people you want. Set `SONORA_OIDC_GROUP` to that name. Anyone who signs in without the group is turned away. Leave it unset to let every Authentik user in.

## 3. Tell Sonora

Set these environment variables (see the commented lines in `docker-compose.yml`):

| Variable | What it does |
| --- | --- |
| `SONORA_OIDC_ISSUER` | The issuer address, like `https://authentik.example.com/application/o/sonora/`. Setting it turns logins on. Unset means logins are off and everyone is "Local". |
| `SONORA_OIDC_CLIENT_ID` | The Client ID from the provider. |
| `SONORA_OIDC_CLIENT_SECRET` | The Client Secret from the provider. |
| `SONORA_OIDC_GROUP` | Optional. Only members of this group can sign in. |
| `SONORA_TRUST_PROXY` | Set to `1` when Sonora is behind a reverse proxy you trust, so it builds the right callback address. |

Restart Sonora after changing them.

## Sessions and guests

- A signed-in session lasts 30 days. Sonora checks the group only when someone signs in, so removing a person from the Authentik group (or disabling them in Authentik) does not end a session they already have: they stay signed in until it runs out.
- To sign everyone out at once, stop Sonora, delete `<data dir>/home/secret.key`, and start it again. Sonora makes a new key, every session (users, guests and TV displays) stops working: users sign in again, guests scan the guest link again, and each TV opens its display link again. Home Assistant tokens, the guest link and display links keep working.
- Guests join with the link `/join?token=...` (shown as a QR code). A guest session lasts 12 hours.
- Making a new guest link ends every guest session that used the old one.

## If sign-in fails

- "Redirect URI mismatch" in Authentik: the callback address must match one of the two above exactly, including `http` or `https`.
- Behind a reverse proxy and still getting the wrong callback address: set `SONORA_TRUST_PROXY=1`.
- Turned away after a good login: check the user is in the group named in `SONORA_OIDC_GROUP`, and that the `groups` scope is enabled.
