# Phase 1 — Accounts, Parties, Presets, Home Assistant — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Named-user login (Authentik OIDC) + one guest account with server-enforced limits, parties (shared queue with "added by", speaker ownership, restore on end), presets, and a Home Assistant API — on top of the Sonora fork.

**Architecture:** All new backend code lives in a new package `app/backend/home/` (keeps upstream merges clean); `main.py` gets only a few lines (router include, middleware registration, lifespan hooks, websocket principal check). Parties reuse Sonora's scene machinery (`backend/sonos/scenes.py`: `capture`, `apply`, `revert`) for grouping/snapshot/restore. All new UI lives in `app/frontend/src/home/` as a **theme-independent page** (`/party`, `/join`, `/tv` later), so none of the 13 themes needs editing; `App.jsx` gets one floating "Parties" button for named users and routes guests to the party page.

**Tech Stack:** Python 3.14, FastAPI, aiohttp (OIDC HTTP calls — no new Python deps), pytest + pytest-asyncio; React 19 + Vite 8; one new npm dep `qrcode` (pinned exact version) for the guest QR.

**Spec:** `docs/superpowers/specs/2026-11-07-sonora-home-design.md` (§6, §7, §8, §8.1, §8.2, §11)

## Global Constraints

- Python ≥3.11 (image uses 3.14). No new Python runtime dependencies; HMAC/secrets/aiohttp only.
- License AGPL-3.0-only; new files carry no other license.
- **Auth is off when `SONORA_OIDC_ISSUER` is unset** → upstream Sonora behaviour unchanged (every request is a named user called "Local"). All upstream tests must keep passing in this mode.
- Data under `settings.data_dir / "home"` (JSON files, atomic writes via the same temp-file + `os.replace` pattern as `scenes._write_json`).
- Permissions are enforced on the server; the UI only hides.
- Guest rights: join a running party; add songs (enqueue only) to its queue; play/pause/next/previous; volume; remove **only their own** queue items. Nothing else.
- A party/preset uses speakers of **one household only** (S1 and S2 are different households).
- Last action wins: an outside change to a party speaker removes it from the party; the party never takes it back.
- From HA, preset start takes speakers from other parties without asking.
- Every new user-visible string goes into `app/frontend/src/i18n/en-US.js` **and every other catalog** (copy the English text into the others; `tests/test_i18n_parity.py` enforces key parity). No emoji in copy.
- Gates (run one at a time): `cd app && ../.venv/bin/python -m pytest -q -p no:cacheprovider` (baseline 846 passed, 1 skipped) and `cd app/frontend && npm run build`.

## Review Focus

1. **Guest reaching an upstream route** (e.g. `POST /api/zones/{uuid}/group`, `/api/scenes/...`, settings) → must get 403. Pinned by Task 2's default-deny test over every registered route.
2. **WebSocket without a session when auth is on** → closed with 1008; with a guest session → accepted. Pinned in Task 2.
3. **A speaker leaves its party group via the official Sonos app** → party drops it and broadcasts a notice; party with zero speakers becomes `paused`, queue attribution kept. Pinned in Task 3.
4. **Server restart mid-party** → parties, snapshots and attribution reload from disk; ending after restart still restores. Pinned in Task 3.
5. **Preset referencing a speaker that is offline or in another household** → start skips missing speakers and reports them; mixed-household preset is rejected at save (400). Pinned in Task 4.

---

### Task 1: Principals, sessions, OIDC login, guest join

**Files:**
- Create: `app/backend/home/__init__.py`, `app/backend/home/store.py`, `app/backend/home/auth.py`, `app/backend/home/auth_routes.py`
- Test: `app/tests/test_home_auth.py`

