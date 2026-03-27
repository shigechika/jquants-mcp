"""Backwards-compatibility shim — implementation moved to settings/ package."""

from .settings import register_settings_routes
from .settings.routes import (
    handle_settings_delete,
    handle_settings_get,
    handle_settings_post,
    handle_settings_verify,
)

__all__ = [
    "register_settings_routes",
    "handle_settings_get",
    "handle_settings_post",
    "handle_settings_delete",
    "handle_settings_verify",
]
