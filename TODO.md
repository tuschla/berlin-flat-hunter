# TODO — deferred backlog

Low-severity items surfaced by the pi→berlin parity + code-review audit
(2026-07-27). None affect normal operation; deferred deliberately.

## Auto-apply
- **B3 — failed dry-run sends are never retried.** `Store.has_send` (used for
  `dry_run` dedup) doesn't filter `ok`, so a failed dry-run attempt is marked
  "sent" and never retried, unlike `live` (which uses `has_live_send`, `ok=1`
  only). Fix: dedup dry-run on `ok=1` too, or don't `record_send` a failed
  dry-run. `store.py` / `applicator.py:_already_sent`.
- **B10 — ManualApplyRequired re-notifies.** Manual-apply records `ok=False`, so
  the per-recipient dedup never suppresses it; if the listing re-reaches the
  applicator it re-fires the "apply by hand" alert. Masked today by flathunter's
  `filter_already_seen`. `applicator.py:process_expose`.
- **G5 verify — WBM form-answer field IDs.** `form_answers` (Anrede/WBS/income)
  are filled into WBM's powermail form best-effort assuming
  `powermail_field_<catalog-name>`; verify against the live form
  (`scripts/discover_forms.py`) before trusting WBS/income delivery.
  `applicator.py:_wbm_answer_fields` / `WbmApplicator.apply`.

## addy.io
- **B11 — mint failure not cached.** On `AddyError`, `AliasResolver` degrades to
  the real email but caches nothing, so every listing re-hits the addy API while
  the key/endpoint is down. Fix: negative-cache a failed mint for a short TTL.
  `email_alias/resolver.py:_alias_for`.

## IMAP
- **B5 — reply vs confirm heuristic.** A Genossenschaft-domain mail with no
  recognised confirm-token link is surfaced as a "landlord reply" (so newsletters
  false-alert), and an opaque-hash opt-in link (no `confirm/bestätig/...`
  substring) is never auto-confirmed. Fix: tighten the reply heuristic / broaden
  confirm-link detection. `email_confirm/imap_reader.py`.

## Config / notify (cosmetic)
- **B7 — send_mode fallback.** `send_mode_for` returns `dry_run` (not `off`) for
  a crawler absent from `send_modes` when `auto_apply.enabled` is true. Harmless
  today (Kleinanzeigen gets no URL from the JSON adapter). `config.py`.
- **B8 — adapter spins applicator needlessly.** `profile_json` builds
  `auto_apply.enabled` from send_modes ignoring per-source `enabled`; a disabled
  source with `send_mode: live` still flips it on (no wrong apply, just wasted
  setup). `profile_json.py`.
- **B12 — alert-channel dedup granularity.** Crawler-down alert channels dedup on
  `(token, tuple(chats))`; the same bot with partially-overlapping `receiver_ids`
  across profiles would double-alert the shared chat. `orchestrator.py:_wire_shared_alert_channels`.

## Ops (very low urgency)
- **G6 — retention/prune.** No prune anywhere; DBs grow unbounded. Non-issue for
  years (data is ~5 MB, 791 GB free), but add a `prune_old(days)` to
  `StatsLogger`/`Store` + a `retention_days` config if the box runs for a very
  long time.
