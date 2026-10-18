# Deploy: TrueNAS SCALE Custom App

The hunter runs as a TrueNAS Custom App (Docker Compose) named `flathunter`.
`deploy/truenas-app.yaml` is the complete compose file, pasted into
Apps > Discover > Custom App > Install via YAML. The image
`ghcr.io/tuschla/berlin-flat-hunter` (`:latest` plus `:<git sha>`) is built by
`.github/workflows/image.yml` on every push to `main`. It contains code only;
config and state come from datasets. (This replaces the old systemd units in an
Incus container.)

## Shared scrape, many profiles: one process

The `hunter` service runs a **single** process (`main.py --hunter /app/hunter.yaml`)
that crawls every source **once per cycle** and fans the results out to every
profile listed in `hunter.yaml`. This replaces the old per-profile services
(`bfh-single` / `bfh-wg`), which each ran a full duplicate crawl of the same
sites.

- `hunter.yaml`: global settings + the list of profiles (see `hunter.yaml.example`).
- `profiles/*.yaml`: one file per profile (unchanged format). Each keeps its own
  urls, filters, telegram, auto-apply, dedup DB and geo settings. The shared
  crawl covers exactly the profiles' urls (`global.urls` is ignored).
- Shared crawl state (incl. the single `schema_monitor.json` the watchdog reads)
  lives next to `global.database_location` in `hunter.yaml`. Per-profile dedup
  DBs live under `data/<profile>/`.

Add a profile = add its YAML to `config/profiles/` and one line under
`profiles:` in `hunter.yaml`; the crawl cost stays flat.

## Layout

| Container path | Host path | Mode |
|---|---|---|
| `/app/hunter.yaml` | `/mnt/nvme0/apps/flathunter/config/hunter.yaml` | ro |
| `/app/profiles/` | `/mnt/nvme0/apps/flathunter/config/profiles/` | ro |
| `/data` (`/app/data` is a symlink to it) | `/mnt/nvme0/apps/flathunter/data` | rw |

Both datasets belong to `568:568` (TrueNAS `apps`), the uid the containers run
as. Config files are `0600` because they hold Telegram tokens, the addy.io key,
IMAP credentials and applicant PII. Every `database_location` must point at
`/app/data/...` (e.g. `/app/data/db.sqlite`, `/app/data/single/db.sqlite`).
`HOME` is `/data/home` (undetected-chromedriver caches its patched driver there).

Services, both from the same image:

- `hunter`: the shared-scrape loop. `mem_limit: 1g`, `shm_size: 1g` (Chrome).
- `watchdog`: `supercronic -passthrough-logs /app/deploy/crontab`, `mem_limit: 256m`.
  It runs every job through `deploy/run-job.sh`, and its healthcheck
  (`run-job.sh --healthy`) goes unhealthy when a job is stale.

## Schedule (`deploy/crontab`, Europe/Berlin)

| When | Job | Stale after |
|---|---|---|
| every 15 min | `watchdog`: `python scripts/crawler_watchdog.py` | 1 h |
| every 30 min | `run-job.sh --check-stale` | – |

## Operate

- Logs: Apps > flathunter > Logs (choose `hunter` or `watchdog`). Everything
  goes to stdout; cron job lines are prefixed `[watchdog]`.
- Shell: Apps > flathunter > Shell. Then, e.g.,
  `python scripts/crawler_watchdog.py --dry-run` or
  `python scripts/probe_url.py <url>`.
- Config change: edit the files in the config dataset, then Apps > flathunter > Restart.
- Update to the newest image: Apps > flathunter > Update (or Redeploy).

## Dry-test before going live (recommended once)

Auto-apply mode is per source via `auto_apply.send_modes` (`off` / `dry_run` /
`live`); the legacy global `auto_apply.dry_run` is the fallback. To do a single
fill-but-don't-submit cycle, set the profile's auto_apply to dry_run and run
from the `hunter` shell:

```bash
python main.py --config /app/profiles/single.yaml --once
# or one shared cycle across all profiles:
python main.py --hunter /app/hunter.yaml --once
```

Stop the app's `hunter` loop first if you don't want duplicate notifications.

## Telegram

- **Per-listing** notifications go out only for profiles that list `telegram`
  under `notifiers:` (`notifiers: []` = no listings, alerts only). They use the
  profile's own `telegram:` config and can be routed **per source** to separate
  bots/chats (`telegram.bots_by_source` / `chats_by_source`). An optional
  heartbeat/log channel (`telegram.log_bot_token` / `log_chat_id`) gets a
  per-cycle summary.
- **Crawler-down** alerts (shared crawl) are sent by the hunter itself when
  `hunter.yaml` sets `global.monitoring.alert_via_notifiers: true`: to
  `global.telegram` and to every profile's own bot, each chat once. Without that
  key they only go to the log.
- **Manual-apply / applicator** alerts use the profile's own
  `monitoring.alert_via_notifiers` + `alert_notifiers`.

## Crawler watchdog (alert-only)

`scripts/crawler_watchdog.py` is a oneshot that cron runs every 15 min:

- It alerts `global.telegram` and, if `NTFY_URL` is set, the ntfy topic when no
  crawl cycle has completed for 30 min (`BFH_STALL_AFTER`, seconds). That means
  the hunter is alive but stuck mid-cycle. One alert per stall.
- When the shared monitor flags a crawler as down, it posts that to ntfy only;
  Telegram already has the hunter's own alert. One alert per *down episode*: a
  crawler that recovers and breaks again alerts again.
- An episode only counts as handled once at least one channel accepted the
  alert; failed sends are retried on the next run.
- De-dup state: `data/watchdog_state.json`. Manual runs: `--dry-run` (print,
  don't send) or `--force` (ignore de-dup).

## One-time data migration (cutover)

Degewo listing ids are now derived from the detail URL like the other sources.
Before the new image's first crawl cycle, with the `hunter` service stopped,
seed the new ids so no Degewo listing is notified again. Run it in the
`watchdog` container (same image, same `/data` mount), e.g. Apps > flathunter >
Shell > watchdog:

```bash
python scripts/migrate_degewo_ids.py            # dry run: prints what would change
python scripts/migrate_degewo_ids.py --apply    # writes /data/*/db.sqlite, state.db, stats.db
```

It is idempotent (a second run reports nothing to change). `--data-dir` defaults
to `/data`.

## Job alerts (`deploy/run-job.sh`)

- A failed job (non-zero exit) POSTs `flathunter: <job> failed (rc=N)` with the
  last 40 output lines to `NTFY_URL`.
- `--check-stale` alerts once per episode when a job has not succeeded within its
  max age (`/data/.status/<job>.ok` / `.max` / `.alerted`).
- A job is never stale before the container has been up for its max age.
