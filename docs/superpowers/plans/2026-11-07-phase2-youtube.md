# Phase 2 — YouTube in party queues — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anyone in a party can search or paste a YouTube video and add it to the party queue; the server streams the audio straight from YouTube to the speaker (nothing stored); viewers can watch the video in step with the speaker; named users get an optional shared remote browser (neko) for browsing YouTube together, guests view-only.

**Architecture:** `home/youtube.py` wraps **yt-dlp** (Python library) for search, metadata and resolving the audio-only stream URL. `home/media.py` issues opaque media keys and serves `GET /api/home/media/{key}.m4a` to speakers: on each request it resolves (cached) the YouTube audio URL and relays it with **Range pass-through**, so the speaker sees an ordinary progressive AAC file — known duration, native seek, no live-stream buffering. Queue items are plain `http://<lan-ip>:<port>/api/home/media/<key>.m4a` URIs with DIDL metadata, so they sit in the Sonos queue next to Spotify tracks. Because the speaker plays a file, its reported track position is the real video time; the video window is a muted YouTube embed seeked to that position (manual offset adjuster, default 0).

**Tech Stack:** yt-dlp (new Python runtime dependency, pinned + hashed in `app/requirements.lock`), a JavaScript runtime for yt-dlp's YouTube extractor (Node.js in the runtime image, `--js-runtimes node`), aiohttp relay, React; neko (`ghcr.io/m1k1o/neko/chromium`) as an optional separate container.

**Spec:** `docs/superpowers/specs/2026-11-07-sonora-home-design.md` §9 (owner decision 2026-11-07: direct stream, nothing stored).

## Global Constraints

- **Nothing stored on disk** for YouTube audio: relay only (in-memory metadata/URL cache is fine).
- Media route is reachable by speakers **without a session** (`/api/home/media/*` is already PUBLIC in `home/permissions.py`); keys are unguessable (`secrets.token_urlsafe(16)`), map only to a YouTube video id, expire after 24 h of no use.
- The speaker-facing URL uses the address speakers already use to reach Sonora: `controller.events._callback_ip` (fallback `backend.sonos.events.local_ip_for(coordinator_host)`) and `settings.port`.
- Guests may search YouTube and add YouTube items to a running party (same rights as other songs); the remote browser is view-only for guests.
- Only `bestaudio[ext=m4a]` (AAC) is relayed; if a video has no m4a audio, adding it fails with 422 `{error:"no_audio"}`. No ffmpeg.
- Party attribution (`record_add`) uses the media URI as the key, exactly like other items.
- Every new string in ALL i18n catalogs; no emoji. Gates one at a time: backend pytest (≈930 after Phase 1) and `npm run build`.

## Review Focus

1. **Expired googlevideo URL mid-party** (they expire after hours): a speaker requesting the media key after expiry must get a freshly resolved URL, not a 403 relay. Pinned in Task 2.
2. **Speaker seek (Range request)** must reach YouTube as a Range request and return 206 with Content-Range. Pinned in Task 2.
3. **yt-dlp failure** (video removed, age-restricted, network): add returns a clear 4xx/502 JSON, the queue is untouched. Pinned in Task 3.
4. **Pasted links in all common forms** (`youtu.be/ID`, `youtube.com/watch?v=ID&t=..`, `music.youtube.com/watch?v=ID`, `/shorts/ID`, bare 11-char id) parse to the same id; anything else → 400. Pinned in Task 1.
5. **Guest abuse:** guest search term length and rate are bounded (term ≤ 100 chars, max 10 results). Pinned in Task 3.

---

### Task 1: YouTube resolver (`home/youtube.py`) + dependency

**Files:** Create `app/backend/home/youtube.py`, `app/tests/test_home_youtube.py`; Modify `app/pyproject.toml` (add `yt-dlp>=<current>`), regenerate `app/requirements.lock` and `app/requirements-dev.lock` with the command at the top of each lock (`uv pip compile ... --generate-hashes`), install into `.venv`.

