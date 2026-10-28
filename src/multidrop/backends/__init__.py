"""Protocol backends."""

from __future__ import annotations

from multidrop.config import Config
from multidrop.core import Backend, TransferError

PROTOCOLS = ("localsend", "quickshare", "airdrop")


def create_backends(cfg: Config, via: str | None = None) -> list[Backend]:
    """The enabled backends (only `via` if given), not started yet."""
    out: list[Backend] = []
    if via in (None, "localsend") and cfg.localsend.enabled:
        from multidrop.backends.localsend import LocalSendBackend
        out.append(LocalSendBackend(cfg))
    if via in (None, "quickshare") and cfg.quickshare.enabled:
        from multidrop.backends.quickshare import QuickShareBackend
        out.append(QuickShareBackend(cfg))
    if via in (None, "airdrop") and cfg.airdrop.enabled:
        from multidrop.backends.airdrop import AirDropBackend
        out.append(AirDropBackend(cfg))
    if not out:
        raise TransferError("no protocol enabled")
    return out
