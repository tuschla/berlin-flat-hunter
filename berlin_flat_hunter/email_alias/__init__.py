"""Email-alias clients for minting on-demand forwarding addresses."""

from berlin_flat_hunter.email_alias.addy import (
    ALIAS_FORMATS,
    DEFAULT_BASE_URL,
    AddyClient,
    AddyError,
)

__all__ = ["ALIAS_FORMATS", "DEFAULT_BASE_URL", "AddyClient", "AddyError"]
