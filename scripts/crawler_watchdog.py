#!/usr/bin/env python3
"""Crawler watchdog — alert when a crawler is down or the hunter has stalled.

The SchemaMonitor already detects when a crawler stops returning results and
persists per-crawler health to ``<state_dir>/schema_monitor.json``. This
oneshot watchdog reads that state (run every 15 min by supercronic in the
TrueNAS app's ``watchdog`` service, see ``deploy/crontab``) and, when a crawler
is flagged *down*, alerts the ``global.telegram`` bot from hunter.yaml and,
when ``NTFY_URL`` is set, the ntfy topic. Alert-only: diagnosis and fixes are
done by a human (``scripts/probe_url.py`` checks whether this host's IP is
blocked).

De-dup: one alert per *down episode*. An episode is keyed by the crawler's
``last_success_ts`` (which only advances on a successful crawl), so a crawler
that recovers and breaks again alerts afresh, while one that stays broken
alerts once.

Stall check: the hunter rewrites schema_monitor.json every cycle (~6 min). If
it is older than ``STALL_AFTER`` (env ``BFH_STALL_AFTER``, default 30 min) the
process is alive but stuck mid-cycle — per-crawler health is frozen then, so
the down-crawler check can't see it. That alerts once per stall episode.

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
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

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
# there is ONE schema monitor (not one per profile) and crawler-down alerts go
# to the shared channel configured in hunter.yaml's `global.telegram`.
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
    if not token or not ids:
        log("telegram: no bot_token/receiver_ids configured — skipping send")
        return False
    ok = True
    for chat_id in ids:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
        }).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=payload),
                                        timeout=20) as resp:
                if resp.status != 200:
                    ok = False
        except urllib.error.URLError as exc:
            log(f"telegram send failed for {chat_id}: {exc}")
            ok = False
    return ok


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


def alert(token: str, ids: list[str], title: str, text: str) -> None:
    """Send one alert to Telegram (with the note for Dezső) and to ntfy."""
    send_telegram(token, ids, f"{text}\n\n{DEZSO_NOTE}")
    send_ntfy(title, text)


def human_ago(ts: float) -> tuple[str, str]:
    if not ts:
        return "never", "?"
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    delta = max(0, int(time.time() - ts))
    h, m = delta // 3600, (delta % 3600) // 60
    ago = f"{h}h {m}m" if h else f"{m}m"
    return when.strftime("%Y-%m-%d %H:%M UTC"), ago


def should_alert(wd_state: dict, key: str, health: dict, force: bool) -> bool:
    """True for a new down episode (``last_success_ts`` differs from the one we
    last alerted on) or when forced."""
    if force:
        return True
    return wd_state.get(key, {}).get("handled_success_ts") != health.get("last_success_ts")


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert on down crawlers and a stalled hunter")
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
            alert(token, ids, f"flathunter: hunter stalled for {ago}", (
                f"🔴 Watchdog: il hunter non completa un ciclo di scansione da {ago} "
                f"(ultimo: {when}) — probabilmente bloccato. Nessun annuncio viene "
                f"cercato finché non riparte (TrueNAS: Apps > flathunter > Restart)."))
            wd_state[STALL_KEY] = {"handled_success_ts": last_tick, "alert_ts": now}

    for crawler, health in down_crawlers(state):
        any_down = True
        key = f"shared:{crawler}"
        if not should_alert(wd_state, key, health, args.force):
            log(f"{key}: down but already alerted this episode — skipping")
            continue
        _, ago = human_ago(float(health.get("last_success_ts", 0)))
        empty = int(health.get("consecutive_empty", 0))
        log(f"{key}: down — {empty} empty crawls, last success {ago} ago; alerting")

        if args.dry_run:
            log(f"DRY-RUN: would alert Telegram {ids or '[]'} and ntfy={bool(NTFY_URL)}")
            continue

        alert(token, ids, f"flathunter: crawler {crawler} down", (
            f"🔴 Watchdog: il crawler {crawler} sembra non rispondere — "
            f"{empty} scansioni a vuoto, ultimo successo {ago} fa. "
            f"Log: TrueNAS > Apps > flathunter > Logs."))
        wd_state[key] = {"handled_success_ts": health.get("last_success_ts"), "alert_ts": now}

    if not args.dry_run:
        os.makedirs(os.path.dirname(WATCHDOG_STATE), exist_ok=True)
        with open(WATCHDOG_STATE, "w") as f:
            json.dump(wd_state, f, indent=1)
    if not any_down:
        log("all crawlers healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
