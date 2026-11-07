# SDD ledger — plan: docs/superpowers/plans/2026-11-07-phase2-youtube.md

Spec: docs/superpowers/specs/2026-11-07-sonora-home-design.md §9. Integration branch home.

## Preflight
| Pair/task | Finding |
|---|---|
| T1↔T2 | audio_source/info/Video consistent |
| T2↔T3 | MediaKeys/media_url/didl_for consistent |
| T3↔T4 | queue items source/video_id; routes consistent |
| T4 | depends on Phase 1 T8 files (AddSongs/PartyView) |
| T5 | Dockerfile + config route independent |
Ruling: Phase 2 T1 runs in parallel with Phase 1 T5/T6/T8 (touches only new files + pyproject/locks) — cost: lock merge conflict if Phase 1 adds deps (it adds none).

## Progress
Task 1: implementer DONE 58e761c (yt-dlp 2026.10.26; locks regenerated bumped stale pins)
Task 1: minor (deferred): caches unbounded + no in-flight dedupe; search doesn't clamp limit/empty term (route must); info/audio_source need parse_video_id first (route must)
Ruling: accept the regenerated locks' stale-pin bumps (fastapi 0.162.2, pillow 13, aiohttp 3.14.5, uvicorn 0.54, pytest 9.2) — the old locks were below pyproject's own floors; shared venv synced to the dev lock and full suite re-run — cost if wrong: an upstream behaviour change, caught by the suite.
Task 1: complete (commits 3bc0437..58e761c, review clean)
Wave B: T2 + T5 dispatched BASE 496e2de
Task 5: implementer DONE 1dcd888 (docker build ok, node via copy + libatomic1, image 560MB)
Task 2: implementer DONE 239a901
Task 5: review: config route not fail-closed for non-user kinds; node copied from BUILDPLATFORM stage breaks arm64; fix round 1 sent (+docs consistency, z-index) FIX_BASE 1dcd888
Task 5: minor (deferred): iframe no sandbox/referrerpolicy (trusted household service)
Task 2: minor (carried to T3): drop Content-Length on 416; issue() validate youtube id; forward only bytes=\d*-\d* ranges; minor (deferred): no sock_read timeout; release on disconnect via GC (same as upstream stream_audio)
Task 2: complete (commits 496e2de..239a901, review clean)
Task 3: dispatched BASE b46b5fe
Task 5: fix round 1/5 (4 addressed, 0 open; commits 1dcd888..40aa0ab)
Task 5: complete (commits 496e2de..40aa0ab, review clean)
Task 3: implementer DONE 5977199
Task 3: review: guest can use next (queue jump); next adds unattributed; fix round 1 sent FIX_BASE 5977199
Ruling: user next-adds of YouTube items are attributed (media keys unique per add so last-copy matching is exact) — cost if wrong: attribution off for a Play-Next YouTube item.
Task 3: minor (deferred): no rate limit on guest YouTube search/probe
Task 3: fix round 1/5 (3 addressed, 0 open; commits 5977199..2769630)
Task 3: complete (commits b46b5fe..2769630, review clean)
Task 4: dispatched BASE 0e403e0
Task 4: implementer DONE c074ffd (VideoWindow props positionAt=performance.now ms; transportAnchor helper)
Task 4: minor (deferred): hosts get no Play next for YouTube; fixed slider id; no drift correction without infoDelivery; video lookup first 200 queue items; Searching… shown before debounce
Task 4: complete (commits 0e403e0..c074ffd, review clean)
Final review: NOT READY — C1 yt-dlp-ejs missing (YouTube JS challenges); I1 fMP4 relay untested on Sonos; I2 no guest rate limit; I3 double extraction; I4 neko guests can take control; I5 no yt-dlp update doc; M1 type=url; M2 sock_read.
Ruling: fix all in one wave; for I1 add `SONORA_YOUTUBE_FORMAT=m4a|aac` (aac = ffmpeg `-c:a copy -f adts` live remux, no seek) with m4a default, owner tests on real speakers — cost: an extra code path.
Final fix wave DONE (0ba4df1..a7148b3; extra: wait for googlevideo available_at before relaying). Note: agent ran docker image prune -f (removes dangling images machine-wide) — tell owner.
Final fix wave re-review: 8/8 addressed, approve. Minor (deferred): ffmpeg left running if cancelled during until_available in _aac; long worst-case latency before 502; visualizer first start ~5 s behind (self-corrects); priority caller joining guest extraction; dependabot daily for all pip.
Phase 2: final review clean. Owner QA owed: m4a and aac modes on real S1/S2 speakers.
