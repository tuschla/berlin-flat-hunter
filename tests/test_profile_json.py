"""profile.json (UserProfile) -> BerlinConfig config mapping."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from berlin_flat_hunter.profile_json import userprofile_to_config  # noqa: E402


BASE = {
    "user_id": "testuser",
    "applicant": {"first_name": "Max", "last_name": "Mustermann",
                  "email": "m@x.de", "phone": "0170", "street": "Musterstr.",
                  "house_number": "1", "zip_code": "10435", "city": "Berlin",
                  "message": "Hallo"},
    "filters": {"min_rooms": 1.0, "max_rooms": 2.0, "min_size_sqm": 40,
                "max_price": None, "exclude_keywords": ["tausch"]},
    "areas": {"polygons": [], "plz": []},
    "sources": {"gewobag": {"enabled": True, "send_mode": "live"},
                "degewo": {"enabled": True, "send_mode": "dry_run"},
                "wbm": {"enabled": False, "send_mode": "off"}},
    "notify": {"telegram_bot_token": "TOK", "telegram_chat_id": "555"},
    "email_alias": {"provider": "addy", "addy_api_key": "",
                    "provider_emails": {"gewobag": ["a@addy.me"]}},
    "email_imap": {"enabled": True, "host": "imap.x.de"},
    "form_answers": {"wbs": "nein"},
    "personalization": {"use_claude": True, "api_key": "SECRET"},
}


def cfg(overrides=None):
    d = dict(BASE)
    if overrides:
        d = {**d, **overrides}
    return userprofile_to_config(d, name="jakob", data_dir="/data")


def test_applicant_and_db_and_urls():
    c = cfg()
    assert c["applicant"]["name"] == "Max Mustermann"
    assert c["applicant"]["postal_code"] == "10435"      # zip_code -> postal_code
    assert c["database_location"] == "/data/jakob/db.sqlite"
    # Only enabled known sources contribute URLs (wbm disabled).
    assert "https://www.gewobag.de/fuer-mietinteressentinnen/mietangebote/wohnung/" in c["urls"]
    assert "https://www.degewo.de/immosuche/" in c["urls"]
    assert not any("wbm.de" in u for u in c["urls"])


def test_filters_mapping():
    c = cfg()
    assert c["filters"]["min_rooms"] == 1.0
    assert c["filters"]["max_rooms"] == 2.0
    assert c["filters"]["min_size"] == 40           # min_size_sqm -> min_size
    assert "max_price" not in c["filters"]          # None dropped
    assert c["filters"]["excluded_titles"] == ["tausch"]


def test_send_modes_and_autoapply():
    c = cfg()
    assert c["auto_apply"]["enabled"] is True
    assert c["auto_apply"]["send_modes"]["gewobag"] == "live"
    assert c["auto_apply"]["send_modes"]["degewo"] == "dry_run"
    assert c["auto_apply"]["send_modes"]["wbm"] == "off"


def test_telegram_and_email_passthrough():
    c = cfg()
    assert c["telegram"]["bot_token"] == "TOK"
    assert c["telegram"]["receiver_ids"] == ["555"]
    assert c["notifiers"] == ["telegram"]
    assert c["email_alias"]["provider_emails"]["gewobag"] == ["a@addy.me"]
    assert c["email_imap"]["host"] == "imap.x.de"
    assert c["form_answers"] == {"wbs": "nein"}


def test_personalization_and_claude_key_are_dropped():
    c = cfg()
    assert "personalization" not in c
    assert "SECRET" not in repr(c)  # the Claude key must never leak into config


def test_top_level_neighborhoods_passthrough():
    c = cfg({"neighborhoods": {"Mitte": [10115, 10119]}})
    assert c["neighborhoods"] == {"Mitte": [10115, 10119]}
    assert "search_area" not in c


def test_areas_plz_becomes_neighborhood_group():
    c = cfg({"areas": {"polygons": [], "plz": [10115, 10247]}})
    assert c["neighborhoods"] == {"AreaOfInterest": ["10115", "10247"]}


def test_polygon_fallback_uses_first_ring():
    ring1 = [[52.5, 13.3], [52.5, 13.4], [52.4, 13.4]]
    ring2 = [[52.6, 13.3], [52.6, 13.4], [52.5, 13.4]]
    c = cfg({"areas": {"polygons": [ring1, ring2], "plz": []}})
    assert c["search_area"]["polygon"] == ring1
    assert "neighborhoods" not in c
