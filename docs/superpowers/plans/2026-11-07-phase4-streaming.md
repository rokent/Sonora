# Phase 4 — Sound from PCs and phones — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any PC or phone can send its sound to any party or speaker: AirPlay receivers (one per running party and per Sonos room), a Windows network sound card (Scream), in-app file drop, and "send this tab's sound" from Chrome/Edge.

**Architecture:** Every live source produces PCM (s16le, 44.1 kHz, stereo) into a `LiveSession` in `home/live.py`. A session encodes once with ffmpeg to MP3 320 kbit/s and fans the bytes out to every HTTP listener of `GET /api/home/live/{key}.mp3`; the target party's coordinator is told to play `x-rincon-mp3radio://<ip>:<port>/api/home/live/<key>.mp3` (the scheme Sonos uses for internet radio). Starting live sound on a party remembers the party queue's track number and position; when the sender goes silent for 8 s (or disconnects) the session ends and the party queue resumes there. Sessions also feed Phase 3's analyzer (`analysis.register_source`). File drop is not live: uploaded files are served with Range support at `/api/home/files/{key}.{ext}` and enqueued like YouTube items, deleted when the party ends.

**Tech Stack:** ffmpeg (already in the image from Phase 3), shairport-sync (AirPlay 1/RAOP receiver, `pipe`/`stdout` backend), asyncio UDP for Scream, browser `getDisplayMedia` + `MediaRecorder` (Opus/WebM) over WebSocket.

**Spec:** `docs/superpowers/specs/2026-11-07-sonora-home-design.md` §12.

## Global Constraints

