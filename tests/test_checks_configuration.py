"""Tests for ``check_icv_search_configuration`` (icv_search.E001 to E004) (#50).

``icv_search.conf`` resolves backend / URL / timeout as import-time module
constants, so each override here reloads ``conf`` before invoking the check.
A local ``setting_changed`` handler reloads again when the pytest ``settings``
fixture reverts those keys, so a poisoned conf cannot leak into later tests.
"""

from __future__ import annotations

import importlib

import pytest
from django.test.signals import setting_changed

from icv_search import conf
from icv_search.checks import check_icv_search_configuration

_CONF_CHECK_SETTINGS = frozenset(
    {
        "ICV_SEARCH_BACKEND",
        "ICV_SEARCH_URL",
        "ICV_SEARCH_TIMEOUT",
    }
)


def _reload_conf_on_check_setting(*, setting, **kwargs):
    if setting not in _CONF_CHECK_SETTINGS:
        return
    importlib.reload(conf)


@pytest.fixture(autouse=True)
def _reload_conf_around_check_settings():
    setting_changed.connect(_reload_conf_on_check_setting)
    yield
    setting_changed.disconnect(_reload_conf_on_check_setting)


def _error_ids(errors) -> set[str]:
    return {e.id for e in errors}


class TestE001BackendUnset:
    def test_fires_when_backend_empty(self, settings):
        settings.ICV_SEARCH_BACKEND = ""
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E001" in _error_ids(errors)

    def test_does_not_fire_when_backend_set(self, settings):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
        settings.ICV_SEARCH_URL = "http://localhost:7700"
        settings.ICV_SEARCH_TIMEOUT = 30
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E001" not in _error_ids(errors)


class TestE002BackendUnimportable:
    def test_fires_when_backend_cannot_be_imported(self, settings):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.no_such.MissingBackend"
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E002" in _error_ids(errors)
        e002 = next(e for e in errors if e.id == "icv_search.E002")
        assert "cannot be imported" in e002.msg

    def test_does_not_fire_when_backend_imports(self, settings):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E002" not in _error_ids(errors)


class TestE003MeilisearchUrl:
    def test_fires_when_meilisearch_url_missing(self, settings):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
        settings.ICV_SEARCH_URL = ""
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E003" in _error_ids(errors)

    def test_fires_when_meilisearch_url_not_http(self, settings):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
        settings.ICV_SEARCH_URL = "ftp://localhost:7700"
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E003" in _error_ids(errors)

    def test_does_not_fire_when_meilisearch_url_valid(self, settings):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.meilisearch.MeilisearchBackend"
        settings.ICV_SEARCH_URL = "https://search.example.com"
        settings.ICV_SEARCH_TIMEOUT = 30
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E003" not in _error_ids(errors)

    def test_does_not_fire_for_non_meilisearch_backend_with_empty_url(self, settings):
        """E003 is Meilisearch-specific; other backends may omit a URL."""
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
        settings.ICV_SEARCH_URL = ""
        settings.ICV_SEARCH_TIMEOUT = 30
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E003" not in _error_ids(errors)


class TestE004Timeout:
    @pytest.mark.parametrize("bad_timeout", [0, -1, 1.5, "30", None])
    def test_fires_when_timeout_not_positive_int(self, settings, bad_timeout):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
        settings.ICV_SEARCH_TIMEOUT = bad_timeout
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E004" in _error_ids(errors)

    def test_does_not_fire_when_timeout_positive(self, settings):
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
        settings.ICV_SEARCH_TIMEOUT = 30
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)

        assert "icv_search.E004" not in _error_ids(errors)


class TestValidConfiguration:
    def test_default_test_settings_produce_no_e001_to_e004(self, settings):
        """DummyBackend + valid URL/timeout is the suite default; no E001-E004."""
        settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
        settings.ICV_SEARCH_URL = "http://localhost:7700"
        settings.ICV_SEARCH_TIMEOUT = 30
        importlib.reload(conf)

        errors = check_icv_search_configuration(app_configs=None)
        config_ids = {e.id for e in errors if e.id.startswith("icv_search.E00")}

        assert config_ids.isdisjoint({"icv_search.E001", "icv_search.E002", "icv_search.E003", "icv_search.E004"})
