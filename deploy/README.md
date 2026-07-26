# Deploy: native systemd (no Docker)

We run the hunter directly on this host via `uv run`, supervised by systemd.
Docker is *not* used here; the `docker-compose.yml` and `Dockerfile` in the repo
root are just an alternative deploy path we don't take.

## Shared scrape, many profiles — one service

`bfh-hunter.service` runs a **single** process (`main.py --hunter hunter.yaml`)
that crawls every source **once per cycle** and fans the results out to every
profile listed in `hunter.yaml`. This replaces the old per-profile services
(`bfh-single` / `bfh-wg`), which each ran a full duplicate crawl of the same
sites.

- `hunter.yaml` — global settings + the list of profiles (see `hunter.yaml.example`).
- `profiles/*.yaml` — one file per profile (unchanged format). Each keeps its own
  filters, telegram, auto-apply, dedup DB, stats and geo settings.
- Shared crawl state (incl. the single `schema_monitor.json` the watchdog reads)
  lives next to `global.database_location` in `hunter.yaml`. Per-profile dedup
  DBs still live under `data/<profile>/`.

Add a profile = add one line under `profiles:` in `hunter.yaml`; the crawl cost
stays flat.

## Layout

- `bfh-hunter.service` — the shared-scrape hunter (all profiles)
- `bfh-watchdog.service` + `bfh-watchdog.timer` — crawler triage (see below)

## One-time install

```bash
# Deps already installed via `uv sync` — re-run if pyproject.toml changes.
cp hunter.yaml.example hunter.yaml   # then edit: global settings + profiles list
sudo install -m 644 deploy/bfh-hunter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bfh-hunter.service
```

## Operate

```bash
sudo journalctl -u bfh-hunter -f            # tail
sudo systemctl status bfh-hunter            # state
sudo systemctl restart bfh-hunter           # after editing hunter.yaml or a profile
sudo systemctl stop bfh-hunter              # stop
```

## Dry-test before going live (recommended once)

Auto-apply mode is per source via `auto_apply.send_modes` (`off` / `dry_run` /
`live`); the legacy global `auto_apply.dry_run` is the fallback. To do a single
fill-but-don't-submit cycle for one profile:

```bash
cd /root/berlin-flat-hunter
# temporarily set the profile's auto_apply to dry_run, then:
/root/.local/bin/uv run python main.py --config profiles/single.yaml --once
# or one shared cycle across all profiles:
/root/.local/bin/uv run python main.py --hunter hunter.yaml --once
```

## Telegram

- **Per-listing** notifications use each profile's own `telegram:` config, and can
  be routed **per source** to separate bots/chats (`telegram.bots_by_source` /
  `chats_by_source`). An optional heartbeat/log channel (`telegram.log_bot_token`
  / `log_chat_id`) gets a per-cycle summary.
- **Crawler-down** alerts (shared crawl) go to `hunter.yaml`'s `global.telegram`
  via `global.monitoring.alert_via_notifiers: true`.

## Crawler watchdog (auto-triage)

When the shared monitor flags a crawler as down (0 results for 3+ cycles), a
timer-driven watchdog launches a **read-only** headless Claude Code run to
diagnose *why* — dead selector, bot-block, or a legitimately empty site — and
pushes the diagnosis plus a one-command fix to the `global.telegram` bot. It
never edits or commits: a human runs the suggested fix. It now reads the single
shared `schema_monitor.json` and keys triage per crawler. See
`scripts/crawler_watchdog.py`.

```bash
sudo install -m 644 deploy/bfh-watchdog.service deploy/bfh-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bfh-watchdog.timer
```

- Runs 5 min after boot, then every 15 min. De-dups to one triage per *down
  episode*; re-triages a still-broken crawler at most once per 24h
  (`BFH_RETRIGGER_COOLDOWN`).
- Manual: `python scripts/crawler_watchdog.py --dry-run` (print, don't act) or
  `--force` (ignore de-dup). Trigger a real run now: `sudo systemctl start bfh-watchdog.service`.
- The Claude run is sandboxed via `--allowedTools` (Read/Grep/Glob/WebFetch +
  read-only Bash) — no Edit/Write/bypass. Tunables via `Environment=` in the
  unit: `BFH_CLAUDE_TIMEOUT`, `BFH_RETRIGGER_COOLDOWN`, `BFH_CLAUDE_BIN`.

## Uninstall

```bash
sudo systemctl disable --now bfh-hunter bfh-watchdog.timer
sudo rm /etc/systemd/system/bfh-hunter.service \
        /etc/systemd/system/bfh-watchdog.service /etc/systemd/system/bfh-watchdog.timer
sudo systemctl daemon-reload
```
