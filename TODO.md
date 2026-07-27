# TODO

## Open
- **G5 verify — WBM form-answer field IDs (jakob live-applies to WBM).** jakob's
  `send_modes` are wbm/gewobag/howoge = **live**. Howoge auto-submit is verified
  working (16-field payload, 2026-07-27). WBM could NOT be verified because the
  WBM list fetch was bot-checked (401) that day. `form_answers.salutation: "Herr"`
  was added to jakob so WBM/Gewobag get *Anrede*, but the powermail field id is
  still **assumed** (`powermail_field_anrede`, a `<select>`). When WBM is
  reachable again, dry-run `WbmApplicator` against a live listing (or run
  `scripts/discover_forms.py`) to confirm the anrede/WBS field names and that a
  no-Anrede vs Anrede submit validates. `applicator.py:_wbm_answer_fields` /
  `WbmApplicator.apply`.
- **WBM crawler currently IP-bot-blocked (401 "Bot check")**, like Kleinanzeigen —
  datacenter-IP throttling, not a code regression. WBM crawl + applies won't fire
  until it clears. Watch via the crawler watchdog.

## Done (2026-07-27)
Cleared from the parity/code-review backlog:
- B3 dry-run retry, B5 IMAP reply/confirm heuristic, B7 send_mode fallback,
  B8 adapter disabled-source handling, B10 manual-apply dedup, B11 addy mint
  negative-cache, B12 per-chat alert dedup, G6 opt-in retention/prune.
