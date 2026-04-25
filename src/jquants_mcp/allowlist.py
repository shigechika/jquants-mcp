"""Email allowlist for restricting multi-user deployment access.

When ``JQUANTS_ALLOWED_EMAILS`` is set on a deployment, only users whose
OAuth email address appears in the comma-separated list are allowed to
register API keys or call user-scoped tools. An unset or empty value
means "allow any authenticated user", which is the expected default
for self-hosted instances.

The allowlist is orthogonal to authentication: OAuth still decides
*who* the user is; this module decides whether *that* user is allowed
to use *this* deployment.
"""

from __future__ import annotations

from typing import Any


def parse_allowed_emails(raw: str) -> list[str]:
    """Parse a comma-separated env-var value into a lowercase email list."""
    if not raw:
        return []
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def get_user_email(token: Any) -> str | None:
    """Return the verified email from a FastMCP OAuth ``AccessToken``.

    Both the Google and GitHub providers populate ``token.claims["email"]``
    when the corresponding scope was requested at authorization time.
    Falls back to ``None`` for any token that does not carry an email
    claim — typically the static bearer-token mode, which already
    bypasses the allowlist gate via the ``client_id == "bearer"`` check
    upstream of the call sites here.
    """
    claims = getattr(token, "claims", None) or {}
    email = claims.get("email")
    return email.lower() if isinstance(email, str) and email else None


def is_email_allowed(email: str | None, allowed_emails: list[str]) -> bool:
    """Return True if the user's email is allowed for this deployment.

    An empty allowlist means "allow all" (self-host default). A missing
    email with a non-empty allowlist fails closed — we cannot prove the
    user is on the list, so we deny.
    """
    if not allowed_emails:
        return True
    if not email:
        return False
    return email.lower() in allowed_emails


# Backwards-compatible alias. The old signature took ``user_id`` and
# treated it as if it were the email. That was a latent bug because the
# OAuth providers expose ``client_id`` as the upstream-IdP ``sub``
# (numeric for Google / GitHub), not the email. ``is_email_allowed`` is
# the corrected name; keep ``is_user_allowed`` available for older
# callers that already pass an email string in.
is_user_allowed = is_email_allowed


def unauthorized_message(identifier: str) -> str:
    """Return a user-facing explanation for an allowlist rejection.

    ``identifier`` is whatever the caller wants to surface to the user;
    callers typically pass the email when known, or the upstream
    ``user_id`` (sub) when the email claim is missing.
    """
    return (
        f"Access denied for {identifier}. This deployment restricts "
        "access via the JQUANTS_ALLOWED_EMAILS allowlist. "
        "If you want to use jquants-mcp, consider running your own "
        "instance — see the 'Cloud Run deployment' section in the README."
    )
