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

Each profile is an independent YAML config with its own DB, stats, filters,
telegram and auto-apply. To run several without re-crawling each source per
profile, list them in a top-level `hunter.yaml` (see `hunter.yaml.example`) and
run one process:

```bash
python main.py --hunter hunter.yaml
```

One *lead* scraper crawls the **union** of all profiles' URLs once per cycle
(one shared `schema_monitor.json`), then fans the identical exposes out to each
profile's own filter → notify → apply pipeline. Each profile keeps its own
`database_location` (so "already seen"/notification dedup is independent). Adding
a profile is one line in `hunter.yaml`; the crawl cost stays flat. One failing
profile never sinks the cycle.

Single-profile mode still works for one-off runs:

```bash
python main.py --config profiles/cheap.yaml --once
```

## Docker

```bash
docker build -t berlin-flat-hunter .
docker run -d --name bfh \
    -v "$PWD/config.yaml:/config/config.yaml:ro" \
    -v "$PWD/data:/data" \
    berlin-flat-hunter
```

The image bundles Chromium + chromedriver so Kleinanzeigen crawling and all
Selenium-based auto-apply work out of the box.

### Hands-off auto-apply via persistent profile + noVNC

Sites with reCAPTCHA / bot detection (Gewobag → wohnungshelden, Kleinanzeigen)
treat warm Chrome profiles much more leniently than fresh headless instances.
The `runtime-vnc` Dockerfile target runs Chrome **headed** inside Xvfb, exposes
a noVNC web client on port 6080 for first-time login + occasional CAPTCHA
solving, and persists the Chrome profile dir on a volume so cookies/login
state survive container restarts.

```bash
# Build the VNC-flavored image
docker build --target runtime-vnc -t berlin-flat-hunter:vnc .

# Or via compose (the `vnc` service in docker-compose.yml is preconfigured)
docker compose up -d vnc
```

**First-time login workflow:**

1. Open `http://localhost:6080/vnc.html` in any browser → you'll see a Chrome
   window inside the container.
2. Visit each site you'll auto-apply on (Gewobag listing, Kleinanzeigen, …),
   log in, solve any CAPTCHA prompts. Browse a few real pages so the session
   looks human-warmed.
3. Cookies + LocalStorage land in `./data/vnc/chrome-profile/`.
4. The hunter loop is already running in the same container; subsequent
   `apply()` calls reuse your warm profile. Most sites stop challenging you.

**When a CAPTCHA still appears:** the existing `[MANUAL APPLY]` notifier ping
fires (telegram et al). Tap it → open the noVNC tab → solve in 5 seconds → the
next cycle picks up.

**Limits:** sessions expire after weeks; you'll occasionally re-login. The
container needs `shm_size: 2gb` to stop Chrome OOMing on `/dev/shm`. Keep
port 6080 bound to localhost (the compose file does this) — don't expose it
to the LAN unless you set a noVNC password.

### Multi-profile via docker-compose

`docker-compose.yml` ships with two example services (`cheap`, `family`).
Each mounts its own `profiles/<name>.yaml` and `data/<name>/`. Make sure each
YAML has `database_location: /data/db.sqlite` so files land in the per-profile
volume.

```bash
mkdir -p profiles data/cheap data/family
cp config.example.yaml profiles/cheap.yaml   # edit for cheap rentals
cp config.example.yaml profiles/family.yaml  # edit for family flats
docker compose up -d
```


## Status (verified live 2026-04-28)

| Crawler | Crawl | Auto-apply |
|---|---|---|
| Gewobag | ✅ ~36 listings | ⚠ form is an `app.wohnungshelden.de` Angular/NG-ZORRO SPA loaded via `iframe#contact-iframe`. We extract the iframe `src` and drive the SPA standalone, filling 9 fields (firstName/lastName/email/phoneNumber/message + street/houseNumber/zipCode/city). **Live submit is blocked by reCAPTCHA** — the applicator detects and aborts cleanly, logging the reason. `dry_run: true` works end-to-end (verified live 2026-04-28). |
| WBM | ✅ 7 listings | ⚠ powermail form requires WBS info + privacy checkbox; we fill basic contact fields only. **Use `dry_run: true` first.** |
| Howoge | ✅ via TYPO3 JSON endpoint | ✅ HTTP-only POST to the TYPO3 ext:HowRealestate `doiSend` form (firstName/lastName/email + harvested cHash + `__trustedProperties`). Triggers double-opt-in email; user clicks the confirmation link to finalise. No JS / captcha as of 2026-05. |
| Gesobau | ✅ 6 listings | not implemented |
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
| Gewobag | Berlin public housing — biggest landlord | ✅ | ✅ |
| WBM | Berlin Mitte public housing | ✅ | ✅ |
| Howoge | Berlin east public housing | ✅ | ✅ |
| Gesobau | Berlin north public housing | ✅ |  |

### Other Berlin public housing (not yet supported)

These sites are JS-rendered SPAs and would need Selenium-based crawlers
(non-trivial to add):

- **Degewo** — biggest Berlin public landlord besides Gewobag
- **Stadt und Land, WBG 1892, Berlinovo** — smaller Berlin public housing

PRs welcome.

## Statistics

When `statistics.enabled: true` in config, every unique notice (deduped by id)
is logged to a SQLite DB with timestamps. Works for **every crawler** (both
flathunter built-ins and ours) since the `StatsProcessor` runs in the pipeline
after dedup and filtering.

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
`schema_monitor.json` next to the DB. Cool-down: 1 hour between repeat alerts
per crawler.

### Routing alerts to Telegram / Slack / Apprise

Set `monitoring.alert_via_notifiers: true`. Schema alerts are then pushed
through whichever notifier names appear under `notifiers:` in your config —
re-using your existing Telegram bot, Apprise URLs, Slack webhook, etc.

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
.venv/bin/python -m pyright   # 0 errors
.venv/bin/python -m pytest    # 132+ tests
```

## License

GNU Affero General Public License v3.0 — same as upstream
[flathunter](https://github.com/flathunters/flathunter), which this project
links against. See [LICENSE](LICENSE) for the full text.
