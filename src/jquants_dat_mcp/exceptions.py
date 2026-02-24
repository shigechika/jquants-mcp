"""Exception classes for jquants-dat-mcp."""

from __future__ import annotations


class JQuantsDatMCPError(Exception):
    """jquants-dat-mcp の基底例外クラス"""

    def to_dict(self) -> dict:
        return {"error": True, "error_type": type(self).__name__, "message": str(self)}


class AuthenticationError(JQuantsDatMCPError):
    """API キーが未設定または無効"""


class RateLimitError(JQuantsDatMCPError):
    """レート制限超過（リトライ上限到達後）"""

    def __init__(self, message: str = "レート制限に達しました", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class APIError(JQuantsDatMCPError):
    """J-Quants API からのエラーレスポンス"""

    def __init__(self, message: str, status_code: int, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["status_code"] = self.status_code
        return d


class PlanRestrictionError(APIError):
    """プラン制限によるアクセス不可（403）"""


def format_api_error(error: APIError) -> dict:
    """API エラーを MCP レスポンス用の辞書に整形する。"""
    d = error.to_dict()
    if isinstance(error, PlanRestrictionError):
        d["hint"] = (
            "このエンドポイントは現在のプランでは利用できません。"
            "J-Quants のプラン比較ページで必要なプランをご確認ください。"
        )
    elif isinstance(error, RateLimitError):
        d["hint"] = "しばらく待ってから再試行してください。"
    return d
