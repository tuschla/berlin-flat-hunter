#!/usr/bin/env python3
"""Crawler watchdog — triage a down crawler with a read-only Claude run.

The SchemaMonitor already detects when a crawler stops returning results and
persists per-crawler health to ``<state_dir>/schema_monitor.json``. This
watchdog polls that state (via a systemd timer, every ~15 min), and when a
crawler is flagged *down* it launches a **read-only** headless Claude Code run
to diagnose *why* — dead selector, bot-block, or a legitimately empty site —
and pushes the diagnosis to the same Telegram bot the alerts already use.

Deliberately read-only: the Claude run gets a restricted ``--allowedTools`` set
(no Edit/Write, no arbitrary shell, no bypass), so it investigates and proposes
a one-command fix for a human to run, but never mutates the repo or pushes.
That keeps a person in the loop for every change and sidesteps the
unattended-agent risk of an auto-fixer.

De-dup: one triage per *down episode*. An episode is keyed by the crawler's
``last_success_ts`` (which only advances on a successful crawl), so a crawler
that recovers and breaks again is triaged afresh, while one that stays broken is
re-triaged at most once per ``RETRIGGER_COOLDOWN``.

Run manually:  python scripts/crawler_watchdog.py --dry-run   # print, don't act
               python scripts/crawler_watchdog.py --force     # ignore de-dup
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

REPO_DIR = os.environ.get("BFH_REPO", "/root/berlin-flat-hunter")
CLAUDE_BIN = os.environ.get("BFH_CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_TIMEOUT = int(os.environ.get("BFH_CLAUDE_TIMEOUT", str(20 * 60)))  # seconds
RETRIGGER_COOLDOWN = int(os.environ.get("BFH_RETRIGGER_COOLDOWN", str(24 * 3600)))
WATCHDOG_STATE = os.path.join(REPO_DIR, "data", "watchdog_state.json")

# The Telegram bot delivers to Dezső's chat, not the maintainer's. Every alert
# carries a note (in Italian) asking Dezső to get in touch with Leon.
DEZSO_NOTE = "📩 Dezső, se stai leggendo questo messaggio, dovresti contattare Leon."

# Profiles to watch: display name -> (schema_monitor.json path, profile yaml).
PROFILES = {
    "single": ("data/single/schema_monitor.json", "profiles/single.yaml"),
    "wg": ("data/wg/schema_monitor.json", "profiles/wg.yaml"),
}

# Read-only tool sandbox for the triage run. No Edit/Write/bypass — everything
# not listed is auto-denied in headless mode, so the run can only investigate.
# Deliberately excludes tools that can write to disk: `curl -o` and `sed -n
# 'w …'` would break the non-mutating guarantee, so live pages go through
# WebFetch (no filesystem access) and file viewing through the Read tool.
CLAUDE_ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "WebFetch",
    # Box-side read-only fetch probe: lets the triage tell a real IP block from a
    # headless-browser problem. WebFetch egresses via Anthropic, not this host,
    # so it can't answer "is *our* IP blocked?" — this can. Fixed script, GET +
    # print only, so allowing arbitrary args after it is safe.
    "Bash(.venv/bin/python scripts/probe_url.py:*)",
    "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
    "Bash(git status:*)", "Bash(ls:*)", "Bash(grep:*)", "Bash(rg:*)",
    "Bash(head:*)", "Bash(wc:*)",
]


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


def telegram_creds(profile_path: str) -> tuple[str, list[str]]:
    cfg = yaml.safe_load(open(_abs(profile_path))) or {}
    tg = cfg.get("telegram") or {}
    token = tg.get("bot_token") or ""
    ids = tg.get("receiver_ids") or []
    return token, [str(i) for i in ids]


def profile_urls(profile_path: str) -> list[str]:
    cfg = yaml.safe_load(open(_abs(profile_path))) or {}
    return list(cfg.get("urls") or [])


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


def human_ago(ts: float) -> tuple[str, str]:
    if not ts:
        return "never", "?"
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    delta = max(0, int(time.time() - ts))
    h, m = delta // 3600, (delta % 3600) // 60
    ago = f"{h}h {m}m" if h else f"{m}m"
    return when.strftime("%Y-%m-%d %H:%M UTC"), ago


def build_prompt(profile: str, crawler: str, health: dict, urls: list[str]) -> str:
    last_success, ago = human_ago(float(health.get("last_success_ts", 0)))
    empty = int(health.get("consecutive_empty", 0))
    url_lines = "\n".join(f"  - {u}" for u in urls) or "  (none listed)"
    return f"""You are triaging a DOWN web scraper in the berlin-flat-hunter repo (a flathunter fork). Working dir: {REPO_DIR}.

The crawler **{crawler}** (profile: {profile}) has returned ZERO results for {empty} consecutive crawls. Last successful crawl: {last_success} ({ago} ago). The health monitor has flagged it as down.

Diagnose WHY — READ-ONLY. Do not edit files; you have no write tools. The three likely causes:
1. Dead/changed CSS selector or page structure (site redesign) — the crawler's selectors no longer match the live HTML.
2. Bot-blocking / captcha / IP ban — the site returns a challenge or an empty listing container to datacenter IPs.
3. Legitimately empty — the site genuinely shows no offers right now (not a bug).

