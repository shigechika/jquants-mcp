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


def parse_allowed_emails(raw: str) -> list[str]:
    """Parse a comma-separated env-var value into a lowercase email list."""
    if not raw:
        return []
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def is_user_allowed(user_id: str, allowed_emails: list[str]) -> bool:
    """Return True if the user is allowed to use this deployment.

    An empty allowlist means "allow all" (self-host default).
    Comparison is case-insensitive.
    """
    if not allowed_emails:
        return True
    return user_id.lower() in allowed_emails


def unauthorized_message(user_id: str) -> str:
    """Return a user-facing explanation for an allowlist rejection."""
    return (
        f"Access denied for {user_id}. This deployment restricts "
        "access via the JQUANTS_ALLOWED_EMAILS allowlist. "
        "If you want to use jquants-mcp, consider running your own "
        "instance — see the 'Cloud Run deployment' section in the README."
    )
