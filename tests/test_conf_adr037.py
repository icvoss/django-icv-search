"""Tests for the ADR-037 fleet-global settings resolved in conf.py.

``icv_search.conf`` reads settings as module-level constants at import time
(matching the file's existing style), so ``override_settings``/the pytest
``settings`` fixture cannot change an already-imported value. Each test
reloads the module after setting the override, per the pattern already used
in ``test_base_model.py``.
"""

from __future__ import annotations

import importlib

from icv_search import conf


class TestIcvAuthUserModel:
    """ICV_AUTH_USER_MODEL falls back to settings.AUTH_USER_MODEL."""

    def test_falls_back_to_auth_user_model_when_unset(self, settings):
        assert not hasattr(settings, "ICV_AUTH_USER_MODEL")
        importlib.reload(conf)
        try:
            assert conf.ICV_AUTH_USER_MODEL == settings.AUTH_USER_MODEL
        finally:
            importlib.reload(conf)

    def test_honours_explicit_override(self, settings):
        settings.ICV_AUTH_USER_MODEL = "auth.User"
        importlib.reload(conf)
        try:
            assert conf.ICV_AUTH_USER_MODEL == "auth.User"
        finally:
            importlib.reload(conf)


class TestIcvCachesAlias:
    """ICV_CACHES_ALIAS falls back to the literal "default"."""

    def test_falls_back_to_default_when_unset(self, settings):
        assert not hasattr(settings, "ICV_CACHES_ALIAS")
        importlib.reload(conf)
        try:
            assert conf.ICV_CACHES_ALIAS == "default"
        finally:
            importlib.reload(conf)

    def test_honours_explicit_override(self, settings):
        settings.ICV_CACHES_ALIAS = "fleet"
        importlib.reload(conf)
        try:
            assert conf.ICV_CACHES_ALIAS == "fleet"
        finally:
            importlib.reload(conf)


class TestIcvSearchCacheAliasChainsOntoCachesAlias:
    """ICV_SEARCH_CACHE_ALIAS defaults to the resolved ICV_CACHES_ALIAS
    (ADR-037 second amendment, ruling 2), not the literal "default"."""

    def test_defaults_to_resolved_caches_alias(self, settings):
        settings.ICV_CACHES_ALIAS = "fleet"
        assert not hasattr(settings, "ICV_SEARCH_CACHE_ALIAS")
        importlib.reload(conf)
        try:
            assert conf.ICV_SEARCH_CACHE_ALIAS == "fleet"
        finally:
            importlib.reload(conf)

    def test_falls_back_to_the_literal_default_when_neither_is_set(self, settings):
        assert not hasattr(settings, "ICV_CACHES_ALIAS")
        assert not hasattr(settings, "ICV_SEARCH_CACHE_ALIAS")
        importlib.reload(conf)
        try:
            assert conf.ICV_SEARCH_CACHE_ALIAS == "default"
        finally:
            importlib.reload(conf)

    def test_own_override_wins_over_the_fleet_alias(self, settings):
        """A package-scoped ICV_SEARCH_CACHE_ALIAS still overrides the
        chained fleet default, so an existing consumer setting both keeps
        working exactly as before."""
        settings.ICV_CACHES_ALIAS = "fleet"
        settings.ICV_SEARCH_CACHE_ALIAS = "search"
        importlib.reload(conf)
        try:
            assert conf.ICV_SEARCH_CACHE_ALIAS == "search"
        finally:
            importlib.reload(conf)
