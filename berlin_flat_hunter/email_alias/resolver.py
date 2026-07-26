"""AliasResolver — the set of email addresses to apply with, per landlord.

Wraps the ``email_alias`` profile config + the per-profile ``Store`` + the
addy.io client so the applicator can ask a single question: *"which addresses
should I submit this application under?"* and get back a list (>= 1).

Behaviour:

* Disabled (no ``provider: addy`` / no API key): returns the explicitly chosen
  addresses for the source (``provider_emails[source]``) or, failing that, the
  applicant's real email — i.e. the classic one-application-per-listing.
* Enabled (addy): additionally mints (and caches in the Store) one forwarding
  alias for the configured *granularity* bucket — one per landlord (default),
  one per listing, or one fixed alias for everything — and applies under that
  alias too. All aliases forward to the real inbox, which the IMAP confirmer
  still scans, so the double-opt-in loop keeps working.

Config (profile YAML ``email_alias:``)::

    email_alias:
      provider: addy                 # "none" | "addy"
      addy_api_key: "..."
      addy_base_url: https://app.addy.io
      addy_domain: ""                # blank => account default
      addy_format: random_words
      granularity: source            # source | listing | fixed
      provider_emails:               # optional explicit picks per landlord
        howoge: ["me+howoge@example.com"]
      extra_real_emails: []          # other real inboxes you own
"""
from __future__ import annotations

from flathunter.logging import logger

from berlin_flat_hunter.email_alias.addy import AddyClient, AddyError

_GRANULARITIES = ("source", "listing", "fixed")


class AliasResolver:
    def __init__(self, cfg: dict, store, user_id: str, *, timeout: float = 20.0):
        self.cfg = cfg or {}
        self.store = store
        self.user_id = user_id
        self.timeout = timeout
        self.provider = str(self.cfg.get("provider", "none")).lower()
        self.api_key = str(self.cfg.get("addy_api_key", "") or "").strip()
        gran = str(self.cfg.get("granularity", "source")).lower()
        self.granularity = gran if gran in _GRANULARITIES else "source"

    @property
    def enabled(self) -> bool:
        return self.provider == "addy" and bool(self.api_key)

    # ------------------------------------------------------------------
    def _chosen(self, source: str) -> list[str]:
        """Explicitly selected addresses for this landlord (deduped, order-kept)."""
        raw = (self.cfg.get("provider_emails") or {}).get(source, [])
        if isinstance(raw, str):
            raw = [raw]
        seen: set[str] = set()
        return [e for e in raw if e and not (e in seen or seen.add(e))]

    def _scope_key(self, source: str, listing_key: str) -> str:
        if self.granularity == "listing":
            return f"listing:{listing_key}"
        if self.granularity == "fixed":
            return "fixed"
        return f"source:{source}"

    def _alias_for(self, source: str, listing_key: str) -> str:
        """Cached or freshly-minted addy alias for the granularity bucket."""
        scope = self._scope_key(source, listing_key)
        cached = self.store.get_alias(self.user_id, scope) if self.store else None
        if cached:
            return cached
        try:
            client = AddyClient(self.api_key, self.cfg.get("addy_base_url") or "https://app.addy.io",
                                timeout=self.timeout)
            rec = client.create_alias(
                description=f"flathunt {scope}",
                domain=str(self.cfg.get("addy_domain", "") or ""),
                fmt=str(self.cfg.get("addy_format", "") or ""),
            )
        except AddyError as exc:
            logger.warning("addy.io alias minting failed for %s (%s): %s", source, scope, exc)
            return ""
        alias = str(rec.get("email", "") or "")
        if alias and self.store:
            self.store.save_alias(self.user_id, scope, alias, str(rec.get("id", "") or ""))
        return alias

    def emails_for(self, source: str, real_email: str, listing_key: str) -> list[str]:
        """The addresses to submit this application under (always >= 1 when a
        real email exists).

        Explicit ``provider_emails`` for the landlord win outright — those are a
        deliberate list (e.g. a pool of pre-created aliases), so we don't tack an
        extra freshly-minted alias onto them. Only when nothing is pre-selected
        do we mint one addy alias for the granularity bucket."""
        chosen = self._chosen(source)
        if chosen:
            return chosen
        if self.enabled:
            alias = self._alias_for(source, listing_key)
            if alias:
                return [alias]
        return [real_email] if real_email else []
