"""Minimal addy.io (formerly AnonAddy) API client.

Lives in ``berlin_flat_hunter.email_alias`` so the hunter can mint email aliases
on demand without pulling extra dependencies — it speaks HTTP via the standard
library (urllib) only. addy.io lets you mint email aliases on demand that forward
to your real inbox, so each application can use a fresh address while the IMAP
reader still scans the one inbox everything lands in.

API docs: https://app.addy.io/docs  (Bearer-token auth)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://app.addy.io"

# Valid alias local-part formats accepted by addy.io.
ALIAS_FORMATS = ("random_words", "uuid", "random_characters", "custom")


class AddyError(Exception):
    """Readable failure from the addy.io API (network, auth, or validation)."""


class AddyClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    # -- low level -------------------------------------------------------- #
    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise AddyError("No addy.io API key configured.")
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            if exc.code in (401, 403):
                raise AddyError(f"addy.io rejected the API key (HTTP {exc.code}).") from exc
            raise AddyError(f"addy.io HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise AddyError(f"Could not reach addy.io at {self.base_url}: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001 - never leak a raw traceback to a route
            raise AddyError(f"addy.io request failed: {exc}") from exc

        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AddyError("addy.io returned a non-JSON response.") from exc

    # -- high level ------------------------------------------------------- #
    def account_details(self) -> dict[str, Any]:
        """GET account details — cheap call used to validate the API key."""
        return self._request("GET", "account-details").get("data", {})

    def domain_options(self) -> dict[str, Any]:
        """Full domain-options response: ``data`` (available domains) plus
        ``defaultAliasDomain`` / ``defaultAliasFormat`` for the account."""
        return self._request("GET", "domain-options")

    def resolve_defaults(self) -> tuple[str, str]:
        """(domain, format) the account actually accepts. Best-effort."""
        try:
            opts = self.domain_options()
        except AddyError:
            return "", "random_characters"
        domains = opts.get("data") or opts.get("sharedDomains") or []
        domain = opts.get("defaultAliasDomain") or (domains[0] if domains else "")
        fmt = opts.get("defaultAliasFormat") or "random_characters"
        if fmt not in ALIAS_FORMATS:
            fmt = "random_characters"
        return domain, fmt

    def list_aliases(self, page_size: int = 100) -> list[dict[str, Any]]:
        resp = self._request("GET", f"aliases?page[size]={int(page_size)}")
        return resp.get("data", []) if isinstance(resp, dict) else []

    def create_alias(
        self,
        description: str = "",
        domain: str = "",
        fmt: str = "",
        local_part: str = "",
        recipient_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new alias and return its record (``email`` holds the address).

        addy.io REQUIRES a domain and a valid format. If they aren't supplied (or
        are invalid for this account), we auto-resolve them from the account's
        domain-options so the caller doesn't have to know either.
        """
        if not domain or not fmt or fmt not in ALIAS_FORMATS:
            d_default, f_default = self.resolve_defaults()
            domain = domain or d_default
            if not fmt or fmt not in ALIAS_FORMATS:
                fmt = f_default
        if not domain:
            raise AddyError("addy.io has no usable alias domain for this account.")
        payload: dict[str, Any] = {"domain": domain, "description": description or "",
                                   "format": fmt}
        if fmt == "custom" and local_part:
            payload["local_part"] = local_part
        if recipient_ids:
            payload["recipient_ids"] = recipient_ids
        return self._request("POST", "aliases", payload).get("data", {})

    # -- convenience ------------------------------------------------------ #
    def test(self) -> tuple[bool, str]:
        """Return (ok, human message). Never raises."""
        try:
            data = self.account_details()
        except AddyError as exc:
            return False, str(exc)
        username = data.get("username") or data.get("id") or "account"
        return True, f"Connected to addy.io as '{username}'."
