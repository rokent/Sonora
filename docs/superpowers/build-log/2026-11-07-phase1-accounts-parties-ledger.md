# SDD ledger — plan: docs/superpowers/plans/2026-11-07-phase1-accounts-parties.md

Spec: docs/superpowers/specs/2026-11-07-sonora-home-design.md. Integration branch: `home` (base 974c341).

## Preflight scan
| Pair / task | Shared | Finding |
|---|---|---|
| T1↔T2 | principal_of, Principal, auth_enabled | consistent |
| T1↔T6 | register_bearer, Principal kind service | T2 says service=full user; T6 tightens to /api/ha/* only → Ruling below |
| T2↔T5 | GUEST allowlist ↔ party routes | paths match (`/api/parties/{id}/queue`, transport, volume, join) |
| T3↔T5 | PartyEngine API, SpeakerTaken, MixedHousehold | consistent |
| T4↔T5 | Presets API | consistent |
| T5↔T7 | homeApi methods ↔ routes | consistent |
| T7↔T8 | homeApi, useMe, PartyApp import in main.jsx/App.jsx | T7 references PartyApp before T8 exists → Ruling below |
| T1 self | tests vs code | ok |
| T2 self | install.py created; T5/T6 modify it | ok |
| T3 self | ok | |
| T4 self | ok | |
| T5 self | attribution via set_source hook in main.py or middleware | builder picks; prefer middleware |
| T6 self | ok | |
| T7 self | ok | |
| T8 self | ok | |
| T9 self | ok | |

Ruling: service principal (HA token) may reach only `/api/ha/*` (T6 wins over T2's "user/service → True") — spec §11 limits HA to parties/presets/TV — cost if wrong: HA can't call other routes; easy to widen.
Ruling: T7 creates a placeholder `home/PartyApp.jsx` (renders "Parties" heading) so the build passes; T8 replaces it — cost: none.
Ruling: run waves in parallel in separate git worktrees (.worktrees/p1-tN, branches p1-tN off `home`), controller merges each into `home` after review — user explicitly asked for parallel agents; skill's "never parallel implementers" is about shared trees — cost if wrong: merge conflicts, resolved at merge.
Ruling: tests run with `PYTHONPATH=$PWD ../../../.venv/bin/python -m pytest` from each worktree's `app/` (shared venv has an editable install pointing at the main tree) — cost: none.

## Progress
Wave A dispatched: T1 (sonnet, agent ab95…), T3 (opus, a419…), T4 (sonnet, a3b4…); BASE 974c341
Task 4: minor (deferred): bool volume guard dead (True→1), floats truncated; names >60 truncated not rejected; stray volumes dropped silently; corrupt file swallowed w/o log; shallow copies from list()/get()
Task 4: note for T5: household_of must return None (not "") for unknown speakers
Task 4: complete (commits 974c341..9dc2ef3, review clean)
Task 1: implementer DONE_WITH_CONCERNS adb55a8; review dispatched (opus)
Ruling: no httpx; tests use T1's in-file ASGI cookie client, to be shared as a test helper for T2/T5/T6 — keeps zero new deps (lock files are hash-pinned) — cost: slightly more test plumbing.
Task 1: review changes requested (t→token; also folded: /api/me 200 principal null, typ field, atomic+cached key, 0600 mkstemp fsync store, rotate ends guest sessions); fix round 1 sent to implementer (FIX_BASE adb55a8)
Task 1: minor (deferred): IdP errors → 500 not 502; TRUST_PROXY trusts any client; guest name truncation/control chars (frontend must escape); logout POST no CSRF; 30-day sessions not revocable on Authentik group removal (owner-visible)
Task 3: implementer DONE b6e14ed (concerns: delegate on lead removal, lead-moved-out drops others, no lock → T5 must serialise, record_remove added, add_speaker offline)
Task 1: fix round 1/5 (6 addressed, 0 open; commits adb55a8..24f1cb5)
Task 1: minor (deferred): os.link key creation fails on no-hardlink FS; GuestPass.token() rotates on any read failure (should rotate only when missing)
Task 1: complete (commits 974c341..24f1cb5, review clean)
Task 2: dispatched (opus, BASE a40908d, worktree p1-t2). Ruling: shared test helper app/tests/home_client.py from T1's ASGI client — cost: none.
Task 3: review changes requested (queue not captured w/ silence; lead-moved drops wrong speakers; busy window before settle → self drops + stack loss); fix round 1 sent (+lock, normalize-first, refuse when no before) FIX_BASE b6e14ed
Task 3: minor (deferred): rejoined speakers get no volumes entry; atomic write w/o fsync; revert may pull a speaker into another party's group unowned (hardware check)
Task 2: implementer DONE 896106f (concerns: served /join,/party; GET /party* guest; logos/icons refused for guests; ingress prefix ignored)
Task 3: fix round 1 implementer DONE 9dd63e2; re-review dispatched. Concern for T5: map NoSpeakers → 409/503
Task 3: fix round 1/5 (6 addressed, 0 open; commits b6e14ed..9dd63e2)
Task 3: minor (deferred): NoSpeakers after takes leaves taken speakers partyless w/o stack; queue restore needs lead transport record (lead-was-follower case); settle timeout 4 s then late event may drop
Task 3: complete (commits 974c341..9dd63e2, review clean)
Task 9: dispatched early (docs only, BASE 1cad40b)
Task 2: review: I1 /api/library on guest list (admin+mutating), I2 route-walk test can't fail; fix round 1 sent (+ origin-first, explicit parties GETs, \d+ sid) FIX_BASE 896106f
Ruling: drop GET /api/library from GUEST although the plan lists it — it's library settings (SMB shares) and mutates; guests browse via /api/browse — cost if wrong: guests can't see library settings (they shouldn't).
Ruling: origin.guard runs before the auth guard — defence in depth for DNS rebinding — cost: none.
Ruling: HA-ingress login redirect left as is (ingress is not a primary target per spec §11 deployment ruling) — cost: auth-on behind ingress redirects to HA's /auth/login.
Task 9: minor (deferred): README guest bullet should list guest rights; where the guest QR lives in UI
Task 9: complete (commits 1cad40b..3bbf11b, review clean)
Task 2: fix round 1/5 (5 addressed, 0 open; commits 896106f..660d07b)
Task 2: minor (deferred): test_routes_unique.py skips include_router routes; origin-order test leans on Starlette internals
Task 2: complete (commits a40908d..660d07b, review clean)
Wave B/C: T5 dispatched (opus) + T7 dispatched (sonnet), BASE 4dbd570
Task 7: implementer DONE 01ba863; review: theme shell flashes to guest before /api/me; JoinPage assign w/o withBase; fix round 1 sent (+ next slashes, join errors, dvh, safe-area) FIX_BASE 01ba863
Ruling: one blank placeholder frame while /api/me loads, also with auth off — prevents guests seeing the theme shell — cost: brief blank frame on upstream path.
Task 7: minor (deferred): Parties button z-index/overlap; /party for non-guest unauth handled in T8
Task 7: fix round 1/5 (4 addressed; re-review flagged SystemProvider fetch for guests)
Ruling: SystemProvider fetch/ws for guests is intended — /api/state and /ws are on the guest allowlist and the party page (T8) uses useSystem; my re-review prompt over-stated the finding — cost if wrong: guests receive the household snapshot (already allowed by spec).
Task 7: complete (commits 4dbd570..4f95c93, review clean)
Task 5: implementer DONE a489d80 (concerns: album enqueue attribution by container URI; clear/reorder untracked; members in-memory)
Task 5: review: Important guest-remove duplicate-URI matching; fix round 1 sent (+ cap start, log engine failure) FIX_BASE a489d80
Task 5: minor (deferred): DELETE holds _queue_lock during main route (slow speaker blocks queue ops); engine() re-subscribe on new path (tests only); replace/clear/reorder don't update attribution; album enqueue attributed by container URI
Task 5: fix round 1/5 (3 addressed, 0 open; commits a489d80..0a16f32)
Task 5: complete (commits 4dbd570..0a16f32, review clean)
Wave C/D: T6 (sonnet) + T8 (opus) dispatched BASE f6168e7. Ruling: RemoteBrowser (P2T5) wiring into PartyView deferred to P2 T4 — cost: none.
Task 6: implementer DONE f37bbe0
Task 6: review approve; required: drop committed node_modules symlink (fix round 1 sent). minor (deferred): token file reread per request; art URL from request origin may not be reachable by HA; YAML not validated in real HA
Task 6: fix round 1/5 (1 addressed: symlink untracked, d2a34dc; verified by controller via git show --stat + ls-files — Ruling: no re-review agent for a pure untrack commit — cost: none)
Task 6: complete (commits f6168e7..d2a34dc, review clean)
Task 8: implementer DONE b275fdd (hash routing /party#/p/<id> due to ./assets relative paths; per-room volumes set after start)
Task 8: review approve w/ fixes (lazy PartyApp + qrcode; remove btn only can_remove; Escape topmost sheet); fix round 1 sent FIX_BASE b275fdd
Ruling: hash routing /party#/p/<id> kept (relative ./assets break under /party/...) — cost: URLs carry a hash; a <base href> backend fix could restore paths later.
Task 8: minor (deferred): double queue refetch on add; positional delete race; move dialog names only first taken room; per-room start volumes silent on failure; AddSongs silently skips needs_auth services
Task 8: fix round 1/5 (3 addressed, 0 open; commits b275fdd..4d2d1c9)
Task 8: minor (deferred): Suspense fallback null (blank) vs neutral bg div
Task 8: complete (commits f6168e7..4d2d1c9, review clean)
Final review: approve w/ fixes. Ruling: fix Important 1 (party drops speakers when something else plays on them) and Important 2 (party with 0 speakers keeps its queue and puts it back on the next speaker) now — the spec is binding (§8) — cost: more engine code. Also fixing: Important 3 (store.write_json + corrupt-file quarantine), is_user=kind user, catch-up snapshot after engine create/locked change, /ws/visual via ws_allowed, QueueBody caps, guest expiry → /join, user opening guest QR guarded, Parties button with auth off, display on / → /tv.
Ruling: T1 30-day sessions — document "delete secret.key to sign everyone out" (no behaviour change) — cost: removal from Authentik group isn't immediate.
Final fix wave: 9/9 addressed (fdcbba3..6cd8ed8). Residual Important: restart marks all parties seen → false drop. Ruling: fix (load-bearing — would drop every party after a restart) as the smallest change: persist seen per party; plus skip put_queue when snapshot read failed; try/except in routes._catch_up — cost: one extra small change after the single fix wave.
Final: minor (deferred): add_speaker vs drop-path queue read race; Sonos-app queue replace adopted silently; bare /join shows expired message to a valid guest
Final residual fix 21ba431 verified by controller (diff read: seen persisted, put_back skips without snapshot, catch-up try/except) — Ruling: no extra re-review agent — cost: none.
Phase 1: final review clean.
