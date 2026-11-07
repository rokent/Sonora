# Phase 3 — TV mode, visualizer, Samsung TV control — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One full-screen "TV mode" page per party (video / MilkDrop visualizer / cover + lyrics) that opens in the app, on a TV browser, or in a Home Assistant card; exact visuals for audio that passes through the server; the app drives the owner's two Samsung TVs directly to show it.

**Architecture:** TVs have no login, so a named user creates **display links** (long-lived tokens) that grant a read-only `display` principal. The `/tv` page (theme-independent, like `/party`) follows a party's current track. Exact visuals: `home/analysis.py` decodes the party's current server-relayed audio (YouTube now; Phase 4 live streams later) with **ffmpeg** at real time from the speaker's current position and publishes small 8-bit waveform frames (512 samples, ~30 fps, stamped with track time) on `/ws/visual`; the page feeds them to **Butterchurn** via `render({audioLevels})`. Spotify/library tracks get microphone input (HTTPS only) or a generic animation. `home/tvs.py` speaks Samsung's local WebSocket remote API directly with aiohttp (no new Python dep) and wakes TVs with Wake-on-LAN.

**Tech Stack:** FastAPI, aiohttp, ffmpeg (runtime image), React, `butterchurn@2.6.7` + `butterchurn-presets@2.4.7` (MIT, pinned exact).

**Spec:** `docs/superpowers/specs/2026-11-07-sonora-home-design.md` §10, §11, §8.1 (preset `tv` field).

## Global Constraints

- Display principal: kind `"display"`, read-only — allowed only `GET /api/state`, `/ws`, `/ws/visual`, `GET /api/parties`, `GET /api/parties/{id}/queue`, `GET /api/home/config`, `GET /api/art`, the SPA shell for `/tv`. Everything else denied (403).
- Display session cookie lasts 365 days; deleting a display link ends its sessions (same generation trick as the guest pass).
- Visual frames are data only (no audio stream to viewers): ≤ 512 bytes/frame, ≤ 30 frames/s per party.
- Exact visuals only for server-relayed audio; Spotify stays native (spec decision); library tracks use the approximate path (Ruling in ledger).
- Samsung control must not block requests: every TV call has a 5 s timeout and returns a clear error (`tv_unreachable`, `tv_needs_pairing`).
- Lyrics on TV mode use the existing LRCLIB code path (`lib/lyrics.js`) and only when the display link has `lyrics: true` (set by the user who created it — that user accepts the LRCLIB notice when ticking it).
- All new strings in all i18n catalogs; no emoji; gates one at a time.

## Review Focus

1. **Party ends while TV mode is open** → page shows "No party playing" and keeps listening; when a new party starts it follows it (if the link was for "any party"). Pinned in Task 2 (frontend logic unit: `lib`-style pure function `pickParty(parties, wanted)` with a `.check.mjs` test, see `app/tests/test_js_checks.py`).
2. **Speaker seeks / skips** → analysis restarts ffmpeg at the new position within 1 s; no frames from the old track after a skip. Pinned in Task 3.
3. **ffmpeg missing** → analysis disabled, `GET /api/home/config` reports `visuals: "approximate"`, nothing crashes. Pinned in Task 3.
4. **TV off / unreachable** → `POST /api/tvs/{id}/show` returns 504 `tv_unreachable` within ~5 s; Wake-on-LAN attempted first when a MAC is stored. Pinned in Task 5.
5. **Display link used after deletion** → 401, page shows "This screen link was removed". Pinned in Task 1.

---

### Task 1: Display links and the `display` principal

