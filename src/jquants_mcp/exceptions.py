"""Exception classes for jquants-dat-mcp."""

from __future__ import annotations


class JQuantsDatMCPError(Exception):
    """Base exception class for jquants-dat-mcp."""

    def to_dict(self) -> dict:
        return {"error": True, "error_type": type(self).__name__, "message": str(self)}


class AuthenticationError(JQuantsDatMCPError):
    """API key is not configured or invalid."""


class RateLimitError(JQuantsDatMCPError):
    """Rate limit exceeded (after exhausting retries)."""

    def __init__(self, message: str = "レート制限に達しました", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class APIError(JQuantsDatMCPError):
    """Error response from J-Quants API."""

    def __init__(self, message: str, status_code: int, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["status_code"] = self.status_code
        if self.body:
            d["body"] = self.body
        return d


class PlanRestrictionError(APIError):
    """Access denied due to plan restriction (HTTP 403)."""


class DecryptionError(JQuantsDatMCPError):
    """Stored API key could not be decrypted (corrupted data or wrong encryption key)."""

    def __init__(self) -> None:
        super().__init__(
            "Failed to decrypt your stored API key. "
            "Please re-register your key with the register_api_key tool."
        )

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["hint"] = "Use the register_api_key tool to re-register your J-Quants API key."
        return d


class UserNotConfiguredError(JQuantsDatMCPError):
    """No J-Quants API key registered for the authenticated user."""

    def __init__(self, user_id: str) -> None:
        # user_id is NOT included in the public message to avoid information disclosure.
        # It is stored as an attribute for server-side logging only.
        super().__init__(
            "No J-Quants API key registered for your account. "
            "Call the register_api_key tool to register your API key."
        )
        self.user_id = user_id

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["hint"] = "Use the register_api_key tool to register your J-Quants API key."
        return d


class InvalidAPIKeyError(JQuantsDatMCPError):
    """User's registered API key is no longer valid."""

    def __init__(self, user_id: str) -> None:
        # user_id is NOT included in the public message to avoid information disclosure.
        # It is stored as an attribute for server-side logging only.
        super().__init__(
            "The registered J-Quants API key is no longer valid. "
            "Please call register_api_key with a new key."
        )
        self.user_id = user_id

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["hint"] = "Use the register_api_key tool to register a new J-Quants API key."
        return d


class ValidationError(JQuantsDatMCPError):
    """Invalid tool parameter value."""

    def __init__(self, param: str, message: str) -> None:
        super().__init__(f"Invalid parameter '{param}': {message}")
        self.param = param

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["param"] = self.param
        return d


def format_api_error(error: JQuantsDatMCPError) -> dict:
    """Format a JQuantsDatMCPError into an MCP-compatible response dict."""
    d = error.to_dict()
    if isinstance(error, PlanRestrictionError):
        d["hint"] = (
            "このエンドポイントは現在のプランでは利用できません。"
            "J-Quants のプラン比較ページで必要なプランをご確認ください。"
        )
    elif isinstance(error, RateLimitError):
        d["hint"] = "しばらく待ってから再試行してください。"
    return d
