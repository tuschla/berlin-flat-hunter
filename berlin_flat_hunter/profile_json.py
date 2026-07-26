"""Load a pi-style ``profile.json`` (UserProfile schema) as a Berlin profile.

The orchestrator's profiles are normally flathunter-style YAML, but a profile
can also be given as a ``.json`` file in the richer UserProfile shape used by the
pi flathunt bot (applicant / filters / areas / per-source send_mode / notify /
email_alias / email_imap / form_answers). This module maps that document onto
the config keys BerlinConfig understands, so both formats can sit side by side in
``hunter.yaml``.

Deliberately dropped: the ``personalization`` block (Claude message tailoring is
not used here).

Areas → PLZ: a UserProfile carries polygons and/or a flat ``plz`` list. This repo
filters by named PLZ groups (``neighborhoods:``), which is cheaper than
per-listing geocoding, so the mapping prefers PLZ. A top-level ``neighborhoods``
block in the JSON (name → [codes]) is passed through verbatim; otherwise a flat
``areas.plz`` becomes one group; a bare polygon is used only as a last resort
(first ring).
"""
from __future__ import annotations

import json
import os

from flathunter.logging import logger

# Default search URL per Berlin source, so an enabled source contributes its URL
# to the shared crawl's union even though a UserProfile doesn't list URLs.
SOURCE_URLS = {
    "degewo": "https://www.degewo.de/immosuche/",
    "gewobag": "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/wohnung/",
    "howoge": "https://www.howoge.de/immobiliensuche/wohnungssuche.html",
    "wbm": "https://www.wbm.de/wohnungen-berlin/angebote/",
    "gesobau": "https://www.gesobau.de/mieten/wohnungssuche.html",
}

_SEND_MODES = ("off", "dry_run", "live")


def userprofile_to_config(data: dict, *, name: str, data_dir: str = "data") -> dict:
    """Map a UserProfile dict onto a BerlinConfig config dict."""
    applicant = data.get("applicant", {}) or {}
    filters_in = data.get("filters", {}) or {}
    areas = data.get("areas", {}) or {}
    sources = data.get("sources", {}) or {}
    notify = data.get("notify", {}) or {}

    cfg: dict = {"database_location": os.path.join(data_dir, name, "db.sqlite")}

    # URLs: enabled known sources → their default search URL (deduped by union).
    cfg["urls"] = [SOURCE_URLS[s] for s, p in sources.items()
                   if s in SOURCE_URLS and (p or {}).get("enabled", True)]

    # Applicant (join first+last into the single `name` the applicators split).
    full_name = f"{applicant.get('first_name', '')} {applicant.get('last_name', '')}".strip()
    cfg["applicant"] = {
        "name": full_name,
        "email": applicant.get("email", ""),
        "phone": applicant.get("phone", ""),
        "message": applicant.get("message", ""),
        "street": applicant.get("street", ""),
        "house_number": applicant.get("house_number", ""),
        "postal_code": applicant.get("zip_code", ""),
        "city": applicant.get("city", "Berlin"),
    }

    # Attribute filters → flathunter filter keys.
    f: dict = {}
    _map = {"max_price": "max_price", "min_price": "min_price",
            "min_rooms": "min_rooms", "max_rooms": "max_rooms",
            "min_size_sqm": "min_size", "max_size_sqm": "max_size"}
    for src_key, dst_key in _map.items():
        if filters_in.get(src_key) is not None:
            f[dst_key] = filters_in[src_key]
    # WBS: false = applicant has no WBS → drop WBS-only listings (see WbsFilter).
    if filters_in.get("wbs_required") is not None:
        f["wbs_required"] = bool(filters_in["wbs_required"])
    if f:
        cfg["filters"] = f
    # Junk-listing exclude shield (title+description+address). Carries the user's
    # exclude_keywords and honours use_default_excludes (curated shield, default on).
    excl = [str(k) for k in (filters_in.get("exclude_keywords") or []) if str(k).strip()]
    cfg["exclude"] = {"keywords": excl,
                      "use_defaults": bool(filters_in.get("use_default_excludes", True))}

    # Areas → PLZ (preferred) or polygon (fallback).
    if data.get("neighborhoods"):
        cfg["neighborhoods"] = data["neighborhoods"]
    elif areas.get("plz"):
        cfg["neighborhoods"] = {"AreaOfInterest": [str(p) for p in areas["plz"]]}
    elif areas.get("polygons"):
        polys = areas["polygons"]
        if len(polys) > 1:
            logger.warning("profile_json[%s]: %d polygons given but PLZ filtering is "
                           "preferred; using only the first ring", name, len(polys))
        cfg["search_area"] = {"polygon": polys[0]}

    # Per-source send modes → auto_apply.send_modes (+ enable if any active).
    send_modes = {}
    for s, p in sources.items():
        mode = str((p or {}).get("send_mode", "off")).lower()
        send_modes[s] = mode if mode in _SEND_MODES else "off"
    if any(m in ("dry_run", "live") for m in send_modes.values()):
        cfg["auto_apply"] = {"enabled": True, "send_modes": send_modes}

    # Telegram (default bot + per-source routing + heartbeat).
    tg: dict = {}
    if notify.get("telegram_bot_token"):
        tg["bot_token"] = notify["telegram_bot_token"]
    if notify.get("telegram_chat_id"):
        tg["receiver_ids"] = [notify["telegram_chat_id"]]
    if notify.get("telegram_bot_tokens"):
        tg["bots_by_source"] = dict(notify["telegram_bot_tokens"])
    if notify.get("telegram_chat_ids"):
        tg["chats_by_source"] = {k: (v if isinstance(v, list) else [v])
                                 for k, v in notify["telegram_chat_ids"].items()}
    if notify.get("telegram_log_bot_token"):
        tg["log_bot_token"] = notify["telegram_log_bot_token"]
    if notify.get("telegram_log_chat_id"):
        tg["log_chat_id"] = notify["telegram_log_chat_id"]
    if tg:
        cfg["telegram"] = tg
        cfg["notifiers"] = ["telegram"]

    # Email features pass through unchanged (same key shapes).
    if data.get("email_alias"):
        cfg["email_alias"] = data["email_alias"]
    if data.get("email_imap"):
        cfg["email_imap"] = data["email_imap"]
    if data.get("form_answers"):
        cfg["form_answers"] = data["form_answers"]

    return cfg


def load_profile_json(path: str, *, name: str, data_dir: str = "data") -> dict:
    with open(path, encoding="utf-8") as fh:
        return userprofile_to_config(json.load(fh), name=name, data_dir=data_dir)
