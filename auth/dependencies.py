"""The single dependency every /v1 route depends on.

Order matters: the key is resolved and stashed on `request.state` *before* rate
limiting runs, so a 429 is still attributable to a tenant in the usage log.
"""

from __future__ import annotations

from fastapi import Depends, Request

from auth.api_keys import Principal, require_api_key
from auth.rate_limit import enforce
from auth.usage import TurnUsage


def authenticate(
    request: Request, principal: Principal = Depends(require_api_key)
) -> Principal:
    enforce(request, principal)
    return principal


def turn_usage(request: Request) -> TurnUsage:
    """Per-request token accumulator, published for the metering middleware.

    Created here in the request thread; the graph mutates it from whichever
    thread it happens to run on (see auth/usage.py for why that matters).
    """
    usage = TurnUsage()
    request.state.turn_usage = usage
    return usage