**Interfaces — Produces:**
- `def parse_video_id(text: str) -> str | None` (Review Focus #4).
- `@dataclass(frozen=True) class Video: id: str; title: str; channel: str; duration: int (s); thumbnail: str`
- `async def search(term: str, limit: int = 10) -> list[Video]` — yt-dlp `ytsearch{limit}:{term}` with `extract_flat`; runs in `asyncio.to_thread`.
- `async def info(video_id: str) -> Video` — cached 1 h in memory.
- `@dataclass class AudioSource: url: str; headers: dict[str,str]; content_type: str; size: int | None; expires: float`
- `async def audio_source(video_id: str) -> AudioSource` — picks `bestaudio[ext=m4a]`; raises `NoAudio`; cached until 10 min before the URL's `expire` query parameter (fallback 1 h).
- `class YouTubeError(Exception)`, `class NoAudio(YouTubeError)`.
- yt-dlp options: `quiet`, `no_warnings`, `skip_download`, `noplaylist`, `js_runtimes` → `{"node": {}}` when `shutil.which("node")`; all yt-dlp calls go through one `_ydl(opts)` helper so tests monkeypatch it.

- [ ] Step 1 tests: `test_parse_video_id_forms` (all forms in Review Focus #4 → `"dQw4w9WgXcQ"`; `"https://evil.example/watch?v=x"` → None); `test_search_maps_entries` and `test_audio_source_picks_m4a` with a monkeypatched `_ydl` returning canned info dicts; `test_no_m4a_raises_noaudio`; `test_audio_source_cache_respects_expire`.
- [ ] Step 2 run → FAIL. Step 3 implement + lock update. Step 4 PASS + full gate. Step 5 commit `feat(home): YouTube resolver`.

### Task 2: Media relay (`home/media.py`)

**Files:** Create `app/backend/home/media.py`, `app/tests/test_home_media.py`; Modify `app/backend/home/install.py` (include router).

**Interfaces:**
- Consumes: Task 1 `audio_source`, `info`.
- Produces: `class MediaKeys(path: Path)` persisted in `home_dir()/"media.json"` (`key → {video_id, used}`; survives restarts so queued items keep working): `issue(video_id: str) -> str`, `video_of(key: str) -> str | None` (touches `used`), purge > 24 h unused on load/issue. `def media_url(key: str, host_for: str) -> str` → `http://{ip}:{settings.port}/api/home/media/{key}.m4a`. `def didl_for(video: Video, uri: str) -> str` — DIDL-Lite item: `dc:title`, `dc:creator` (channel), `upnp:albumArtURI` (thumbnail), `upnp:class object.item.audioItem.musicTrack`, `<res protocolInfo="http-get:*:audio/mp4:*" duration="H:MM:SS">uri</res>` (escape all text).
- Route `GET /api/home/media/{key}.m4a` (and HEAD): unknown key → 404; resolve `audio_source`; forward `Range` header; on upstream 403/410 drop the cache entry, resolve once more and retry (Review Focus #1); stream chunks; pass `Content-Length`, `Content-Range`, `Accept-Ranges`, status (200/206); `Content-Type: audio/mp4`. Use `backend.main.state["outward"]` session (fetchguard) like `/api/stream/audio`.
- [ ] Tests: unknown key 404; range forwarded and 206 + Content-Range returned (Review Focus #2); expired upstream 403 → re-resolve → 200; keys persist across `MediaKeys` reload; DIDL escapes `&<>`.
- [ ] Fail → implement → pass (+ full gate) → commit `feat(home): YouTube media relay for speakers`.

### Task 3: Party + search routes

**Files:** Modify `app/backend/home/routes.py`, `app/backend/home/permissions.py`, `app/tests/test_home_permissions.py` (expected guest sets), Create `app/tests/test_home_youtube_routes.py`.

**Interfaces:**
- `GET /api/youtube/search?q=` (guest + user) → `{results: [{id,title,channel,duration,thumbnail}]}`; `q` stripped, 1–100 chars else 400; max 10 results (Review Focus #5).
- `POST /api/parties/{id}/youtube` (guest + user) `{video: "<url or id>", next?: bool}` → parse id (400 `bad_link`), `info` (502 `youtube_failed` on YouTubeError), `audio_source` probe (422 `no_audio`), issue key, enqueue on the party coordinator with `didl_for`, `record_add(party, uri, principal)` → `{ok, item: {title, channel, duration, thumbnail}}`. Queue untouched on any failure (Review Focus #3).
- `GET /api/parties/{id}/queue` items for media URIs get `source: "youtube"` and `video_id` (frontend uses them for the badge and the video window).
- [ ] Tests for each status path, guest allowed, attribution recorded, queue untouched on failure. Commit `feat(home): add YouTube to party queues`.

### Task 4: Frontend — YouTube in Add Songs, badge, video window

**Files:** Create `app/frontend/src/home/YouTubeSearch.jsx`, `app/frontend/src/home/VideoWindow.jsx`; Modify `AddSongs.jsx`, `PartyView.jsx`, `home/api.js` (`youtubeSearch(q)`, `addYouTube(id, body)`), `home.css`, all i18n catalogs.

- **AddSongs:** a "YouTube" tab: search box (debounced 400 ms) listing results (thumbnail, title, channel, m:ss), and a "Paste a link" field; tap/submit → `addYouTube`; error kinds `bad_link`/`no_audio`/`youtube_failed` shown in words.
- **Queue rows:** YouTube badge for `source === "youtube"`.
- **VideoWindow** (in PartyView when the current track is a YouTube item; collapsible): `<iframe src="https://www.youtube-nocookie.com/embed/{video_id}?enablejsapi=1&mute=1&controls=0&playsinline=1">`; control it only via `postMessage` commands (`playVideo`, `pauseVideo`, `seekTo`, `mute`) — **do not load the YouTube iframe API script** (page CSP is `script-src 'self'`). Sync: target = speaker position (from the store's transport state + elapsed, see `lib/useElapsed.js`) + offset; re-seek when drift > 1.5 s, at most every 5 s; follow play/pause state. Offset slider −5…+5 s, step 0.25, stored in `localStorage['sonora.videoOffset']` (try/catch).
- [ ] `npm run build` + backend gate (i18n parity, theme reference if CSS vars change) → commit `feat(home): YouTube search, badge and video window`.

### Task 5: Shared remote browser (neko) + container runtime

**Files:** Modify `Dockerfile` (runtime stage: install Node.js runtime for yt-dlp — e.g. copy the `node` binary from the existing `node:26-slim` build stage, or `apt-get install nodejs`; builder's call, keep the image small), `docker-compose.yml` (commented optional `neko` service), Create `docs/remote-browser.md`, `app/backend/home/config_routes.py` (+ install), `app/frontend/src/home/RemoteBrowser.jsx`, PartyView entry, i18n.

- Env: `SONORA_NEKO_URL` (full control, users) and `SONORA_NEKO_VIEW_URL` (watch-only, guests). `GET /api/home/config` (guest + user) → `{remote_browser: {url} | null}` choosing the URL by principal kind (guest → view URL or null).
- `RemoteBrowser.jsx`: button "Shared browser" in PartyView opening a full-screen panel with an `<iframe src={url} allow="autoplay; fullscreen">`.
- `docs/remote-browser.md`: compose snippet for `ghcr.io/m1k1o/neko/chromium` with host networking or UDP port range, `NEKO_MEMBER_MULTIUSER_USER_PASSWORD` / `ADMIN_PASSWORD`, how to make a view-only member, and a Chromium policy file restricting URLs to `youtube.com`, `*.youtube.com`, `*.googlevideo.com`, `accounts.google.com` (allowlist + `URLBlocklist: ["*"]`).
- [ ] Tests: config route returns per-principal URL / null; Docker build is not run in CI here — verify `docker build .` locally once if Docker works (report if it can't). Commit `feat(home): shared remote browser and yt-dlp runtime`.

## Execution waves
- **Wave A:** Task 1.
- **Wave B (parallel):** Task 2, Task 5 (backend/docs parts don't touch Task 2 files).
- **Wave C:** Task 3. **Wave D:** Task 4 (needs Phase 1 Task 8 merged).
