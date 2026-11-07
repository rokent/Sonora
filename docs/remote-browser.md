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
      NEKO_WEBRTC_EPR: 52000-52100
      NEKO_WEBRTC_ICELITE: 1
    volumes:
      - ./neko-policies.json:/etc/chromium/policies/managed/policies.json:ro
```

Without host networking, drop `network_mode`, publish `8080:8080` and `52000-52100:52000-52100/udp`, and add `NEKO_WEBRTC_NAT1TO1: <the machine's LAN address>`.

## Passwords and the view-only member

`NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD` signs in as an admin, `NEKO_MEMBER_MULTIUSER_USER_PASSWORD` as a regular member who can take control. To make guests watch-only, either:

* give them a member that has no control right: in neko an admin can take away a member's "can host" permission in the room settings, or
* leave control unclaimed: guests join and watch while a member holds control (neko gives control to one person at a time).

Then tell Sonora which link each kind of visitor gets:

```
SONORA_NEKO_URL=http://sonora.local:8080/?usr=host&pwd=change-me-admin
SONORA_NEKO_VIEW_URL=http://sonora.local:8080/?usr=guest&pwd=change-me-user
```

`GET /api/home/config` returns `SONORA_NEKO_URL` to household members and `SONORA_NEKO_VIEW_URL` to guests (nothing if unset, so a guest is never handed the full-control link). Both end up in the page, so use passwords that are only for this.

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