Steps:
- Read the crawler source: berlin_flat_hunter/crawlers/{crawler.lower()}.py (follow into its flathunter base class if it subclasses one).
- CRITICAL — is our IP actually blocked? Run the box-side probe (it fetches from THIS host's real egress):
    .venv/bin/python scripts/probe_url.py <the crawler's target URL>
  `HTTP 200` with a listing marker (e.g. srchrslt-adtable) → the site serves US fine: it is NOT an IP block, the problem is the browser/driver path or a selector. A captcha/block marker or a non-200 → a real block. Do NOT infer an IP block from a WebFetch timeout: WebFetch egresses via Anthropic's network, not this host, so it cannot tell you whether our crawler's IP is blocked. Trust the probe over WebFetch for that question.
- Compare the fetched page's structure to the selectors in the code. The profile's target URLs:
{url_lines}
- Decide which cause it is, with concrete evidence.

Output ONLY a concise Telegram-ready report, written IN ITALIAN, in PLAIN TEXT, under 350 words, in exactly this shape (keep the Italian field labels):

🔎 Diagnosi {crawler} ({profile})
Causa: <una riga — selettore / blocco-bot / sito vuoto>
Prove: <1-3 righe brevi>
Soluzione: <l'azione più utile; se è una modifica al codice, indica il selettore/metodo esatto e il nuovo valore; se non è risolvibile via codice, dillo>
Comando: claude "fix the {crawler} crawler: <modifica specifica>"    (ometti questa riga se non serve una modifica al codice)

Write everything in Italian. No preamble, no markdown headers, nothing else. (The `Comando:` line stays in English since it is a command to run verbatim.)"""


def run_claude_triage(prompt: str) -> tuple[bool, str]:
    cmd = [CLAUDE_BIN, "-p", prompt, "--allowedTools", *CLAUDE_ALLOWED_TOOLS]
    try:
        proc = subprocess.run(
            cmd, cwd=REPO_DIR, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"(Claude triage timed out after {CLAUDE_TIMEOUT}s)"
    except OSError as exc:
        return False, f"(could not launch claude: {exc})"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:400]
        return False, out or f"(claude exited {proc.returncode}: {err})"
    return True, out


def should_trigger(wd_state: dict, key: str, health: dict, now: float, force: bool) -> bool:
    if force:
        return True
    prev = wd_state.get(key, {})
    # New down episode: last_success_ts differs from the one we last handled.
    if prev.get("handled_success_ts") != health.get("last_success_ts"):
        return True
    # Same episode, still down: re-nag at most once per cooldown.
    return (now - float(prev.get("trigger_ts", 0))) >= RETRIGGER_COOLDOWN


def main() -> int:
    ap = argparse.ArgumentParser(description="Triage down crawlers via read-only Claude")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen; do not launch Claude or send Telegram")
    ap.add_argument("--force", action="store_true",
                    help="ignore de-dup state and triage every currently-down crawler")
    args = ap.parse_args()

    wd_state = load_json(WATCHDOG_STATE)
    now = time.time()
    any_down = False

    for profile, (state_path, profile_path) in PROFILES.items():
        state = load_json(state_path)
        for crawler, health in down_crawlers(state):
            any_down = True
            key = f"{profile}:{crawler}"
            if not should_trigger(wd_state, key, health, now, args.force):
                log(f"{key}: down but already triaged this episode — skipping")
                continue
            last_success, ago = human_ago(float(health.get("last_success_ts", 0)))
            empty = int(health.get("consecutive_empty", 0))
            token, ids = telegram_creds(profile_path)
            log(f"{key}: down — {empty} empty crawls, last success {ago} ago; running triage")

            if args.dry_run:
                log(f"DRY-RUN: would Telegram {ids or '[]'} and launch:\n"
                    f"  {CLAUDE_BIN} -p <prompt> --allowedTools {' '.join(CLAUDE_ALLOWED_TOOLS)}")
                continue

            header = (f"🔴 Watchdog: il crawler {crawler} ({profile}) sembra non rispondere — "
                      f"{empty} scansioni a vuoto, ultimo successo {ago} fa. "
                      f"Avvio della diagnosi con Claude…")
            send_telegram(token, ids, f"{header}\n\n{DEZSO_NOTE}")
            prompt = build_prompt(profile, crawler, health, profile_urls(profile_path))
            ok, report = run_claude_triage(prompt)
            report = report or "(nessun output dalla diagnosi)"
            prefix = "" if ok else "⚠️ diagnosi incompleta —\n"
            send_telegram(token, ids, f"{prefix}{report[:3400]}\n\n{DEZSO_NOTE}")
            log(f"{key}: triage {'ok' if ok else 'FAILED'}, {len(report)} chars sent")

            wd_state[key] = {"handled_success_ts": health.get("last_success_ts"),
                             "trigger_ts": now, "last_ok": ok}

    if not args.dry_run:
        os.makedirs(os.path.dirname(WATCHDOG_STATE), exist_ok=True)
        with open(WATCHDOG_STATE, "w") as f:
            json.dump(wd_state, f, indent=1)
    if not any_down:
        log("all crawlers healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
