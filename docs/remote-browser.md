# Shared remote browser (neko)

Sonora Home can embed a shared browser, so the household signs in to YouTube once and everyone at the party can watch, or members can drive it. It is [neko](https://github.com/m1k1o/neko) running Chromium, beside Sonora.

## Compose

```yaml
services:
  neko:
    image: ghcr.io/m1k1o/neko/chromium:latest
    container_name: neko
    restart: unless-stopped
    shm_size: 2gb
    cap_add: [SYS_ADMIN]
    network_mode: host            # simplest: WebRTC needs UDP to the browser
    environment:
      NEKO_DESKTOP_SCREEN: 1920x1080@30
      NEKO_MEMBER_PROVIDER: multiuser
      NEKO_MEMBER_MULTIUSER_USER_PASSWORD: change-me-user
      NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD: change-me-admin
      # The guest (view) login can watch but never take control. neko enforces
      # this, not Sonora: every field is spelled out so nothing is left to a
      # default.
      NEKO_MEMBER_MULTIUSER_USER_PROFILE: >-
        {"name":"","is_admin":false,"can_login":true,"can_connect":true,
        "can_watch":true,"can_host":false,"can_share_media":false,
        "can_access_clipboard":false,"sends_inactive_cursor":false,
        "can_see_inactive_cursors":false}
      NEKO_WEBRTC_EPR: 52000-52100
      NEKO_WEBRTC_ICELITE: 1
    volumes:
      - ./neko-policies.json:/etc/chromium/policies/managed/policies.json:ro
```

Without host networking, drop `network_mode`, publish `8080:8080` and `52000-52100:52000-52100/udp`, and add `NEKO_WEBRTC_NAT1TO1: <the machine's LAN address>`.

## Passwords and the view-only member

neko's `multiuser` provider has two logins: `NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD`
signs in as an admin, `NEKO_MEMBER_MULTIUSER_USER_PASSWORD` as a regular user.
Out of the box a regular user may take control (`can_host: true` in neko's
default user profile), so a guest handed that login could drive the browser.

**Watch-only for guests is enforced by neko's configuration, not by Sonora.**
Sonora only decides which link a visitor is given; whatever the link's login
allows, neko allows. Set the regular user's profile so it can watch but not
host, with `NEKO_MEMBER_MULTIUSER_USER_PROFILE` as in the compose file above
(neko v3, the `ghcr.io/m1k1o/neko/chromium` image; `member.multiuser.user_profile`
in neko's `config.yaml` is the same setting):

```json
{"name": "", "is_admin": false, "can_login": true, "can_connect": true,
 "can_watch": true, "can_host": false, "can_share_media": false,
 "can_access_clipboard": false, "sends_inactive_cursor": false,
 "can_see_inactive_cursors": false}
```

Household members sign in with the admin login, which keeps `can_host`.
Check it after a change: open the view link in a private window and try to
take control; neko should refuse.

Then tell Sonora which link each kind of visitor gets:

```
SONORA_NEKO_URL=http://sonora.local:8080/?usr=host&pwd=change-me-admin
SONORA_NEKO_VIEW_URL=http://sonora.local:8080/?usr=guest&pwd=change-me-user
```

`GET /api/home/config` returns `SONORA_NEKO_URL` to household members and `SONORA_NEKO_VIEW_URL` to guests (nothing if unset, so a guest is never handed the full-control link). Both end up in the page, so use passwords that are only for this.

## HTTPS

When Sonora is served over HTTPS (behind a reverse proxy, or with sign-in),
an `http://` neko URL inside it is mixed content and browsers block the
embedded frame. Put neko behind the same HTTPS proxy (for example
`https://neko.example.com/`, proxying the websocket too) and use `https://`
in `SONORA_NEKO_URL` and `SONORA_NEKO_VIEW_URL`. WebRTC media still flows
over the UDP ports above, not through the proxy.

## Restricting Chromium to YouTube

Save as `neko-policies.json` (mounted above):

```json
{
  "URLBlocklist": ["*"],
  "URLAllowlist": [
    "youtube.com",
    "*.youtube.com",
    "*.googlevideo.com",
    "accounts.google.com"
  ]
}
```

If playback or thumbnails fail, YouTube may also need `*.ytimg.com`, `*.ggpht.com`, `*.googleusercontent.com` and `*.gstatic.com` in the allowlist.

The allowlist wins over the blocklist, so only those hosts load.
