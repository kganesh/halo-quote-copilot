"""M5: the one place a token becomes a `Principal`.

Design rule 03 says identity is a token that travels. This module is where the
travelling starts, and it is deliberately the only door: nothing else in the
codebase constructs a `Principal` from anything a caller sent.

**This module does not verify signatures, and that is not an oversight.** The
API Gateway JWT authorizer verifies the token against the Cognito user pool's
JWKS before the request reaches any of our code, and hands us the decoded
claims. Verifying again here would mean either trusting a second, weaker
implementation, or fetching JWKS on the request path for no gain.

What it does instead is refuse to admit claims that do not belong to us. An
authorizer wired to the wrong user pool is a real and quiet failure: the token
is genuinely valid, the signature checks out, and the person it describes is a
stranger. So `issuer` and `audience` are compared when configured, `token_use`
is checked, and `exp` is re-checked because claims can be replayed into a
process the gateway never sat in front of.

Everything here fails closed. A missing tenant, an unknown group, an empty
account list: each of these is an error, not a default. A `Principal` that
defaulted to an empty scope would be denied everywhere and look like a bug; one
that defaulted to a role would be a privilege escalation with a comment next to
it.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from halo.platform.identity import Principal, Role

TENANT_CLAIM = "custom:tenant_id"
ACCOUNTS_CLAIM = "custom:account_ids"
GROUPS_CLAIM = "cognito:groups"

ROLE_BY_GROUP = {
    "halo-sales-manager": Role.SALES_MANAGER,
    "halo-operations": Role.OPERATIONS,
    "halo-seller": Role.SELLER,
}
"""Cognito group -> role. A group with no entry here grants nothing.

The order matters where a user is in several groups: the most privileged group
wins, which is how every directory in the world behaves and what an
administrator will assume when they add someone to a second group.
"""

ROLE_PRECEDENCE = (Role.SALES_MANAGER, Role.OPERATIONS, Role.SELLER)

ACCEPTED_TOKEN_USE = frozenset({"id"})
"""Only the ID token carries the custom attributes this maps from.

An access token from the same pool is signed by the same key and passes every
signature check. It has no `custom:` claims, so admitting one would produce an
`AdmissionError` about a missing tenant and send whoever is debugging it looking
in the wrong place entirely.
"""


class AdmissionError(Exception):
    """The claims cannot become a principal.

    Carries no claim values and never the token. This message reaches logs and,
    in some deployments, the caller. What it can say is which claim was at
    fault, because that is a configuration bug the operator has to fix.
    """


def principal_from_claims(
    claims: Mapping[str, Any],
    *,
    issuer: str | None = None,
    audience: str | None = None,
    now: float | None = None,
) -> Principal:
    """Verified claims from the authorizer, in. A frozen `Principal`, out."""
    if issuer is not None and claims.get("iss") != issuer:
        raise AdmissionError("token issuer is not the configured user pool")

    if audience is not None and audience not in _audiences(claims):
        raise AdmissionError("token audience is not this application")

    token_use = claims.get("token_use")
    if token_use is not None and token_use not in ACCEPTED_TOKEN_USE:
        raise AdmissionError(f"token_use {token_use!r} cannot be admitted; expected an ID token")

    if (expiry := claims.get("exp")) is not None and float(expiry) <= (now or time.time()):
        raise AdmissionError("token has expired")

    user_id = _required(claims, "sub")
    tenant_id = _required(claims, TENANT_CLAIM)

    accounts = _accounts(claims)
    if not accounts:
        raise AdmissionError(f"{ACCOUNTS_CLAIM} is empty; a principal with no scope cannot act")

    return Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=_role(claims),
        account_ids=accounts,
    )


def principal_from_event(event: Mapping[str, Any], **options: Any) -> Principal:
    """The same thing, from an API Gateway HTTP API event.

    The claims live at `requestContext.authorizer.jwt.claims`. An event without
    them did not come through the authorizer, which means the route is
    misconfigured — an open door, not a missing field — so it is refused rather
    than treated as an anonymous caller.
    """
    context = event.get("requestContext", {}).get("authorizer", {})
    claims = context.get("jwt", {}).get("claims")
    if not claims:
        raise AdmissionError("request carries no authorizer claims; the route is not protected")
    return principal_from_claims(claims, **options)


def _required(claims: Mapping[str, Any], name: str) -> str:
    value = str(claims.get(name, "")).strip()
    if not value:
        raise AdmissionError(f"claim {name!r} is missing")
    return value


def _audiences(claims: Mapping[str, Any]) -> set[str]:
    """`aud` on an ID token, `client_id` on an access token, and `aud` may be a list."""
    raw = claims.get("aud") or claims.get("client_id") or []
    values = raw if isinstance(raw, list | tuple) else [raw]
    return {str(value) for value in values}


def _accounts(claims: Mapping[str, Any]) -> tuple[str, ...]:
    """Cognito custom attributes are strings, so a list arrives comma-separated.

    Duplicates are dropped and order is preserved, so the same directory entry
    produces the same principal every time. A list is accepted too, because a
    Lambda authorizer can return real JSON and there is no reason to make it
    flatten a list first.
    """
    raw = claims.get(ACCOUNTS_CLAIM, "")
    parts = raw if isinstance(raw, list | tuple) else str(raw).split(",")
    seen: dict[str, None] = {}
    for part in parts:
        if account := str(part).strip():
            seen.setdefault(account, None)
    return tuple(seen)


def _role(claims: Mapping[str, Any]) -> Role:
    raw = claims.get(GROUPS_CLAIM, "")
    groups = raw if isinstance(raw, list | tuple) else str(raw).split(",")
    granted = {
        ROLE_BY_GROUP[name] for group in groups if (name := str(group).strip()) in ROLE_BY_GROUP
    }
    for role in ROLE_PRECEDENCE:
        if role in granted:
            return role
    raise AdmissionError(f"no group in {GROUPS_CLAIM} maps to a role")
