"""Tests for the ADR-037 fleet-global settings resolved in conf.py.

``icv_search.conf`` reads settings as module-level constants at import time
(matching the file's existing style), so ``override_settings``/the pytest
``settings`` fixture cannot change an already-imported value. Each test
reloads the module after setting the override, per the pattern already used
in ``test_base_model.py``.

Cleanup relies on the ``setting_changed`` signal handler registered in
``conftest.py``, which reloads ``conf`` whenever one of the settings it
resolves reverts (a ``finally: importlib.reload(conf)`` inside a test body
runs too early, while the pytest ``settings`` fixture's override is still
live; see the handler's docstring).
"""

from __future__ import annotations

import importlib

from icv_search import conf


class TestIcvAuthUserModel:
    """ICV_AUTH_USER_MODEL falls back to settings.AUTH_USER_MODEL."""

    def test_falls_back_to_auth_user_model_when_unset(self, settings):
        assert not hasattr(settings, "ICV_AUTH_USER_MODEL")
        importlib.reload(conf)
        assert conf.ICV_AUTH_USER_MODEL == settings.AUTH_USER_MODEL

    def test_honours_explicit_override(self, settings):
        settings.ICV_AUTH_USER_MODEL = "auth.User"
        importlib.reload(conf)
        assert conf.ICV_AUTH_USER_MODEL == "auth.User"


class TestIcvCachesAlias:
    """ICV_CACHES_ALIAS falls back to the literal "default"."""

    def test_falls_back_to_default_when_unset(self, settings):
        assert not hasattr(settings, "ICV_CACHES_ALIAS")
        importlib.reload(conf)
        assert conf.ICV_CACHES_ALIAS == "default"

    def test_honours_explicit_override(self, settings):
        settings.ICV_CACHES_ALIAS = "fleet"
        importlib.reload(conf)
        assert conf.ICV_CACHES_ALIAS == "fleet"


class TestIcvSearchCacheAliasChainsOntoCachesAlias:
    """ICV_SEARCH_CACHE_ALIAS defaults to the resolved ICV_CACHES_ALIAS
    (ADR-037 second amendment, ruling 2), not the literal "default"."""

    def test_defaults_to_resolved_caches_alias(self, settings):
        settings.ICV_CACHES_ALIAS = "fleet"
        assert not hasattr(settings, "ICV_SEARCH_CACHE_ALIAS")
        importlib.reload(conf)
        assert conf.ICV_SEARCH_CACHE_ALIAS == "fleet"

    def test_falls_back_to_the_literal_default_when_neither_is_set(self, settings):
        assert not hasattr(settings, "ICV_CACHES_ALIAS")
        assert not hasattr(settings, "ICV_SEARCH_CACHE_ALIAS")
        importlib.reload(conf)
        assert conf.ICV_SEARCH_CACHE_ALIAS == "default"

    def test_own_override_wins_over_the_fleet_alias(self, settings):
        """A package-scoped ICV_SEARCH_CACHE_ALIAS still overrides the
        chained fleet default, so an existing consumer setting both keeps
        working exactly as before."""
        settings.ICV_CACHES_ALIAS = "fleet"
        settings.ICV_SEARCH_CACHE_ALIAS = "search"
        importlib.reload(conf)
        assert conf.ICV_SEARCH_CACHE_ALIAS == "search"
