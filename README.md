# berlin-flat-hunter

Berlin-focused flat-hunting tool. Thin extension layer on top of [flathunter](https://github.com/flathunters/flathunter):

- **Adds** Berlin public-housing crawlers (Gewobag, WBM, Gesobau, Howoge, degewo)
- **Adds** shared scraping — one crawl per source per cycle, fanned out to many profiles
- **Adds** auto-application for Gewobag, WBM, Kleinanzeigen (Selenium) and Howoge (requests), with per-source `off`/`dry_run`/`live` modes
- **Adds** per-source Telegram routing + an optional per-cycle heartbeat channel
- **Adds** addy.io email aliases (a distinct forwarding address per landlord) with one application per address
- **Adds** IMAP double-opt-in auto-confirm + landlord-reply Telegram alerts
- **Adds** polygon-based area filtering
- **Adds** Ollama (local LLM) listing filter and per-application gate
- **Adds** Kleinanzeigen scam filter (keyword denylist for advance-fee templates)
- **Adds** schema-change alerting (warn when a crawler returns 0 results or empty fields)
- **Adds** SQLite statistics logging for unique notices

## Disclaimer

This project is provided **for personal and educational use only**, with no
warranty of any kind. It crawls and submits forms on third-party real-estate
sites which have their own Terms of Service — many of those ToS prohibit
automated access, scraping, or form submission. **You are solely responsible**
for ensuring your use of this software complies with the ToS of every site you
target, with `robots.txt`, with applicable rate limits, and with any local laws
governing automated processing of personal data (e.g. GDPR Art. 22 around
profiling, the German UWG around automated form submissions). The author(s)
accept no liability for accounts being banned, IPs being blocked, applications
being rejected, or any other consequence of running this tool. **Default to
`auto_apply.dry_run: true` and review each form by hand before going live.**

Site selectors and form structures change frequently; submissions may silently
fail or land in spam queues. Treat auto-apply as a best-effort convenience
layer, not a guarantee.

## Install

```bash
uv sync --dev
cp config.example.yaml config.yaml
# edit config.yaml
python main.py --config config.yaml
```

## Multiple search profiles (shared scraping — recommended)

Each profile is an independent YAML config with its own DB, filters, telegram
and auto-apply. To run several without re-crawling each source per
profile, list them in a top-level `hunter.yaml` (see `hunter.yaml.example`) and
run one process:

```bash
python main.py --hunter hunter.yaml
```

One *lead* scraper crawls the **union** of all profiles' URLs once per cycle
(one shared `schema_monitor.json`), then fans the identical exposes out to each
profile's own filter → notify → apply pipeline. Each profile keeps its own
`database_location` (so "already seen"/notification dedup is independent) and
only sees the sources it lists in its own `urls`, so the crawl covers exactly
those (a leftover `global.urls` is ignored with a warning). Adding a profile is
one line in `hunter.yaml`; the crawl cost stays flat. One failing profile never
sinks the cycle.

Single-profile mode still works for one-off runs:

```bash
python main.py --config profiles/cheap.yaml --once
```

## Running on TrueNAS

Production runs as the TrueNAS SCALE Custom App `flathunter`. The full details
are in [`deploy/README.md`](deploy/README.md).

- **Image/CI:** `.github/workflows/image.yml` builds the `Dockerfile` on every
  push to `main` and pushes `ghcr.io/tuschla/berlin-flat-hunter:latest` plus
  `:<git sha>`. The image holds code only (Python 3.13, Chromium + chromedriver,
  supercronic) and runs as uid:gid `568:568`.
- **App:** `deploy/truenas-app.yaml`, installed via Apps > Discover > Custom App >
  Install via YAML. It has two services from that image: `hunter` (the
  `--hunter` loop) and `watchdog` (supercronic running `deploy/crontab`).
- **Datasets:** `/mnt/nvme0/apps/flathunter/data` → `/data` (state; `/app/data`
  links to it) and `/mnt/nvme0/apps/flathunter/config` (read-only, files `0600`).
- **Config files:** `config/hunter.yaml` → `/app/hunter.yaml` and
  `config/profiles/*.yaml` → `/app/profiles/`. Every `database_location` points
  at `/app/data/...`.
- **Schedule:**

  | When | Job | Stale after |
  |---|---|---|
  | always | `hunter`: `python main.py --hunter /app/hunter.yaml` (sleeps `loop.sleeping_time` between crawls) | – |
  | every 15 min | `watchdog`: `scripts/crawler_watchdog.py` (hunter stalled; crawler down → ntfy) | 1 h |
  | every 30 min | `run-job.sh --check-stale` | – |

- **Alerts:**
  - Crawler-down alerts come from the hunter itself: `global.telegram` plus
    every profile's bot (`global.monitoring.alert_via_notifiers: true`).
  - The watchdog alerts `global.telegram` and ntfy (`NTFY_URL`) when the hunter
    has stalled, and copies crawler-down to ntfy only (never Telegram again).
  - `deploy/run-job.sh` POSTs to ntfy when a cron job fails, and once per
    episode when a job has not succeeded within its max age.
  - The `watchdog` healthcheck goes unhealthy while a job is stale.
- **Update:** push to `main`, wait for the workflow, then Apps > flathunter >
  Update (or Redeploy); `pull_policy: always` fetches the new `:latest`.
- **Logs/shell:** Apps > flathunter > Logs, and Apps > flathunter > Shell.


## Status (verified live 2026-04-28)

| Crawler | Crawl | Auto-apply |
|---|---|---|
| Gewobag | ✅ ~36 listings | ⚠ form is an `app.wohnungshelden.de` Angular/NG-ZORRO SPA loaded via `iframe#contact-iframe`. We extract the iframe `src` and drive the SPA standalone, filling 9 fields (firstName/lastName/email/phoneNumber/message + street/houseNumber/zipCode/city). **Live submit is blocked by reCAPTCHA** — the applicator detects and aborts cleanly, logging the reason. `dry_run: true` works end-to-end (verified live 2026-04-28). |
| WBM | ✅ 7 listings | ⚠ powermail form requires WBS info + privacy checkbox; we fill basic contact fields only. **Use `dry_run: true` first.** |
| Howoge | ✅ via TYPO3 JSON endpoint | ✅ HTTP-only POST to the TYPO3 ext:HowRealestate `doiSend` form (firstName/lastName/email + harvested cHash + `__trustedProperties`). Triggers double-opt-in email; user clicks the confirmation link to finalise. No JS / captcha as of 2026-05. |
| Gesobau | ✅ follows the `tx_solr[page]` pagination | not implemented |
| degewo | ✅ crawled in the page's own Warmmiete sort (the default sort's pages overlap) | not implemented |
| Kleinanzeigen | ✅ 27 listings | ⚠ requires login; selectors are best-effort. **Use `dry_run: true` first.** |
| ImmobilienScout24 | ✅ via flathunter | ❌ not implemented (heavy anti-bot) |
| Immowelt | ❌ DataDome 403 — flathunter HTTP-only crawler can't bypass currently | ❌ not implemented |
| WG-Gesucht, Idealista, Subito, Immobiliare, VrmImmo | via flathunter | ❌ not implemented |

