"""터널 제공자 구현 (cloudflared / ngrok)."""
from __future__ import annotations

from typing import Type

from .base import BaseProvider, Tunnel, ProviderError
from .cloudflared import CloudflaredProvider
from .ngrok import NgrokProvider

PROVIDERS = {
    "cloudflared": CloudflaredProvider,
    "ngrok": NgrokProvider,
}


def get_provider(name: str) -> Type[BaseProvider]:
    try:
        return PROVIDERS[name.lower()]
    except KeyError:
        raise ProviderError(
            "알 수 없는 제공자 %r (사용 가능: %s)" % (name, ", ".join(sorted(PROVIDERS)))
        )


__all__ = ["BaseProvider", "Tunnel", "ProviderError", "PROVIDERS", "get_provider"]
