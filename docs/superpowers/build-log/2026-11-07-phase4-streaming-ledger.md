# SDD ledger — plan: docs/superpowers/plans/2026-11-07-phase4-streaming.md

Spec §12. Integration branch home.

## Preflight
| Pair/task | Finding |
|---|---|
| T1↔T2/T3/T5 | LiveHub.start/feed/end + input_format (T3 needs 48k input) — T1 must accept input_format param |
| T1↔P3T3 | analysis.register_source guarded import |
| T4 | independent of T1 |
Ruling: T1 includes an `input_format` param (rate, channels) so T3/T5 can feed non-44.1k PCM — cost: none.

## Progress
T1 dispatched BASE 2728767
Task 1: implementer DONE 682c881 (resume plays only if queue was playing; zone targets resume; 64KB backlog)
T4 dispatched BASE b1d599d
Task 1: review: listener hang after end; resume vs start race; fix round 1 sent (+open failure resume, task refs, close timeout) FIX_BASE 682c881
Task 1: minor (deferred): dropping bytes ignores MP3 frame boundaries (decoders resync); on_end listeners run inside engine lock; radio/line-in not restored after live (owner-visible)
Task 4: implementer DONE 23c21f7 (raw body upload, no python-multipart; on_end added — conflicts with T1)
Task 4: review approve w/ fixes (aggregate cap 2 GB/party + 1 GB free floor; upload vs party-end race; control chars); fix round 1 sent FIX_BASE 23c21f7
Task 4: minor (deferred): _find scans party folders per request; loose mp3 sniff; mp4 video accepted as audio
Task 1: fix round 1/5 (5 addressed; commits 682c881..4a461fa); new minor: spurious CancelledError to waiters on party end — fix round 2 sent FIX_BASE 4a461fa
Task 4: fix round 1/5 (3 addressed, 0 open; commits 23c21f7..948b361)
Task 4: minor (deferred): disk check double-counts size (conservative); concurrent uploads can overshoot cap / rmdir race
Task 4: complete (commits b1d599d..948b361, review clean)
Task 1: fix round 2/5 (1 addressed, 0 open; commits 4a461fa..2353dfc)
Task 1: complete (commits 2728767..2353dfc, review clean)
T2 + T3 dispatched BASE fdcbba3
Task 3: implementer DONE 6572395 (lifespan wrap in install.py; silence never starts a session)
Task 3: minor (deferred → final): 64-source cap never evicts (admit mapped IPs); rate multiplier unbounded (cap 384 kHz); target JSON uses {kind,id} (frontend must match)
Task 3: complete (commits fdcbba3..6572395, review clean)
Task 2: implementer DONE 3cc6aef (avahi+dbus in container entrypoint; shairport 4.3.7; lifespan wrap; routes.py change-listener hook). Ruling: room receivers exclude offline rooms and rooms currently in a party (use the party's receiver) — cost: fewer AirPlay names shown.
Task 2: review changes requested (room filter ruling, SONORA_AIRPLAY off switch, avahi probe/crash loop, entrypoint set -e); fix round 1 sent FIX_BASE 3cc6aef
Task 5: dispatched BASE 43c9699
Task 2: fix round 1/5 (5 addressed, 0 open; commits 3cc6aef..b6482c8)
Task 2: minor (deferred): offline room removal may try a resume seek on a gone speaker; _stop doesn't catch process.stop errors; avahi probe timeout leaves dbus-send; no test for _pump error path; given-up receiver stays dead until restart
Task 2: complete (commits fdcbba3..b6482c8, review clean)
Task 5: implementer DONE c9d4401
Task 5: minor (deferred → final): 256 KB cap after buffering (lower ws_max_size); misleading 4500 on encoder stall; silence needs exact zeros (add small threshold, also LiveHub idle test); pump non-Stop exceptions logged at debug
Task 5: complete (commits 43c9699..c9d4401, review clean)
Final review: approve w/ fixes. Important: continuous senders re-take after "replaced" (AirPlay/Scream ping-pong); room-in-party and party live sessions coexist; no tabsound decoder cap; silence checks exact-zero & inconsistent. Fix-now minors: scream eviction, rate cap, airplay _stop, avahi probe kill, scream format change w/o resume, ended-party mapping retry, radio/line-in restore after live, listener cap 16, init/tini, Scream in configs/docs, avahi rename doc.
Ruling: fix all Important + listed minors in one wave — cost: one more wave.
Final fix wave re-review: 9/9 addressed; residual Important — a pending resume on another target can cut a newer live session on the same coordinator. Ruling: fix (load-bearing for "newer sender wins") as the smallest change: _resume_now skips & hands over when the coordinator is busy; rooms_changed skips zone resumes — cost: one small change after the single wave.
Final: minor (deferred): zone start racing a join; stride sampling may miss short bursts
Final residual fix b4086e7 verified by controller (diff read) — Ruling: no extra re-review agent — cost: none.
Phase 4: final review clean.
Post-merge smoke: final image works (pages, config visuals exact, ffmpeg 7.1.5, node 26, shairport 4.3.7, yt-dlp+ejs). Bug found: avahi probe fails when app uid has no passwd entry (dbus refuses) → AirPlay silently off for bind-mounted/Unraid data dirs. Fix dispatched (fix-dbus-uid).
fix-dbus-uid merged (99684b0), controller read the diff; verified in Docker with a uid-1000 bind mount (receivers on).