**Files:** Modify `app/backend/home/auth.py` (Principal kind adds `"display"`; `DisplayLinks` store `home_dir()/"displays.json"`: `{id, name, token_hash, party: str | null, mode: "video"|"visualizer"|"cover", lyrics: bool, created}`), `home/permissions.py` (display allowlist, `/tv` shell public like `/join`, `/auth/display` public), `home/auth_routes.py` (`GET /auth/display?token=` → sets 365-day display session, redirects `/tv?display=<id>`), `home/install.py` (serve shell at `/tv`); Create `home/display_routes.py`: `GET/POST /api/displays` (user; POST returns `{id, url}` where url = `{origin}/auth/display?token=…`, token shown once), `PUT/DELETE /api/displays/{id}` (user), `GET /api/displays/me` (display principal → its settings). Tests `app/tests/test_home_displays.py`, update expected sets in `test_home_permissions.py`.
- [ ] Tests: create → open link → cookie → `/api/displays/me`; display denied `POST /api/parties`; deleted link → 401 (Review Focus #5); token never returned by GET list. Commit `feat(home): display links for TV screens`.

### Task 2: TV mode page

**Files:** Create `app/frontend/src/home/TvApp.jsx`, `app/frontend/src/home/tvParty.js` (+ `tvParty.check.mjs`, picked up by `test_js_checks.py`), `tv.css`; Modify `main.jsx` (route `/tv` inside providers), i18n.
- `pickParty(parties, wanted) -> party | null` (wanted id or first running party) — Review Focus #1.
- Modes: **video** → reuse `home/VideoWindow.jsx` full-screen (from Phase 2) when the track is YouTube, else cover; **visualizer** → `home/Visualizer.jsx` (Task 4); **cover** → large art, title, artist, room names, and lyrics when enabled (reuse `lib/lyrics.js` / its hook as the themes do). Mode comes from the display link, or `?mode=` for signed-in users. Cursor hidden after 3 s idle; fullscreen button; no other chrome. Works at 1920×1080 and 360 px.
- [ ] `npm run build`, JS checks, backend gate → commit `feat(home): TV mode page`.

### Task 3: Server audio analysis + `/ws/visual`

**Files:** Create `app/backend/home/analysis.py`, `app/tests/test_home_analysis.py`; Modify `home/install.py` (websocket route + start/stop with the app lifespan via a startup hook that does not edit main.py — e.g. a router `on_startup`/lifespan-safe lazy start on first subscriber), `home/config_routes.py` (add `visuals: "exact"|"approximate"`).
- `class Analyzer`: per party with ≥1 subscriber; source = the party's current track if its URI is a Sonora media URI (`/api/home/media/<key>.m4a` → YouTube `audio_source`) — and a pluggable `register_source(party_id, async_iter_pcm)` hook for Phase 4 live streams (PCM s16le mono 22050 Hz). Runs `ffmpeg -nostdin -ss <pos> -re -i <url> -f u8 -ac 1 -ar 22050 pipe:1` (headers passed via `-headers`), slices 512-sample frames, publishes `{"t": track_seconds, "w": base64(512 bytes)}`. Restarts on track change or when |speaker position − analysis position| > 2 s (Review Focus #2); stops when no subscribers for 10 s. `shutil.which("ffmpeg")` None → disabled (Review Focus #3).
- `/ws/visual?party=<id>`: principal check as `/ws` (user/guest/display); sends frames.
- [ ] Tests with a fake process factory (no real ffmpeg): frames sliced and stamped; restart on seek/skip; no stale frames after skip; disabled without ffmpeg; subscriber refcount stop. Commit `feat(home): server-side visual analysis`.

### Task 4: Visualizer component

**Files:** Create `app/frontend/src/home/Visualizer.jsx`, `app/frontend/src/home/visualFrames.js` (+ `.check.mjs`: frame queue aligns frames to `speakerTime + offset`, drops stale); Modify `package.json` (butterchurn + butterchurn-presets exact versions), i18n.
- Inputs, chosen automatically: server frames (`/ws/visual`) when config says exact and the track is relayed; else microphone (`getUserMedia({audio:true})` only on `isSecureContext`, after a tap "Use microphone"); else generic (Butterchurn fed a slow synthetic waveform whose amplitude follows a 120 BPM pulse).
- Butterchurn: `createVisualizer(audioContext, canvas, {width, height})`, `render({audioLevels: {timeByteArray, timeByteArrayL, timeByteArrayR}})` each animation frame from the aligned frame; preset cycles every 30 s through `butterchurn-presets` `getPresets()`; "Next preset" on tap.
- Lazy-load the visualizer chunk (`React.lazy`) so the main bundle doesn't grow.
- [ ] build + JS checks → commit `feat(home): MilkDrop visualizer`.

### Task 5: Samsung TV control backend

**Files:** Create `app/backend/home/tvs.py`, `app/backend/home/tv_routes.py`, `app/tests/test_home_tvs.py`; Modify `home/ha.py` (implement `POST /api/ha/parties/{id}/tv/{tv_id}` → show), `home/routes.py` (preset start with `tv` set → show that party on that TV after start; failures reported in the response, not fatal), `home/install.py`.
- Store `home_dir()/"tvs.json"`: `{id, name, host, mac?, token?, mode_default}`.
- Protocol: `wss://{host}:8002/api/v2/channels/samsung.remote.control?name=<base64 "Sonora Home">&token=<token>` (TLS verify off — TVs use self-signed certs; fall back to `ws://{host}:8001/...` without token). First connect shows the allow prompt on the TV; the `ms.channel.connect` event carries `data.token` → store it. Open browser: send `{"method":"ms.channel.emit","params":{"event":"ed.apps.launch","to":"host","data":{"appId":"org.tizen.browser","action_type":"NATIVE_LAUNCH","metaTag":"<url>"}}}`. Wake: Wake-on-LAN magic packet to `255.255.255.255:9` (and the TV's /24 broadcast) when `mac` set, then retry connect for up to 15 s.
- The URL opened = a display link for that TV (auto-created per TV, `party` = chosen party, `mode` = chosen mode).
- Routes (user): `GET/POST /api/tvs`, `PUT/DELETE /api/tvs/{id}`, `POST /api/tvs/{id}/pair` (connect and store token), `POST /api/tvs/{id}/show {party_id, mode}`.
- [ ] Tests with a fake websocket server (aiohttp test server) emulating `ms.channel.connect` + token and capturing the launch message; WOL packet built correctly (6×FF + 16×MAC); unreachable → 504 within timeout (Review Focus #4); HA route works with service token. Commit `feat(home): Samsung TV control`.

### Task 6: TV management UI

**Files:** Create `app/frontend/src/home/TvSettings.jsx`, `ShowOnTv.jsx`, `DisplayLinks.jsx`; Modify `PartyApp.jsx` (links), `PartyView.jsx` ("Show on TV" for users: pick TV + mode), `PresetEditor.jsx` (optional TV + mode), `home/api.js`, i18n.
- TvSettings: add (name, IP, optional MAC), Pair (explains "accept on the TV"), test "Show now", delete. DisplayLinks: create (name, party any/specific, mode, lyrics checkbox with the LRCLIB notice text reused from the theme lyrics notice), copy link/QR (reuse qrcode), delete.
- [ ] build + backend gate → commit `feat(home): TV and screen settings`.

### Task 7: Container + docs

**Files:** Modify `Dockerfile` (runtime: `apt-get install --no-install-recommends ffmpeg`), Create `docs/tv-mode.md` (display links, Samsung pairing, TV browser bookmark fallback, Home Assistant iframe card YAML using a display link, mic needs HTTPS).
- [ ] `docker build .` once if possible (report if not). Commit `docs(home): TV mode`.

## Execution waves
- **Wave A (parallel):** Task 1, Task 3, Task 5.
- **Wave B (parallel):** Task 2 (needs 1; Phase 2 Task 4 for VideoWindow), Task 4 (needs 3's frame format), Task 7.
- **Wave C:** Task 6 (needs 5, 1).