When auto-apply silently degrades (selectors stale on a site after a redesign),
the AutoApplicator counts consecutive URL-matched failures per site and pushes
a `[APPLICATOR ALERT] <Site>: N consecutive auto-apply failures …` message
through the same notifier chain used for schema alerts (enable
`monitoring.alert_via_notifiers: true` to receive it). Counter resets on the
next successful submission; cooldown is 1h to avoid spam.

**Auto-apply is best-effort.** Form structures change regularly. Always run with
`auto_apply.dry_run: true` first to verify selectors match — it fills forms
without clicking submit. Once verified, flip to `dry_run: false`.

## Crawler coverage

flathunter's built-in crawlers (already supported, just configure URLs):

| Site | Coverage | Stats |
|---|---|---|
| ImmobilienScout24 | DE #1 — biggest portal | ✅ |
| Immowelt / Immonet | DE #2 — major portal | ✅ |
| WG-Gesucht | Shared flats / rooms #1 | ✅ |
| Kleinanzeigen | Classifieds, private landlords | ✅ + auto-apply |
| Idealista | Spain/Italy (limited DE) | ✅ |
| Subito / Immobiliare | Italy | ✅ |
| VrmImmo | Regional (Mainz/Wiesbaden) | ✅ |

This repo adds:

| Site | Coverage | Stats | Auto-apply |
|---|---|---|---|
| Gewobag | Berlin public housing — biggest landlord | ✅ | ⚠ live submit blocked by reCAPTCHA |
| WBM | Berlin Mitte public housing | ✅ | ✅ |
| Howoge | Berlin east public housing | ✅ | ✅ |
| Gesobau | Berlin north public housing | ✅ |  |
| degewo | Berlin public housing | ✅ |  |

