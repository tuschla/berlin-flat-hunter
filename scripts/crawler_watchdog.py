#!/usr/bin/env python3
"""Crawler watchdog — alert when the hunter has stalled; copy crawler-down to ntfy.

Crawler-down alerts on Telegram come from the hunter itself: its SchemaMonitor
flags a crawler and the hunter sends the alert in-process to
``global.telegram`` and every profile's bot (``global.monitoring``). This
oneshot (run every 15 min by supercronic in the TrueNAS app's ``watchdog``
service, see ``deploy/crontab``) reads the monitor's state in
``<state_dir>/schema_monitor.json`` and covers what the hunter cannot:

* Stall: the hunter rewrites schema_monitor.json every cycle (~6 min). If it is
  older than ``STALL_AFTER`` (env ``BFH_STALL_AFTER``, default 30 min) the
  process is alive but stuck mid-cycle, and per-crawler health is frozen. This
  alerts ``global.telegram`` from hunter.yaml and, when ``NTFY_URL`` is set, the
  ntfy topic. One alert per stall episode, keyed by the frozen mtime.
* Crawler down: ntfy only (the maintainer's channel; Telegram already has the
  hunter's alert). A crawler is down while the monitor's ``consecutive_alerts``
  is >= 1. One alert per down episode, keyed by the crawler's
  ``last_success_ts`` (which only moves on a healthy crawl), so a crawler that
  recovers and breaks again alerts afresh.

An episode is recorded as handled only once at least one channel accepted the
alert, so a send that fails (e.g. the network is down too) is retried on the
next run. Alert-only: diagnosis and fixes are done by a human
(``scripts/probe_url.py`` checks whether this host's IP is blocked).

Run manually:  python scripts/crawler_watchdog.py --dry-run   # print, don't act
               python scripts/crawler_watchdog.py --force     # ignore de-dup
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml

from berlin_flat_hunter.notify import TelegramNotifier

REPO_DIR = os.environ.get("BFH_REPO", "/app")
NTFY_URL = os.environ.get("NTFY_URL", "")
# The hunter rewrites schema_monitor.json at the end of every crawl cycle
# (~6 min apart). A file older than this means the process is alive but no
# cycle is completing (e.g. a crawler stuck in a loop) — per-crawler health
# is frozen then, so the down-crawler check alone can't see it.
STALL_AFTER = int(os.environ.get("BFH_STALL_AFTER", str(30 * 60)))  # seconds
STALL_KEY = "hunter:stall"
WATCHDOG_STATE = os.path.join(REPO_DIR, "data", "watchdog_state.json")

# The Telegram bot delivers to Dezső's chat, not the maintainer's. Every
# Telegram alert carries a note (in Italian) asking Dezső to get in touch with Leon.
DEZSO_NOTE = "📩 Dezső, se stai leggendo questo messaggio, dovresti contattare Leon."

# Shared-scrape deployment: ONE process crawls every source once per cycle, so
# there is ONE schema monitor (not one per profile), and the stall alert goes to
# the shared channel configured in hunter.yaml's `global.telegram`.
HUNTER_YAML = os.path.join(REPO_DIR, "hunter.yaml")


def log(msg: str) -> None:
    print(f"[watchdog {datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}",
          flush=True)


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(REPO_DIR, path)


def load_json(path: str) -> dict:
    try:
        with open(_abs(path)) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def down_crawlers(state: dict) -> list[tuple[str, dict]]:
    """Crawlers the monitor has flagged down (an alert has fired for the streak)."""
    out = []
    for name, health in state.items():
        if not isinstance(health, dict):
            continue
        if int(health.get("consecutive_alerts", 0)) >= 1:
            out.append((name, health))
    return out


def _load_hunter_cfg() -> dict:
    try:
        return yaml.safe_load(open(HUNTER_YAML)) or {}
    except (FileNotFoundError, ValueError):
        return {}


def shared_monitor_path(hunter_cfg: dict | None = None) -> str:
    """Path to the single shared schema_monitor.json (next to the shared
    crawl DB configured in hunter.yaml's global.database_location)."""
    cfg = hunter_cfg if hunter_cfg is not None else _load_hunter_cfg()
    db = (cfg.get("global", {}) or {}).get("database_location", "")
    state_dir = os.path.dirname(db) if db else os.path.join(REPO_DIR, "data")
    return os.path.join(state_dir, "schema_monitor.json")


def last_cycle_ts(monitor_path: str) -> float:
    """When the hunter last completed a crawl cycle (mtime of the shared
    schema_monitor.json, rewritten every cycle); 0.0 if it doesn't exist yet."""
    try:
        return os.path.getmtime(monitor_path)
    except FileNotFoundError:
        return 0.0


def hunter_telegram(hunter_cfg: dict | None = None) -> tuple[str, list[str]]:
    """Shared alert channel (``global.telegram``) from hunter.yaml."""
    cfg = hunter_cfg if hunter_cfg is not None else _load_hunter_cfg()
    tg = (cfg.get("global", {}) or {}).get("telegram", {}) or {}
    token = tg.get("bot_token") or ""
    ids = [str(i) for i in (tg.get("receiver_ids") or [])]
    return token, ids


def send_telegram(token: str, ids: list[str], text: str) -> bool:
    notifier = TelegramNotifier(token, ids)
    if not notifier.enabled:
        log("telegram: no bot_token/receiver_ids configured — skipping send")
        return False
    return notifier.send(text)


def send_ntfy(title: str, text: str) -> bool:
    """POST the alert to the ntfy topic in ``NTFY_URL``; no-op when unset."""
    if not NTFY_URL:
        return False
    try:
        req = urllib.request.Request(NTFY_URL, data=text.encode(),
                                     headers={"Title": title, "Tags": "warning"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (OSError, ValueError) as exc:  # URLError, timeouts, bad URL
        log(f"ntfy send failed: {exc}")
        return False


def alert(token: str, ids: list[str], title: str, text: str) -> bool:
    """Send one alert to Telegram (with the note for Dezső) and to ntfy.
    True when at least one of them accepted it."""
    telegram_ok = send_telegram(token, ids, f"{text}\n\n{DEZSO_NOTE}")
    ntfy_ok = send_ntfy(title, text)
    return telegram_ok or ntfy_ok


def human_ago(ts: float) -> tuple[str, str]:
    if not ts:
        return "never", "?"
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    delta = max(0, int(time.time() - ts))
    h, m = delta // 3600, (delta % 3600) // 60
    ago = f"{h}h {m}m" if h else f"{m}m"
    return when.strftime("%Y-%m-%d %H:%M UTC"), ago


def should_alert(wd_state: dict, key: str, health: dict, force: bool) -> bool:
    """True for a new episode (``last_success_ts`` differs from the one we last
    alerted on) or when forced."""
    if force:
        return True
    return wd_state.get(key, {}).get("handled_success_ts") != health.get("last_success_ts")


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert on a stalled hunter; copy down crawlers to ntfy")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen; do not send alerts")
    ap.add_argument("--force", action="store_true",
                    help="ignore de-dup state and alert for every current problem")
    args = ap.parse_args()

    wd_state = load_json(WATCHDOG_STATE)
    now = time.time()
    any_down = False

    hunter_cfg = _load_hunter_cfg()
    token, ids = hunter_telegram(hunter_cfg)
    monitor_path = shared_monitor_path(hunter_cfg)
    state = load_json(monitor_path)

    last_tick = last_cycle_ts(monitor_path)
    if last_tick and now - last_tick >= STALL_AFTER:
        any_down = True
        when, ago = human_ago(last_tick)
        # One alert per stall episode: the frozen mtime identifies the episode.
        tick = {"last_success_ts": last_tick}
        if not should_alert(wd_state, STALL_KEY, tick, args.force):
            log(f"{STALL_KEY}: no crawl cycle completed for {ago} — already alerted this episode")
        elif args.dry_run:
            log(f"{STALL_KEY}: no crawl cycle completed for {ago} (last {when}); "
                f"DRY-RUN: would alert Telegram {ids or '[]'} and ntfy={bool(NTFY_URL)}")
        else:
            log(f"{STALL_KEY}: no crawl cycle completed for {ago} (last {when}); alerting")
            if alert(token, ids, f"flathunter: hunter stalled for {ago}", (
                    f"🔴 Watchdog: il hunter non completa un ciclo di scansione da {ago} "
                    f"(ultimo: {when}) — probabilmente bloccato. Nessun annuncio viene "
                    f"cercato finché non riparte (TrueNAS: Apps > flathunter > Restart).")):
                wd_state[STALL_KEY] = {"handled_success_ts": last_tick, "alert_ts": now}
            else:
                log(f"{STALL_KEY}: every alert channel failed — retrying next run")

    for crawler, health in down_crawlers(state):
        any_down = True
        key = f"shared:{crawler}"
        if not should_alert(wd_state, key, health, args.force):
            log(f"{key}: down but already alerted this episode — skipping")
            continue
        last_ok, _ = human_ago(float(health.get("last_success_ts", 0)))
        empty = int(health.get("consecutive_empty", 0))
        problem = (f"0 results for {empty} consecutive crawls" if empty
                   else "most results miss critical fields (title/url/address/price)")
        log(f"{key}: down — {problem}, last healthy crawl {last_ok}")

        if not NTFY_URL:
            log(f"{key}: NTFY_URL not set — only the hunter's Telegram alert goes out")
            continue
        if args.dry_run:
            log(f"DRY-RUN: would alert ntfy for {key}")
            continue
        if send_ntfy(f"flathunter: crawler {crawler} down", (
                f"{crawler}: {problem}; last healthy crawl {last_ok}. The hunter has "
                f"alerted Telegram. Logs: TrueNAS > Apps > flathunter > Logs.")):
            wd_state[key] = {"handled_success_ts": health.get("last_success_ts"), "alert_ts": now}
        else:
            log(f"{key}: ntfy send failed — retrying next run")

    if not args.dry_run:
        os.makedirs(os.path.dirname(WATCHDOG_STATE), exist_ok=True)
        with open(WATCHDOG_STATE, "w") as f:
            json.dump(wd_state, f, indent=1)
    if not any_down:
        log("all crawlers healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