- Delay of ~2–5 s on live sources is accepted (spec §5); no attempt to sync with video.
- Live URLs and file URLs are reachable by speakers without a session (`/api/home/live/*`, `/api/home/files/*` added to PUBLIC); keys unguessable; a live key stops working when its session ends; a file key when its party ends.
- Who may start live sound on what: users → any party or room; guests → only a running party (they're in), never a bare room; AirPlay/Scream senders are not principals — their destination is fixed by which AirPlay name / Scream mapping they use, and the Scream mapping is set by users.
- AirPlay names: `Sonora: <party name>` and `Sonora: <room name>`; S1 rooms included.
- Uploads: max 300 MB per file, types mp3, m4a/aac, flac, wav, ogg; stored under `home_dir()/"uploads"/<party_id>/`; deleted when the party ends and on startup for parties that no longer exist.
- Tab sound needs a secure context: hidden on the plain-HTTP address with a hint (spec §5).
- All new strings in all i18n catalogs; no emoji; gates one at a time.

## Review Focus

1. **Sender stops abruptly** (phone locked, laptop lid closed): session ends after 8 s silence/no data and the party queue resumes at the saved track and position. Pinned in Task 1.
2. **Two senders target the same party** → the newer one wins (last action wins); the older session ends cleanly; only one resume happens, after the last one ends. Pinned in Task 1.
3. **Party ends while live** → session ends, no resume attempted on released speakers. Pinned in Task 1.
4. **Upload of a non-audio or oversize file** → 415 / 413, nothing stored. Pinned in Task 4.
5. **AirPlay process crash** → supervisor restarts it with backoff; party/room list changes add/remove receivers within 10 s. Pinned in Task 2.

---

### Task 1: Live session hub (`home/live.py`)

**Files:** Create `app/backend/home/live.py`, `app/backend/home/live_routes.py`, `app/tests/test_home_live.py`; Modify `home/permissions.py` (PUBLIC `/api/home/live/*`), `home/install.py`.
**Interfaces — Produces:**
- `class LiveHub`: `async def start(self, *, target: Target, source_name: str, by: str) -> LiveSession` (Target = `("party", id)` or `("zone", uuid)`); ends any existing session on that target (Review Focus #2); for a party: records `{track_no, position}` from the coordinator transport state, then `commands.play_now(coordinator, "x-rincon-mp3radio://…/api/home/live/<key>.mp3", didl(title=source_name))`.
- `class LiveSession`: `async def feed(self, pcm: bytes) -> None`, `async def end(self, reason: str) -> None`, `key: str`; internal ffmpeg encoder process (`-f s16le -ar 44100 -ac 2 -i pipe:0 -f mp3 -b:a 320k pipe:1`); listeners get the byte stream from "now" (no backlog beyond 64 KB); silence watchdog 8 s → `end("idle")`; on end for a party target → `seek_track(track_no)` + `seek_time(position)` + `play` (Review Focus #1) unless the party has ended (Review Focus #3, hook into `PartyEngine` end → `hub.end_for_party(id)`).
- Calls `analysis.register_source(party_id, pcm_iter)` when Phase 3's analyzer exists (import guarded).
- Route `GET /api/home/live/{key}.mp3` → `audio/mpeg` streaming, ICY-free; 404 unknown/ended.
- [ ] Tests with a fake encoder (identity) and fake commands: start→play_now URI shape; feed → listener receives; idle 8 s (use an injectable clock) → resume calls in order; newer session replaces older, single resume; party end → no resume. Commit `feat(home): live sound hub`.

### Task 2: AirPlay receivers (spike first)

**Files:** Create `app/backend/home/airplay.py`, `app/tests/test_home_airplay.py`, `docs/airplay.md`; Modify `Dockerfile`, `home/install.py`.
- [ ] **Step 0 spike (report findings in the task report before building):** in a throwaway container (`debian:trixie-slim` + `apt-get install shairport-sync`), confirm (a) the CLI flags to run several classic-AirPlay instances side by side with distinct names and ports writing raw PCM to stdout (`-a NAME -p PORT -o stdout` or the `pipe` backend), (b) which mDNS backend works inside a host-network container without fighting the host's avahi (options: `-m avahi` with the host's avahi socket mounted, `-m tinysvcmdns`, or advertising `_raop._tcp` ourselves with python-zeroconf and running shairport-sync with mDNS off). Pick the simplest working option; record it as a ledger ruling.
- `class AirPlaySupervisor`: desired receivers = running parties ∪ rooms (one per zone group coordinator… rule: one per *room* (zone) and one per party); each receiver = one shairport-sync process (port range 5100–5199) whose stdout PCM is fed to `LiveHub.start(target=…)` on first audio and `session.feed()` after; reconciles every 5 s and on party changes (Review Focus #5); restarts crashed processes with exponential backoff (1 s → 30 s).
- Dockerfile: install shairport-sync (+ whatever the spike chose).
- [ ] Tests with a fake process factory: reconcile adds/removes; crash → restart with backoff; first audio starts a session on the right target. Commit `feat(home): AirPlay receivers`.

### Task 3: Scream receiver (Windows network sound card)

**Files:** Create `app/backend/home/scream.py`, `app/backend/home/scream_routes.py`, `app/tests/test_home_scream.py`, `docs/windows-sound.md`; Modify `home/install.py`.
- UDP listener on multicast `239.255.77.77:4010` (also unicast on the same port). Packet: 5-byte header (`rate` byte: bit7 → 44.1 kHz base else 48 kHz, low 7 bits multiplier; `bits`; `channels`; 2-byte channel map) + PCM. Convert to s16le 44.1 kHz stereo (resample 48k→44.1k via ffmpeg in the session, i.e. allow `LiveHub.start(..., input_format=...)`).
- Sources keyed by sender IP; `GET /api/scream/sources` (user) → `[{ip, last_seen, format, target}]`; `PUT /api/scream/sources/{ip}` `{target: {"party"|"zone", id} | null}` persisted in `home_dir()/"scream.json"`. Audio from an unmapped source is ignored.
- `docs/windows-sound.md`: install Scream driver, set unicast/multicast, pick it as Windows output, map it in Sonora.
- [ ] Tests: header parsing (both rate bases), mapping persisted, unmapped ignored, mapped → session feed. Commit `feat(home): Windows network sound card`.

### Task 4: File drop

**Files:** Create `app/backend/home/files.py`, `app/tests/test_home_files.py`; Modify `home/routes.py` (`POST /api/parties/{id}/files` multipart, guest + user), `home/permissions.py` (PUBLIC `/api/home/files/*`; guest write added + expected sets), `home/parties.py` integration (delete uploads on end), `home/install.py`.
- Validate by extension + magic bytes (ID3/MPEG frame sync, `ftyp`, `fLaC`, `RIFF....WAVE`, `OggS`); stream to disk in chunks, abort over 300 MB (413) and delete partial; 415 otherwise (Review Focus #4). Read tags for title/artist when cheap (ID3v2 TIT2/TPE1 only; else file name). Enqueue with DIDL (`protocolInfo` per type), `record_add`.
- `GET /api/home/files/{key}.{ext}` with Range support (206).
- [ ] Tests: each accepted type, 413, 415, range 206, deletion on party end, startup cleanup. Commit `feat(home): drop music files into a party`.

### Task 5: Tab sound + frontend for all streaming

**Files:** Create `app/backend/home/tabsound.py` (`/ws/tabsound?target=party:<id>|zone:<uuid>`: receives WebM/Opus chunks, pipes into an ffmpeg decoder to PCM, feeds a LiveSession; principal rules per Global Constraints), tests; Create `app/frontend/src/home/SendSound.jsx`, `FileDrop.jsx`; Modify `PartyView.jsx` (buttons "Send this tab's sound", "Add files"), `AddSongs.jsx` (Files tab), `PartyApp.jsx` (users: "Windows sound" mapping screen from Task 3 routes; "AirPlay names" help listing current receiver names via `GET /api/airplay/receivers`), `home/api.js`, i18n.
- SendSound: `navigator.mediaDevices.getDisplayMedia({audio: true, video: true})` (Chrome requires video; stop the video track immediately), `MediaRecorder(stream, {mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 160000})`, `timeslice` 250 ms → WebSocket binary; shows "Sending…" with Stop; hidden with a hint when `!window.isSecureContext`.
- FileDrop: drag-and-drop + file picker (multiple), per-file progress (XHR upload progress), errors 413/415 in words.
- [ ] Backend tests for tabsound with fake decoder; `npm run build`; backend gate. Commit `feat(home): send tab sound and drop files from the app`.

## Execution waves
- **Wave A:** Task 1.
- **Wave B (parallel):** Task 2, Task 3, Task 4.
- **Wave C:** Task 5.
