"""Settings Web UI package for J-Quants API key management.

Re-exports the public API so that existing imports from ``settings_ui``
continue to work after the refactor::

    from .settings import register_settings_routes
"""

from .routes import register_settings_routes

__all__ = ["register_settings_routes"]
