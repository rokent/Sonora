# Sonora Home — design

Date: 2026-11-07 · Status: owner review (builder check done) · Working name: **Sonora Home** (builder's call, rename freely)

## 1. Intent

The owner rarely uses their Sonos speakers because the official Sonos app doesn't give them the experience they want. They want one self-hosted app, running on their home server, that:

- fully controls every Sonos speaker in the house, locally;
- lets several people use it at the same time, including guests;
- turns the house into a "party" system: shared, mixed-source queues on chosen speakers, with video and visuals on screens and TVs;
- lets any PC or phone send its sound to any speaker or party;
- plugs into Home Assistant.

It is built as a **fork of Sonora** (https://github.com/Bretteroo/Sonora, AGPL-3.0-only), a self-hosted Sonos web controller: Python ≥3.11 / FastAPI backend (`app/backend`, own SOAP/UPnP implementation in `app/backend/sonos/`, one large `main.py` with REST endpoints + a `/ws` WebSocket that broadcasts state snapshots), React 19 + Vite 8 frontend (`app/frontend`), Docker image with `network_mode: host`, a Home Assistant add-on with ingress (`homeassistant/`), an Unraid template, a theme engine, and "Scenes" (layered multi-room setups that restore the previous state when they end). Sonora has **no login** today (see its SECURITY.md); it guards against cross-site requests and DNS rebinding via allowed hosts (`SONORA_ALLOWED_HOSTS`).

## 2. Goals

1. **Local only.** Discover and control speakers on the LAN (SSDP/mDNS, SOAP on :1400, UPnP event subscriptions). No dependency on Sonos cloud for core features. Sonora's optional Sonos sign-in may remain for the extras it unlocks.
2. **Full control** of every speaker and group, S1 and S2 — everything Sonora already does stays working.
3. **Multi-user, concurrent.** Many people use it at once; changes are live for everyone (Sonora's WebSocket model). No locking: the last action wins.
4. **Accounts:** named users via Authentik; one shared guest account with restricted rights.
5. **Parties** with presets, mixed-source queues, speaker ownership, and restore-on-end.
6. **YouTube** played by the server itself (audio streamed directly from YouTube, nothing stored), with a remote-browser view in the app.
7. **TV mode** page (video / visualizer / cover + lyrics) usable in the app, on TVs, and in Home Assistant; the app drives the owner's Samsung TVs directly.
8. **Visualizer** (Winamp/MilkDrop style).
9. **Sound streaming** from PCs and phones to any speaker or party: AirPlay receivers, Windows network sound card, in-app file drop and tab sound.
10. **Home Assistant integration:** start/stop parties (presets), see party state, trigger "show on TV".

## 3. Non-goals

- Setting up brand-new speakers (out-of-box onboarding). New speakers are set up once in the official Sonos app; the app then discovers them automatically. Do not reverse-engineer Sonos setup.
- Linking music service accounts. Services (Spotify etc.) are linked in a Sonos app; the app reuses the logins stored on the speakers (as Sonora does).
- Grouping S1 and S2 speakers together (impossible on Sonos).
- Duplicating Home Assistant's built-in Sonos integration (it already controls speakers locally). The HA integration adds only what HA can't do: parties, presets, TV mode, extra sources.
- Public-internet exposure. LAN + VPN only (same stance as Sonora).
- Real Google Cast receiver support (requires Google certification).
- Spotify played by the server (librespot). Possible later "party mode" extra, **not in scope**.
- Downloading YouTube audio as files (stronger ToS breach, storing copies). Not in scope.

## 4. Anti-goals (would be a failure even if it "works")

- Music stops when someone closes the app on their device. **The server does the playing; devices are remotes.** Exception: streaming a device's own sound naturally needs that device.
- Guests able to regroup speakers, steal speakers, change settings, or start parties.
- Breaking existing Sonora behaviour for normal single-room use.
- Requiring Home Assistant for any feature (HA is optional; every HA action is also in the app).

## 5. Constraints

- **Host:** runs as a Docker container on the owner's **apps-vm** (a normal x86 VM, not a Raspberry Pi). Host networking stays required (speakers must reach the server; AirPlay/mDNS need it too).
- **Network:** speakers and apps-vm must be on the same L2 segment, or the owner's UniFi network must relay mDNS/SSDP between VLANs. Verify before relying on discovery.
- **Two ways in (owner decision):** HTTPS through the owner's reverse proxy **and** the direct LAN address (`http://apps-vm:50205`). Both go through the same app login (§6). Tab-audio capture and microphone visuals need HTTPS, so on the plain-HTTP address those two features are hidden with a hint to use the HTTPS address. Authentik must accept redirect URIs for both.
- **Guests are on the same Wi-Fi/VLAN as the speakers** (owner decision), so they can reach the app and see the AirPlay speakers directly.
- **License:** AGPL-3.0-only. Fine for private use. If others use a modified version over the network, they must be offered its source. Keep the fork's changes isolated where practical so pieces can be offered back upstream as PRs (owner agreed).
- **Upstream moves fast** (~200 commits in its first month). Prefer adding new modules/routes/components over editing Sonora's existing files; keep a clean way to merge upstream.
- **Sonos live-stream delay:** anything the server streams to a speaker (YouTube, PC/phone sound, server-served files) plays ~2–5 s behind the source because speakers buffer for Wi-Fi resilience and group sync. This cannot be turned off. Tuning (format, stream type) may reduce it; measure it. All video/visual sync must compensate (§9, §10).
- **S1 and S2:** a party (and a preset) uses speakers from **one Sonos system only**, since S1 and S2 can't be grouped (owner agreed). The speaker picker shows which system a speaker belongs to and greys out the other system once one is chosen.
- **Owner's devices:** mixed Windows / macOS / Linux PCs, iPhones and Android phones; **two Samsung TVs**, no Chromecast/Fire TV sticks.

## 6. Accounts and permissions

- **Named users** log in through **Authentik** (OIDC). All named users have the same powers; the owner manages who has access (through Authentik group membership — builder's call on the exact mapping).
- **One shared guest account**, local to the app (guests don't have Authentik accounts). Guests get in with a **QR code or link**, no password typing. Builder's call: how the guest link is protected and rotated (e.g. a token in the link, regenerated by a named user).
- **Guest rights:** join a running party; add songs to its queue; play/pause/skip; change volume. **Cannot:** start parties, create/edit presets, move speakers between parties, change speaker groups, add/remove speakers, change EQ, alarms, scenes, or settings, use the remote browser beyond viewing.
- **Named-user rights:** everything.
- Permissions are enforced **on the server**, not only hidden in the UI.
- Every action is attributed to a user (needed for "added by" in queues).
- **Guest display name** *(Claude's call)*: on joining, a guest types a first name (remembered on that device); queue items show "Ana (guest)". Guests may remove **only their own** queue items; named users may remove/reorder anything.
- **Authentik wiring** *(Claude's call)*: the app does **OIDC itself** (Authentik as provider); the reverse proxy only passes traffic through. Reason: the app must serve unauthenticated routes anyway (guest join, speaker media URLs, HA), and needs per-user identity. Every entry point (proxy, direct LAN port, HA ingress) goes through the same app-level auth; the only unauthenticated routes are the guest-join exchange and the media URLs speakers fetch (unguessable per-item tokens).
- **HA add-on ingress** *(Claude's call)*: an ingress user is treated as a named user only if they also log in; otherwise the add-on path is not a primary target (see §11).

## 7. Normal mode (outside a party)

Works like a normal Sonos controller — Sonora's existing behaviour: pick a room or group, play something, control it. No shared party queue. Guests outside a party: builder's call (suggest: guests only see a "join a party" screen when a party is running, and otherwise volume/play-pause for rooms — confirm with owner if unclear).

## 8. Parties

- A named user presses **Start a party**, picks speakers (or a preset). The app groups those speakers for the party.
- A party has **its own queue**, mixing sources: Spotify and other Sonos-linked services, local/NAS library, YouTube (§9), files dropped from PCs/phones, and live device sound (§12).
- Each queue item shows **who added it**.
- **Speaker ownership:** a speaker belongs to at most one party. Picking a speaker that's already in another party asks "Move *Living Room* from *Ana's party* to yours?"; on confirm it moves. If a party loses its last speaker, it **pauses and keeps its queue** so it can resume on other speakers.
- **Ending a party** restores the speakers to how they were before (previous groups and what was playing) — reuse/extend Sonora's Scenes restore logic.
- **Several parties at once** on different speakers are allowed.
- **Changes from outside the party** *(Claude's call, follows "last action wins")*: if the official Sonos app, HA's Sonos integration, or a named user in normal mode regroups a party speaker or plays something else on it, the party **follows reality**: that speaker leaves the party (everyone sees a notice "Kitchen left the party"). The party never fights to take it back. Party state is derived from what the speakers report.
- **Restore edge cases** *(Claude's call)*: each speaker keeps a stack of snapshots. When party B ends, a speaker that was moved from party A returns to A if A is still running, otherwise to its state before any party. Snapshots include groups, volume, what was playing, and the full previous Sonos queue (reuse Sonora's Scenes restore).
- **Guests join** a running party; they can't start one.

### 8.1 Presets

- Saved by named users; started with **two taps** in the app (pick preset → Start) and with **one action from Home Assistant**.
- A preset holds: name; speakers; starting volume per speaker; optional starting music (e.g. a Spotify playlist, NAS folder, YouTube playlist) that guests' songs then join. Optional (builder's call): which TV mode content to show on which TV when it starts.
- Started from the app: same "move speaker?" question for speakers in another party. Started from **Home Assistant: takes the speakers without asking** (nobody to ask).

### 8.2 Queue mechanics (builder's call, constraints)

Sonos queues natively mix tracks from different services and the local library. Items the server must produce itself (YouTube, dropped files, live sound) enter the Sonos queue as HTTP URLs served by the app. The builder decides whether the party queue is the Sonos queue (with app-side metadata such as "added by") or an app-managed queue that feeds the speaker; the constraints are: survives app restarts, live for every viewer, and the official Sonos app sees something sensible.

## 9. YouTube on the server

- Anyone in a party (guests included) can search/paste a YouTube video and add it to the queue, then close their device.
- **Audio path (owner decision 2026-11-07): direct stream.** When the speaker reaches the item, the server resolves the video's audio stream directly from YouTube (yt-dlp-style extraction) and relays it to the speaker over HTTP, transcoding only if the speaker can't play the format. **Nothing is stored on disk.** No hidden browser is involved in playback.
- **Controls:** play, pause, skip, volume, and **seek** must work (the relay can restart at an offset / use range requests; a short gap is acceptable).
- **Remote browser view:** separately, the app can show a server-side shared browser (like the open-source **neko** project, WebRTC) for browsing/searching YouTube together. Named users can interact; **guests: view only**; lock it to YouTube. It is a nice-to-have for this phase, not needed for playback. (A Premium login is only relevant to that browser; the direct stream carries no ads.)
- **Video window:** viewers see the video as a muted copy played on their own screen (or in TV mode), kept in step with the speaker.
- Video shown to viewers is **delayed to match the speaker delay**, with a manual sync adjuster.
- Owner accepted: this breaches YouTube's terms and the extraction will need maintenance when YouTube changes.

## 10. Visualizer and TV mode

### 10.1 TV mode page

One page, opened anywhere: full-screen in the app on any device, in a TV's browser, or in a Home Assistant dashboard card (iframe). Per party (and per screen) it shows: the YouTube video, the visualizer, or cover art + lyrics. Follows the party's current track automatically.

### 10.2 Visualizer

- MilkDrop-style visuals in the browser (e.g. **Butterchurn**, a WebGL MilkDrop port with the classic presets).
- **Exact visuals** for sound that passes through the server: YouTube, live device sound, server-served local/NAS files. The server analyses the audio and sends viewers only beat/frequency data (small, ~60 updates/s), **not** a second audio stream; timed to the speaker delay.
- Local/NAS library songs: to get exact visuals, the server may serve those files to the speaker itself instead of the speaker reading the share (builder's call per measured trade-off).
- **Spotify stays on the normal Sonos path** (owner decision). Spotify visuals are approximate: either from the microphone of the device showing TV mode (needs mic permission + HTTPS) or a generic animation coloured from album art. Builder's call on default; offer both.

### 10.3 Showing TV mode on the TVs

- **Primary:** the app controls the owner's **two Samsung TVs directly** over the LAN (Samsung's network remote API; e.g. the `samsungtvws` library): wake from standby where supported, open the browser at the TV mode URL for a party. First use needs a one-time approval on the TV screen. Exposed as an app action ("Show on Living Room TV") and to Home Assistant.
- **Fallback:** a bookmarked TV mode URL opened manually in the TV's browser.
- **Unverified:** whether these TVs support opening the browser at a URL over the network (depends on model/year; pre-2016 models lack the API). **First task of this phase: a spike on one TV.** If it fails, fallback only (or owner buys a stick; out of scope here).

## 11. Home Assistant integration

- Exposes, per preset: a **start** action; per running party: current track, state, **stop** action; **show TV mode on TV X** action.
- Does not re-expose plain speaker control (HA's built-in Sonos integration does that).
- Builder's call on the mechanism (custom HA integration via HACS, MQTT discovery, or HA REST/webhooks). Sonora already ships an HA add-on; keep install via the add-on path working.
- HA is optional; all of this is also available in the app.
- **Deployment target** *(Claude's call, from owner's "runs on apps-vm")*: primary target is Docker on apps-vm; heavy components (Chromium, shairport-sync, Scream receiver, ffmpeg) may be separate containers. The HA add-on and Unraid paths must not break for basic control, but need not ship the heavy features.

## 12. Sound streaming from PCs and phones

All three paths go through the server to Sonos with the ~2–5 s delay; fine for music, not for video/games (owner accepted). All of them feed the visualizer exactly.

1. **AirPlay receivers.** The server advertises AirPlay speakers on the LAN — **one per running party and one per Sonos speaker/group** — e.g. "Sonora: Friday party", "Sonora: Kitchen" (e.g. shairport-sync instances, AirPlay 2 where possible). Native on iPhone/iPad/Mac, Linux (PipeWire), Windows with TuneBlade; Android AirPlay-sender apps work as-is. Works for **S1 speakers** too. Received audio is re-streamed to the target speaker(s); live sound joins/interrupts the party queue (builder's call on exact behaviour — e.g. live sound pauses the queue and resumes it when the sender stops).
2. **Windows network sound card.** Support the open-source **Scream** virtual audio driver: Windows sends PCM over the LAN; the server receives it and plays it to a chosen party/speaker. Builder's call on how a Scream source is mapped to a destination (e.g. selection in the app per source).
3. **In-app:**
   - **File drop:** any device (incl. Android) drops/picks music files into a party queue; the server stores them temporarily and serves them to the speaker. Builder's call on formats and retention (suggest: delete when the party ends).
   - **Send this tab's sound:** from desktop Chrome/Edge, capture a browser tab's audio and stream it to a party/speaker (needs HTTPS).

## 13. Phasing (proposed; owner may reorder)

1. **Foundation:** fork, Authentik login + guest account + server-side permissions, parties (start, queue with "added by", speaker ownership + move prompt, restore on end, multiple parties), presets, Home Assistant integration for presets/parties.
2. **YouTube on the server** (direct stream) + video window; remote browser view last in this phase.
3. **TV mode + visualizer** + Samsung TV control (starts with the TV spike).
4. **Sound streaming:** AirPlay + file drop first, then Scream, then tab sound.

Each phase should be usable on its own.

## 14. Decided by the owner (record)

- Fork Sonora; offer useful pieces upstream.
- Local only; new speakers set up once in the official app.
- Last action wins, no locking.
- Named users (Authentik) + one shared guest (play, volume, join parties only).
- Parties with presets, mixed queue, "added by", move-speaker prompt, restore on end, multiple at once; guests join only.
- Presets in app (2 taps) and HA (no prompt).
- YouTube on the server via **direct audio stream** (nothing stored), remote browser view for browsing; YouTube ToS risk accepted.
- Spotify stays native; approximate Spotify visuals.
- TV mode everywhere; app controls Samsung TVs directly, bookmark fallback.
- All three streaming paths (AirPlay, Scream, in-app file drop + tab sound).
- Runs on apps-vm.

## 15. Left to the builder

Mechanism choices marked "builder's call" above; storage for users/parties/presets (Sonora keeps data in `SONORA_DATA_DIR`); exact UI (follow Sonora's theme engine); the HA mechanism; audio formats and delay tuning; container packaging of Chromium/shairport-sync/ffmpeg (optional components may be separate containers if cleaner); test strategy.

## 16. Open / to verify

- Samsung TV models/years and whether URL-launch works (spike, §10.3).
- Network: speakers and apps-vm on the same segment, or mDNS/SSDP relayed (UniFi).
- Measured Sonos stream delay on the owner's speakers and how far tuning lowers it.
