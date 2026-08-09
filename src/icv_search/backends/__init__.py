"""Search backend loading and access."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from icv_search.backends.base import BaseSearchBackend

_backend_instance: BaseSearchBackend | None = None


def get_search_backend(*, force_new: bool = False) -> BaseSearchBackend:
    """Return the configured search backend instance.

    The backend is instantiated once per process and cached. Pass
    ``force_new=True`` to create a fresh instance (useful in tests).

    Reads the backend settings directly from ``django.conf.settings`` at
    call time, not from ``icv_search.conf``'s re-exported constants: those
    are evaluated once, at first import, so they never observe a setting
    reassigned afterwards (as pytest-django's ``settings`` fixture does,
    once per test). Baking the value in here would silently keep using
    whatever backend was configured for the first test that ever built one
    in the process, for the rest of the test session.
    """
    global _backend_instance  # noqa: PLW0603

    if _backend_instance is not None and not force_new:
        return _backend_instance

    from django.conf import settings

    backend_path = getattr(settings, "ICV_SEARCH_BACKEND", "icv_search.backends.meilisearch.MeilisearchBackend")
    backend_class = import_string(backend_path)
    _backend_instance = backend_class(
        url=getattr(settings, "ICV_SEARCH_URL", "http://localhost:7700"),
        api_key=getattr(settings, "ICV_SEARCH_API_KEY", ""),
        timeout=getattr(settings, "ICV_SEARCH_TIMEOUT", 30),
        **getattr(settings, "ICV_SEARCH_BACKEND_OPTIONS", {}),
    )
    return _backend_instance


def reset_search_backend() -> None:
    """Clear the cached backend instance. Useful in tests."""
    global _backend_instance  # noqa: PLW0603
    _backend_instance = None
