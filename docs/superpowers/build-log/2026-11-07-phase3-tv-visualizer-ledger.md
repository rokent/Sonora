# SDD ledger — plan: docs/superpowers/plans/2026-11-07-phase3-tv-visualizer.md

Spec §10, §11, §8.1. Integration branch home.

## Preflight
| Pair/task | Finding |
|---|---|
| T1↔T2/T6 | display links API consistent |
| T3↔T4 | frame format {t, w(base64 512 u8)} consistent |
| T5↔T6 | tvs routes consistent |
| T2 | needs Phase 2 T4 VideoWindow — Ruling: if not merged yet, T2 renders cover for video mode and P2T4/T2 wiring done when both exist |
| T3 | analysis config field added to home/config_routes.py (from P2 T5) |
Ruling: library (x-file-cifs) tracks use approximate visuals (spec builder's call) — cost: no exact visuals for NAS songs.

## Progress
T3 + T5 dispatched BASE adb1b92
T1 + T7 dispatched BASE d483e28
Task 5: implementer DONE 5434a1b (concerns: WOL path ~15-20s; pair waits 30s; preset start waits for TV; org.tizen.browser vs Samsung Internet app id on 2022+)
Task 5: review approved w/ recs; Ruling: apply now — background TV show on preset start (HA rest_command 10 s timeout) + Samsung Internet probe fallback — cost: slightly more code; fix round 1 sent FIX_BASE 5434a1b
Task 5: minor (deferred): pairing waits 30 s for Allow (beyond 5 s rule, justified); real TLS/Samsung behaviour unverified
Task 7: implementer DONE c8c8760 (image 1.19 GB with ffmpeg). Ruling: accept Debian ffmpeg size (~+600MB) for now — cost: bigger pulls; a static ffmpeg build could shrink it later.
Task 7: minor (deferred → final review): reconcile tv-mode.md wording (/auth/display, tv_needs_pairing status, HA endpoint table) with T1/T5/T6 final code
Task 7: complete (commits d483e28..c8c8760, review clean)
Task 3: implementer DONE 771ce12 (frames {t,w}; ws closes 4001 no ffmpeg / 4004 unknown party; concern: PCM sources disabled without ffmpeg)
Task 1: implementer DONE bdfabcb (display gets guest view of parties; auth-off /api/displays/me?display=)
Task 3: review approve w/ I-1 (PCM source destroyed on idle) + minors; Ruling: fix now (Phase 4 depends) — reader stays alive while source registered, skip publishing w/o viewers; fix round 1 sent FIX_BASE 771ce12
Task 3: minor (deferred): stale _pending_sources on loop change; u8 conversion per-byte; Analyzer.close not called (relies on loop shutdown); PCM sources disabled w/o ffmpeg (TODO per-party mode)
Task 1: review dispatched (opus)
Task 5: fix round 1/5 (3 addressed, 0 open; commits 5434a1b..e34e957)
Task 5: minor (deferred): no test for generic tv_failed branch; non-TvError from prepare (e.g. OSError) could fail a started preset — widen except
Task 5: complete (commits adb1b92..e34e957, review clean)
Task 1: review approve; fix round 1 sent (can_remove fail-closed is_user) FIX_BASE bdfabcb
Task 1: minor (deferred): opening a display link in a user's browser replaces the user session; POST url built from Host header; DisplayLinks writes unlocked
Task 1: fix round 1/5 (1 addressed; 75d28a2; 19-line diff verified by controller — Ruling: no re-review agent for a 2-line logic swap + tests — cost: none)
Task 1: complete (commits d483e28..75d28a2, review clean)
Task 3: fix round 1/5 (5 addressed, 0 open; commits 771ce12..d41e387)
Task 3: minor (deferred): close() doesn't await cancelled readers; _pending_sources keeps finished sources
Task 3: complete (commits adb1b92..d41e387, review clean)
T4 dispatched BASE b1d599d
Task 6: dispatched BASE 0e403e0
Task 6: implementer DONE 57504df
Task 6: minor (deferred → final review): orphan display link when stored URL missing / concurrent first shows; TV rename leaves old link name; TvSettings add clears form on failure; ShowOnTv mode from first TV; Show now picks first party; TV links visible in DisplayLinks; duplicate import
Task 6: complete (commits 0e403e0..57504df, review clean)
Task 4: implementer DONE_WITH_CONCERNS 8f010c5. Ruling: build-time compilation of butterchurn preset equations (vite plugin) instead of loosening CSP — cost: plugin maintenance if butterchurn changes its preset format.
Task 4: review approve w/ fixes (context loss; reconnect loop; mic race; globals via window); fix round 1 sent FIX_BASE 8f010c5
Task 4: fix round 1/5 (4 addressed, 1 regression: missing enabled treated as disabled); fix round 2 sent FIX_BASE 02f74ac
Task 4: fix round 2/5 (claimed regression refuted with evidence — loadPreset fills enabled:0 default; defensive change applied anyway + backoff; 16-line diff verified by controller); commits 02f74ac..909a54d
Task 4: minor (deferred): context never restored → frozen canvas, no message
Task 4: complete (commits b1d599d..909a54d, review clean)
Task 2: dispatched BASE c22a9dd
Task 2: implementer DONE 3b2a592 (added display→/tv redirect, which p1-final also added — merge conflict expected in App.jsx)
Task 2: review approve w/ fixes (lyrics consent for non-display, 401 next query, memo); fix round 1 + merge home sent FIX_BASE 3b2a592
Task 2: fix round 1/5 (5 addressed, 0 open; commits 3b2a592..6049e1a incl. merge of home)
Task 2: complete (commits c22a9dd..6049e1a, review clean)
Final review: approve w/ fixes. Important: .aac not recognised by videoSync MEDIA_URI; HA can't learn tv_id; HA show call may exceed HA 10 s timeout; display token in uvicorn access log; HA iframe cross-site cookie (SameSite).
Ruling: also wire exact visuals for live (Phase 4) sources on the TV page — spec §10.2 promises it — via a "live" alignment mode (frames by arrival + fixed adjustable delay), without touching live.py (parallel P4 fix) — cost: approximate sync for live visuals.
Final fix wave re-review: 8/8 addressed, READY. Minor (deferred): HA TV route 500 on non-TvError (no half-done action); preset banner relies on PartyApp staying mounted; TV offset per-browser localStorage only.
Phase 3: final review clean.
