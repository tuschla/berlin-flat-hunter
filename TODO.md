# TODO

## Open
- **G5 verify — WBM form-answer field IDs.** `form_answers` (Anrede/WBS/income)
  are filled into WBM's powermail form best-effort assuming field ids of the form
  `powermail_field_<catalog-name>` (see `applicator.py:_wbm_answer_fields` /
  `WbmApplicator.apply`). Verify against the live form with
  `scripts/discover_forms.py` and correct any names before relying on WBS/income
  delivery. Low urgency — no current profile has WBM form_answers that matter
  (jakob's are empty, phil is notify-only).

## Done (2026-07-27)
Cleared from the parity/code-review backlog:
- B3 dry-run retry, B5 IMAP reply/confirm heuristic, B7 send_mode fallback,
  B8 adapter disabled-source handling, B10 manual-apply dedup, B11 addy mint
  negative-cache, B12 per-chat alert dedup, G6 opt-in retention/prune.