Every crawler of this repo derives a listing's id from its detail URL
(`berlin_flat_hunter/crawlers/common.py: listing_id`), because the sites' own
uids change for the same listing. `price` is the warm rent (Warmmiete /
Gesamtmiete) wherever the site gives one, or empty when the site shows no
amount ("Auf Anfrage"); `price_cold` / `price_warm` hold the split where known.

### Other Berlin public housing (not yet supported)

These sites are JS-rendered SPAs and would need Selenium-based crawlers
(non-trivial to add):

- **Stadt und Land, WBG 1892, Berlinovo** — smaller Berlin public housing

PRs welcome.

## Statistics

In shared-scrape mode (`--hunter`) every listing of every crawl is logged once
to `stats.db` next to `global.database_location` (`global.statistics`, on by
default; `db_path` overrides the location): first and last sighting, plus
title/address/rooms/size/price/price_cold/price_warm. A field that was empty at
first sight (or a price without an amount) is filled in on a later sighting.

Query from Python:

```python
from berlin_flat_hunter.stats import StatsLogger
stats = StatsLogger("stats.db")
stats.count_total()                     # total unique notices
stats.count_by_crawler()                # {"Gewobag": 42, "Wbm": 18, ...}
stats.count_since(time.time() - 86400)  # last 24h
stats.recent(limit=20)                  # newest 20 notices
```

## Schema-change alerting

The `SchemaMonitor` watches for two failure modes per crawler:

1. **Empty crawls** — N consecutive runs with 0 results (default 3) → alert
2. **Field misses** — >X% of results missing `title`/`url`/`address`/`price` (default 50%) → alert

Alerts log at ERROR level (visible in any log setup). State persists in
`schema_monitor.json` next to the DB. Cool-down per crawler: 1 h
(`alert_cooldown_seconds`), doubling with each alert of the same failure streak
up to 24 h (`alert_cooldown_cap_seconds`); only a healthy crawl ends a streak.

### Routing alerts to Telegram / Slack / Apprise

Set `monitoring.alert_via_notifiers: true`. Alerts (schema alerts in
single-profile mode, auto-apply alerts) are then pushed through the notifier
names under `monitoring.alert_notifiers`, or under `notifiers:` when that is
unset — re-using your existing Telegram bot, Apprise URLs, Slack webhook, etc.
`notifiers:` alone decides where *listings* go: with `notifiers: []` a profile
gets no per-listing messages but still gets alerts through `alert_notifiers`.

In shared-scrape mode crawler-down alerts come from `hunter.yaml`'s
`global.monitoring.alert_via_notifiers: true` and go to `global.telegram` plus
every profile's own Telegram bot (each chat once).

```yaml
notifiers: [apprise, telegram]
apprise:
  - "ntfys://ntfy.sh/your-topic"
telegram:
  bot_token: "..."
  receiver_ids: [123456789]
monitoring:
  alert_via_notifiers: true
```

## Polygon area filter

Define a polygon in lat/lon to drop listings outside a target area:

```yaml
search_area:
  polygon:
    - [52.540, 13.350]
    - [52.540, 13.450]
    - [52.490, 13.450]
    - [52.490, 13.350]
```

Geocoding via OpenStreetMap Nominatim (free, no key). Cache + rate-limit
(1.1s between requests) per process.

## Ollama integration

Local LLM filter (no API key needed). Two independent gates:

- `ollama.enabled: true` — filters notifications (drop listings the model says NO)
- `auto_apply.ollama_gate: true` — gates application submission separately

Both default to fail-open (keep/apply) when Ollama is unreachable.

## Type checking & tests

```bash
.venv/bin/python -m pyright
uv run --frozen pytest -q     # also run by CI before every image build
```

## License

GNU Affero General Public License v3.0 — same as upstream
[flathunter](https://github.com/flathunters/flathunter), which this project
links against. See [LICENSE](LICENSE) for the full text.