**Interfaces:**
- Produces (`home/store.py`): `home_dir() -> Path` (creates `settings.data_dir/"home"`), `read_json(path: Path, default)`, `write_json(path: Path, data) -> None` (atomic).
- Produces (`home/auth.py`):
  - `@dataclass(frozen=True) class Principal: kind: Literal["user","guest","service"]; id: str; name: str` with `is_user` (kind in user/service) and `is_guest` properties.
  - `LOCAL = Principal("user", "local", "Local")`
  - `def auth_enabled() -> bool` — true iff env `SONORA_OIDC_ISSUER` non-empty.
  - `def principal_of(scope_headers_cookie: str | None, authorization: str | None) -> Principal | None` — reads cookie `sonora_session` (signed) or `Authorization: Bearer <ha token>` (Task 6 registers token checker via `register_bearer(fn)`); returns `LOCAL` when auth disabled.
  - `def request_principal(request) -> Principal` — set by middleware into `request.state.principal`; raises HTTPException(401) if missing.
  - `def sign_session(p: Principal, max_age: int) -> str` / `def read_session(value: str) -> Principal | None` — `base64url(json).hmac_sha256` with key from `home_dir()/"secret.key"` (32 random bytes, created on first use, mode 0600); payload has `exp`.
  - `class GuestPass: token() -> str; rotate() -> str; check(t: str) -> bool` persisted in `home_dir()/"guest.json"` (token = `secrets.token_urlsafe(24)`), compare with `hmac.compare_digest`.
  - `def register_bearer(check: Callable[[str], Principal | None]) -> None`
- Produces (`home/auth_routes.py`): `router = APIRouter()` with:
  - `GET /auth/login?next=` → redirect to OIDC authorize (code + PKCE S256, `state` and verifier in a short-lived signed cookie `sonora_oidc`, scope `openid profile email groups`). Redirect URI = `{scheme}://{host}/auth/callback` from the request (works for both proxy HTTPS and direct LAN address; honour `X-Forwarded-Proto`/`X-Forwarded-Host` only when env `SONORA_TRUST_PROXY=1`).
  - `GET /auth/callback?code&state` → token exchange (aiohttp POST to discovered `token_endpoint`, client id/secret from `SONORA_OIDC_CLIENT_ID`/`SONORA_OIDC_CLIENT_SECRET`), fetch `userinfo_endpoint`; if `SONORA_OIDC_GROUP` set, require it in `groups`; set `sonora_session` (user, id=`sub`, name=`preferred_username` or `name`; 30 days; HttpOnly; SameSite=Lax; Secure when scheme https) and redirect to `next` (only same-site relative paths).
  - `POST /auth/guest` body `{token, name}` → if token valid, set guest session (id=`guest:<random>`, name trimmed 1–30 chars, 12 h) → `{ok, principal}`; else 403.
  - `POST /auth/logout` → clears cookie.
  - `GET /api/me` → `{auth: bool, principal: {kind,id,name}}`.
  - `GET /api/guest-pass` (user only) → `{token, url}`; `POST /api/guest-pass/rotate` (user only) → new token.
  - OIDC discovery (`{issuer}/.well-known/openid-configuration`) cached in memory for 1 h.

- [ ] **Step 1: Write failing tests** in `app/tests/test_home_auth.py`:
  - `test_session_round_trip` — `read_session(sign_session(Principal("user","u1","Ana"), 60))` equals the principal; tampered value → `None`; expired (max_age=-1) → `None`.
  - `test_auth_disabled_is_local` — with `SONORA_OIDC_ISSUER` unset, `principal_of(None, None) == LOCAL`.
  - `test_guest_pass_rotate_invalidates` — old token fails `check` after `rotate()`.
  - `test_guest_join_sets_cookie` — FastAPI `TestClient` on an app including `router` with `SONORA_OIDC_ISSUER=https://idp.example` (monkeypatched) and `SONORA_DATA_DIR=tmp_path`: POST `/auth/guest` with valid token + name "Ana" → 200, cookie `sonora_session` set; `/api/me` → kind `guest`, name `Ana`. Invalid token → 403. Name "" → 400.
  - `test_callback_rejects_wrong_group` — monkeypatch the OIDC HTTP helper (`auth_routes._oidc_json`/`_exchange`) to return userinfo without the required group → 403.
  - `test_next_must_be_relative` — `next=https://evil.example` redirects to `/`.
- [ ] **Step 2:** Run `cd app && ../.venv/bin/python -m pytest tests/test_home_auth.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement the files above.
- [ ] **Step 4:** Run the test file → PASS; run full backend gate → 846+ passed.
- [ ] **Step 5:** Commit `feat(home): principals, sessions, OIDC login and guest join`.

### Task 2: Enforcement middleware (default-deny for guests) and wiring into main.py

**Files:**
- Create: `app/backend/home/permissions.py`
- Modify: `app/backend/main.py` (register middleware after `origin.guard`, include `auth_routes.router` and later routers via `home.install(app)`, websocket principal check at the top of `websocket_endpoint`)
- Create: `app/backend/home/install.py` — `def install(app: FastAPI) -> None` registers middleware + routers (Tasks 5/6 add their routers here).
- Test: `app/tests/test_home_permissions.py`

**Interfaces:**
- Consumes: Task 1 `principal_of`, `Principal`, `auth_enabled`.
- Produces (`home/permissions.py`):
  - `PUBLIC: list[tuple[str, re.Pattern]]` — no principal needed: `GET /`, static assets (`/assets/`, `/favicon`, theme files GET), `/auth/*`, `GET /api/me`, `GET /api/stream/audio/*`, `GET /api/art`, `GET /api/home/media/*` (Phase 2+ media served to speakers), healthcheck path used by `backend.healthcheck`.
  - `GUEST: list[tuple[str, re.Pattern]]` — guest allowlist: `GET /api/state`, `/ws`, `GET /api/parties*`, `POST /api/parties/{id}/join`, `POST /api/parties/{id}/queue`, `DELETE /api/parties/{id}/queue/{index}`, `POST /api/parties/{id}/transport/{action}` where action ∈ play,pause,next,previous, `POST /api/parties/{id}/volume`, GET browse/search reads: `/api/favorites`, `/api/services`, `/api/services/{sid}/(browse|search|letters|item)`, `/api/library` GET, `/api/browse*`, `/api/queue/{uuid}` GET, `/api/i18n*`/locale GETs used by the page, `GET /api/themes*`.
  - `def allowed(principal: Principal | None, method: str, path: str) -> bool` — PUBLIC → True; None → False; user/service → True; guest → matches GUEST.
  - `async def guard(request, call_next)` — sets `request.state.principal`; unauthenticated browser GET of an HTML page → 302 `/auth/login?next=<path>` except `/join` (guest page) which is served; API → 401 JSON `{error:"login"}`; guest denied → 403 JSON `{error:"forbidden"}`.
- [ ] **Step 1: Write failing tests:**
  - `test_every_upstream_write_is_denied_to_guests` — iterate `backend.main.app.routes`; for every route with a non-GET method not in `GUEST`, assert `allowed(guest, method, sample_path) is False` (substitute path params with `x`). This pins Review Focus #1.
  - `test_guest_allowlist` — guest allowed `POST /api/parties/p1/queue`, denied `POST /api/zones/x/group`, `POST /api/scenes/x/apply`, `POST /api/presets`.
  - `test_auth_off_everything_allowed` — with issuer unset, `TestClient(backend.main.app)` GET `/api/me` → `auth:false`.
  - `test_ws_requires_session` — issuer set: websocket connect to `/ws` without cookie → closes 1008 (Review Focus #2); with a guest cookie → not refused by the principal check (the snapshot code may fail without a controller — assert only that close code ≠ 1008 or accept happened).
  - `test_api_without_session_is_401_and_page_redirects`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; edit `main.py` minimally: `from .home.install import install as install_home` and `install_home(app)` right after `app.middleware("http")(origin.guard)`; in `websocket_endpoint` after the origin check: `if not home_permissions.ws_allowed(websocket): await websocket.close(code=1008); return`.
- [ ] **Step 4:** Test file PASS; full gate PASS.
- [ ] **Step 5:** Commit `feat(home): server-side permissions, guests default-deny`.

### Task 3: Party engine (ownership, attribution, outside changes, restore, persistence)

**Files:**
- Create: `app/backend/home/parties.py`
- Test: `app/tests/test_home_parties.py`

**Interfaces:**
- Consumes: `backend.sonos.scenes.capture(controller, groups) -> dict`, `scenes.apply(controller, commands, scene, metadata_fn) -> dict` (returns report with `"before"`), `scenes.revert(controller, commands, before) -> dict`; `controller.zones` / topology for which zones are in which group (read `scenes.capture`/`_playing_now` for how state is read); `Commands.add_to_queue(zone, uri, metadata, next_=False, position=0) -> dict` (returns `FirstTrackNumberEnqueued`, `NumTracksAdded`), `Commands.remove_from_queue(zone, index)`.
- Produces:
  - `@dataclass class Party: id: str; name: str; owner: str (principal id); owner_name: str; household: str; speakers: list[str] (zone uuids, first = coordinator); volumes: dict[str,int]; state: Literal["playing","paused"]; preset_id: str | None; created: float`
  - `class PartyEngine:`
    - `__init__(self, path: Path, controller, commands, metadata_fn, notify: Callable[[dict], Awaitable[None]])`
    - `def list(self) -> list[dict]` (party as dict + `queue_owners` summary)
    - `def owner_of(self, zone: str) -> str | None` (party id)
    - `async def start(self, *, name: str, speakers: list[str], by: Principal, volumes: dict[str,int] | None = None, music: dict | None = None, preset_id: str | None = None, take: bool = False) -> dict` — raises `SpeakerTaken(zone, party_id, party_name)` if any speaker is owned and `take` is False; raises `MixedHousehold` if speakers span households. Builds a one-group scene `{"name": name, "groups": [{"rooms": [{"uuid", "volume"}...], "play": music or {"kind": "silence"}... }]}` matching `scenes.normalize` shape (read `normalize` for the exact keys) and calls `scenes.apply`; pushes the report's `before` per speaker onto that speaker's **snapshot stack** (`stack[zone] = [ {party_id, before_for_zone}, ... ]`). Taken speakers are removed from their old party first (old party → `paused` if emptied).
    - `async def end(self, party_id: str) -> dict` — for each speaker: if the top-of-stack entry below this party belongs to a still-running party → re-add speaker to that party's group (join its coordinator); else `scenes.revert` with that speaker's pre-party snapshot. Deletes party + attribution.
    - `async def add_speaker(self, party_id, zone, by, take=False)` / `async def remove_speaker(self, party_id, zone)`.
    - `def record_add(self, party_id: str, uri: str, by: Principal) -> None` and `def added_by(self, party_id: str, uri: str) -> list[str]` — attribution keyed by track URI, FIFO list of `{id,name}` per URI (a Sonos queue has no stable item ids).
    - `def can_remove(self, party_id, uri, principal) -> bool` — users always; guest only if the oldest attribution for that URI is theirs.
    - `async def on_state(self, message: dict) -> None` — registered via `controller.on_state`; after each topology change, for every party speaker no longer in the party coordinator's group → drop it, `notify({"type":"party_notice","party":id,"zone":zone,"event":"left"})`; zero speakers → `state="paused"`. Never regroups.
    - Persistence: `home_dir()/"parties.json"` holds parties, stacks, attribution; loaded in `__init__`; written after every mutation.
  - `class SpeakerTaken(Exception)`, `class MixedHousehold(Exception)`.
- [ ] **Step 1: Write failing tests** with a fake controller/commands (pattern: see `tests/test_scene*.py` if present, else build a minimal fake exposing what `scenes.capture/apply/revert` read; monkeypatching `scenes.apply`/`scenes.revert` to record calls is acceptable):
  - `test_start_groups_and_snapshots` — start on [A,B] → `scenes.apply` called with one group of A,B; `owner_of("A") == id`.
  - `test_taken_speaker_needs_confirmation` — party1 on [A]; start party2 on [A] → `SpeakerTaken`; with `take=True` → A in party2, party1 `paused` with empty speakers.
  - `test_end_returns_moved_speaker_to_running_party` — party1 [A,B], party2 takes B, end party2 → B rejoins party1's coordinator (join called), not reverted.
  - `test_end_restores_pre_party_state` — end with no underlying party → `scenes.revert` called with the speaker's original snapshot.
  - `test_outside_change_drops_speaker` — simulate topology where B left A's group → B removed, notice emitted; A alone stays. (Review Focus #3)
  - `test_guest_can_remove_only_own` — attribution then `can_remove`.
  - `test_reload_after_restart` — new `PartyEngine` on same path sees same parties/stacks/attribution; `end` still reverts. (Review Focus #4)
  - `test_mixed_household_rejected`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** PASS + full gate. **Step 5:** Commit `feat(home): party engine`.

### Task 4: Presets store

**Files:**
- Create: `app/backend/home/presets.py`
- Test: `app/tests/test_home_presets.py`

**Interfaces:**
- Produces: `class Presets(path: Path)`: `list() -> list[dict]`, `get(id)`, `add(body: dict, household_of: Callable[[str], str | None]) -> dict`, `update(id, body, household_of)`, `remove(id)`. Preset dict: `{id, name (1–60), speakers: [uuid…] (≥1), volumes: {uuid: 0–100}, music: null | {"kind":"item","uri","metadata","title"} (same shape scenes' `play` uses), tv: null | {"tv_id": str, "show": "video"|"visualizer"|"cover"} (stored only; used in Phase 3), created, updated}`. `ValueError` on: empty name, no speakers, volume out of range, speakers in >1 household (`household_of` returns different ids), unknown speaker (household_of None).
- [ ] **Step 1: Tests:** `test_add_and_list`, `test_mixed_household_rejected` (Review Focus #5), `test_unknown_speaker_rejected`, `test_update_and_remove`, `test_persisted`.
- [ ] **Step 2–4:** fail → implement → pass (+ full gate). **Step 5:** Commit `feat(home): party presets store`.

### Task 5: Party and preset HTTP API + live updates

**Files:**
- Create: `app/backend/home/routes.py`; Modify: `app/backend/home/install.py` (include router; create `PartyEngine`/`Presets` lazily from `backend.main.state["controller"]`/`["commands"]` on first use, register `engine.on_state` with `controller.on_state` once), `app/backend/main.py` only if a hook is unavoidable (prefer none).
- Modify: `app/backend/main.py` `set_source` route **or** a middleware in `home/permissions.py` to record attribution: when `POST /api/zones/{uuid}/source` with `enqueue=true` targets a party speaker, call `engine.record_add(party, uri, principal)` after success. Guests: this route is **not** in GUEST; guests add via `POST /api/parties/{id}/queue`.
- Test: `app/tests/test_home_routes.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces (all JSON; broadcast `{"type":"parties"}` through `backend.main._fan_out` after every mutation; party notices broadcast as-is):
  - `GET /api/parties` → `{parties: [...], presets: [...] (users only; guests get [])}`
  - `POST /api/parties` (user) `{name?, speakers?, preset_id?, take?: bool}` → 200 party | 409 `{error:"speaker_taken", zone, room, party_id, party_name}` | 400 `{error:"mixed_household"}`. Default name `"<user name>'s party"`.
  - `POST /api/parties/{id}/end` (user)
  - `POST /api/parties/{id}/speakers` (user) `{zone, take?}` → 409 as above when owned; `DELETE /api/parties/{id}/speakers/{zone}` (user)
  - `POST /api/parties/{id}/join` (guest/user) → `{ok}` (records the guest as a member, used for "who's here", no rights change)
  - `GET /api/parties/{id}/queue` → Sonos queue of the coordinator (reuse `/api/queue/{uuid}` logic via `commands`) with each item annotated `added_by: {id,name} | null` and `can_remove: bool` for the caller.
  - `POST /api/parties/{id}/queue` `{uri, metadata, title?}` → enqueue at end on coordinator + `record_add`.
  - `DELETE /api/parties/{id}/queue/{index}` → 403 `{error:"not_yours"}` when `can_remove` false.
  - `POST /api/parties/{id}/transport/{action}` action ∈ play,pause,next,previous; `POST /api/parties/{id}/volume` `{zone?: str, level: 0–100}` (zone must be in party; none = group volume).
  - Presets (user): `GET/POST /api/presets`, `PUT/DELETE /api/presets/{id}`, `POST /api/presets/{id}/start` `{take?}` (app path: 409 on conflict unless take).
- [ ] **Step 1: Tests** (TestClient over `backend.main.app` with auth on and monkeypatched engine/controller fakes): start → 200 and broadcast; conflict 409 body; guest `POST /api/parties` → 403; guest queue add → attribution shows guest name; guest delete other's item → 403; volume zone outside party → 400; preset start conflict 409, `take=true` 200.
- [ ] **Step 2–4.** **Step 5:** Commit `feat(home): parties and presets API`.

### Task 6: Home Assistant API (token) + docs

**Files:**
- Create: `app/backend/home/ha.py`; Modify `home/install.py`
- Create: `docs/home-assistant.md`
- Test: `app/tests/test_home_ha.py`

**Interfaces:**
- Consumes: Task 1 `register_bearer`, Task 5 engine/presets.
- Produces:
  - Token store `home_dir()/"ha_tokens.json"`; `POST /api/ha/tokens` (user) `{label}` → `{id, token}` (token shown once, stored as sha256); `GET /api/ha/tokens` (user) → list without secrets; `DELETE /api/ha/tokens/{id}`.
  - Bearer principal `Principal("service", "ha:<id>", "Home Assistant")`; `permissions.allowed` for kind `service` = only `/api/ha/*` routes (tighten Task 2: service ≠ full user).
  - `GET /api/ha/state` → `{presets: [{id,name}], parties: [{id,name,state,speakers:[room names],track:{title,artist,album,art}}]}` (track from coordinator transport state).
  - `POST /api/ha/presets/{id}/start` → starts with `take=True` (spec: no prompt from HA).
  - `POST /api/ha/parties/{id}/end`.
  - `POST /api/ha/parties/{id}/tv/{tv_id}` → returns 501 `{error:"phase3"}` for now (route reserved for Phase 3).
  - `docs/home-assistant.md`: YAML for `rest_command` (start preset, end party), a `rest` sensor on `/api/ha/state` (scan_interval 10 s) with template sensors per party, a dashboard button example, and how to create the token in Sonora.
- [ ] **Step 1: Tests:** token create/list/delete; bearer reaches `/api/ha/state`, gets 403 on `/api/zones/x/group` and `/api/presets`; HA preset start takes owned speakers without 409; wrong token 401.
- [ ] **Step 2–4.** **Step 5:** Commit `feat(home): Home Assistant API`.

### Task 7: Frontend — auth plumbing, guest join page, routing

**Files:**
- Create: `app/frontend/src/home/api.js` (wraps `request` from `lib/api.js`; export it from there if not exported), `app/frontend/src/home/useMe.js`, `app/frontend/src/home/JoinPage.jsx`, `app/frontend/src/home/home.css`
- Modify: `app/frontend/src/main.jsx` (route: path `/join` → `<JoinPage/>` without `SystemProvider`; `/party*` → `<PartyApp/>` (Task 8) inside providers; else `<Guarded/>`), `app/frontend/src/lib/api.js` (on 401 `{error:"login"}` → `location.assign('/auth/login?next=' + encodeURIComponent(location.pathname))`; on guest 403 surface `ApiError` kind `forbidden`).
- Modify: `app/frontend/src/App.jsx` — if `me.principal.kind === 'guest'` render `<PartyApp/>` instead of the theme shell; if user and `me.auth`, render a fixed-position "Parties" button (bottom-left, above theme UI, `z-index` high, uses theme CSS vars `--t-bg`/`--t-fg` fallbacks) linking to `/party`.
- i18n: keys `home.join.title`, `home.join.name`, `home.join.go`, `home.join.bad`, `home.parties.button` in all catalogs.

**Interfaces:**
- Consumes: `/api/me`, `/auth/guest`.
- Produces: `useMe() -> {loading, auth, principal: {kind,id,name} | null}` (fetch once, cached module-level), `homeApi` object with methods matching Task 5 routes: `parties()`, `startParty(body)`, `endParty(id)`, `addSpeaker(id, body)`, `removeSpeaker(id, zone)`, `joinParty(id)`, `partyQueue(id)`, `enqueue(id, body)`, `dequeue(id, index)`, `transport(id, action)`, `volume(id, body)`, `presets()`, `savePreset(body)`, `updatePreset(id, body)`, `deletePreset(id)`, `startPreset(id, body)`, `guestPass()`, `rotateGuestPass()`, `haTokens()`, `createHaToken(label)`, `deleteHaToken(id)`.
- `JoinPage`: reads `token` from query, asks first name (remembered in `localStorage['sonora.guestName']`, try/catch), POST `/auth/guest`, then `location.assign('/party')`; bad token → message.
- [ ] **Step 1:** Build passes before change (`npm run build`). **Step 2:** Implement. **Step 3:** `npm run build` passes; backend gate (i18n parity) passes. **Step 4:** Manual check with auth off: `/` unchanged; with `SONORA_OIDC_ISSUER` set and a guest token, `/join?token=…` → name → lands on `/party` (Playwright/Firefox optional per DEVELOPING.md). **Step 5:** Commit `feat(home): login plumbing and guest join page`.

### Task 8: Frontend — Party page (hub, party view, start/move, presets, guest QR)

**Files:**
- Create: `app/frontend/src/home/PartyApp.jsx`, `PartyList.jsx`, `PartyView.jsx`, `StartParty.jsx`, `PresetEditor.jsx`, `GuestPass.jsx`, `HaTokens.jsx`, `AddSongs.jsx`
- Modify: `app/frontend/package.json` (add `"qrcode": "<exact current version>"`), `home.css`, all i18n catalogs (`home.*` keys).

**Interfaces:**
- Consumes: Task 7 `homeApi`, `useMe`; `useSystem()` from `lib/store.jsx` for zones/groups/households (room names, which household a zone is in, now playing for the coordinator), live `parties` websocket message → refetch `homeApi.parties()` (subscribe by adding a `parties`/`party_notice` case to the store reducer, bumping `partiesEpoch` and keeping the last notice).
- Behaviour:
  - **Hub (`/party`)**: running parties (name, rooms, now playing, owner) with Join/Open; for users: preset cards — **tap preset → confirm sheet with Start (2 taps)**; "New party" → `StartParty`; links to Guest pass and Home Assistant tokens.
  - **StartParty** (user): room checklist grouped by household (S1/S2 label from household name), once one room is picked rooms of other households are disabled (spec §5); per-room volume slider; Start. On 409 `speaker_taken` → dialog "Move {room} from {party} to yours?" → retry with `take:true`.
  - **PartyView**: now playing (art, title, artist from coordinator zone in store), transport (play/pause/next/previous), group volume + per-room volume, queue (`homeApi.partyQueue`) with "added by {name}" and a remove button only when `can_remove`; refresh on `queueEpochs` for the coordinator and `partiesEpoch`. Users additionally: add/remove rooms (same move dialog), End party, Save as preset. Notices ("Kitchen left the party") as a transient banner.
  - **AddSongs** (everyone in the party): search box → for each configured service from `GET /api/services` (use the same response fields the themes use; read `lib/sonosServices.js`) call `GET /api/services/{sid}/search?term=&zone=<coordinator>` and list tracks; plus Sonos Favorites (`GET /api/favorites`). Tap result → `homeApi.enqueue(id, {uri, metadata, title})`. Read how themes build `uri`/`metadata` from a search result (`lib/items.js`, `parts/Browse.jsx`) and reuse those helpers rather than re-deriving.
  - **GuestPass** (user): QR of `${location.origin}/join?token=…` (qrcode → data URL `<img>`), copy link, Rotate.
  - **HaTokens** (user): list/create (show token once)/delete.
  - Narrow-first layout (phone), 16px gutter, works at 360px width; uses CSS vars with fallbacks so it looks fine under any theme.
- [ ] **Step 1:** `npm run build` baseline. **Step 2:** Implement components. **Step 3:** build + backend gate (i18n parity). **Step 4:** Manual run against real or faked backend: start, move dialog, guest add + remove-own. **Step 5:** Commit `feat(home): party page`.

### Task 9: Docs, compose, config

**Files:**
- Modify: `README.md` (new top section "Sonora Home additions" — parties, accounts, HA — linking to docs), `docker-compose.yml` (commented env vars), `SECURITY.md` (login model now: Authentik users + guest pass; auth off when issuer unset).
- Create: `docs/authentik.md` — Authentik provider/application setup (OAuth2/OIDC provider, confidential client, redirect URIs for **both** `https://<proxy-host>/auth/callback` and `http://apps-vm:50205/auth/callback`, scopes incl. `groups`, optional group restriction via `SONORA_OIDC_GROUP`), env vars table (`SONORA_OIDC_ISSUER`, `SONORA_OIDC_CLIENT_ID`, `SONORA_OIDC_CLIENT_SECRET`, `SONORA_OIDC_GROUP`, `SONORA_TRUST_PROXY`).
- [ ] **Step 1:** Write docs. **Step 2:** Full gates. **Step 3:** Commit `docs(home): accounts, parties, Home Assistant`.

## Execution waves (for parallel agents)

- **Wave A (parallel):** Task 1, Task 3, Task 4.
- **Wave B (parallel):** Task 2 (needs 1), Task 5 (needs 1, 3, 4; codes against Task 2's `request.state.principal` contract).
- **Wave C (parallel):** Task 6 (needs 5), Task 7 (needs 1/2 API), Task 9.
- **Wave D:** Task 8 (needs 5, 7).
