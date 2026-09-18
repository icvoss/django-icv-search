"""Tests for async-versus-sync dispatch of settings sync and auto-indexing (#48).

Covers:
- ``handlers.on_search_index_save`` honouring ``ICV_SEARCH_ASYNC_INDEXING``
  (sync path, Celery ``.delay`` path, Celery-unavailable fallback)
- ``auto_index._index_instance`` honouring per-index ``async`` /
  ``ICV_SEARCH_ASYNC_INDEXING`` (True dispatches ``.delay``, False runs sync)
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest
from django.test.signals import setting_changed

from icv_search import conf
from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend
from icv_search.handlers import on_search_index_save
from icv_search.models import SearchIndex
from icv_search.services import create_index

# Settings that ``handlers`` and ``checks`` read from ``icv_search.conf`` as
# import-time constants. Reload conf whenever they change or revert so a
# settings override cannot poison later tests.
_CONF_DISPATCH_SETTINGS = frozenset(
    {
        "ICV_SEARCH_AUTO_SYNC",
        "ICV_SEARCH_ASYNC_INDEXING",
    }
)


def _reload_conf_on_dispatch_setting(*, setting, **kwargs):
    if setting not in _CONF_DISPATCH_SETTINGS:
        return
    importlib.reload(conf)


@pytest.fixture(autouse=True)
def _conf_reload_and_dummy_backend(settings):
    """Reload conf on AUTO_SYNC / ASYNC_INDEXING changes; use DummyBackend."""
    setting_changed.connect(_reload_conf_on_dispatch_setting)
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    settings.ICV_SEARCH_ASYNC_INDEXING = False
    settings.ICV_SEARCH_DEBOUNCE_SECONDS = 0
    importlib.reload(conf)
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()
    setting_changed.disconnect(_reload_conf_on_dispatch_setting)


class TestHandlersAsyncDispatch:
    """``on_search_index_save`` sync / async / Celery-fallback branches."""

    @pytest.mark.django_db
    def test_sync_path_calls_engine_sync_without_delay(self, settings):
        """With ASYNC_INDEXING off, sync runs in-process and ``.delay`` is unused."""
        # Create under AUTO_SYNC=False so post_save does not run the handler
        # before the patches are in place; then enable and invoke directly.
        index = create_index("products")
        settings.ICV_SEARCH_AUTO_SYNC = True
        settings.ICV_SEARCH_ASYNC_INDEXING = False
        importlib.reload(conf)

        with (
            patch("icv_search.services.indexing._sync_index_to_engine") as mock_sync,
            patch("icv_search.tasks.sync_index_settings.delay") as mock_delay,
        ):
            on_search_index_save(sender=SearchIndex, instance=index, created=False)

            mock_sync.assert_called_once()
            assert mock_sync.call_args[0][0].pk == index.pk
            mock_delay.assert_not_called()

    @pytest.mark.django_db
    def test_async_path_dispatches_via_delay(self, settings):
        """With ASYNC_INDEXING on, settings sync is queued via ``.delay``."""
        index = create_index("products")
        settings.ICV_SEARCH_AUTO_SYNC = True
        settings.ICV_SEARCH_ASYNC_INDEXING = True
        importlib.reload(conf)

        with (
            patch("icv_search.services.indexing._sync_index_to_engine") as mock_sync,
            patch("icv_search.tasks.sync_index_settings.delay") as mock_delay,
        ):
            on_search_index_save(sender=SearchIndex, instance=index, created=False)

            mock_delay.assert_called_once_with(str(index.pk))
            mock_sync.assert_not_called()

    @pytest.mark.django_db
    def test_celery_unavailable_falls_back_to_sync(self, settings):
        """When ``.delay`` raises, the handler falls back to synchronous sync."""
        index = create_index("products")
        settings.ICV_SEARCH_AUTO_SYNC = True
        settings.ICV_SEARCH_ASYNC_INDEXING = True
        importlib.reload(conf)

        with (
            patch("icv_search.services.indexing._sync_index_to_engine") as mock_sync,
            patch(
                "icv_search.tasks.sync_index_settings.delay",
                side_effect=RuntimeError("broker down"),
            ),
        ):
            on_search_index_save(sender=SearchIndex, instance=index, created=False)

            mock_sync.assert_called_once()
            assert mock_sync.call_args[0][0].pk == index.pk


class TestAutoIndexAsyncDispatch:
    """``_index_instance`` honouring async True versus False."""

    @pytest.mark.django_db
    def test_async_true_dispatches_add_documents_delay(self, settings):
        """``config['async']=True`` queues indexing via ``add_documents.delay``."""
        from icv_search.auto_index import _index_instance

        index = create_index("articles")
        instance = MagicMock()
        instance.pk = 1
        instance.to_search_document.return_value = {"id": "1", "title": "Hello"}

        with (
            patch("icv_search.tasks.add_documents.delay") as mock_delay,
            patch("icv_search.services.documents.index_documents") as mock_index,
        ):
            _index_instance(
                instance,
                "articles",
                {"async": True, "auto_create": False},
            )

            mock_delay.assert_called_once_with(str(index.pk), [{"id": "1", "title": "Hello"}])
            mock_index.assert_not_called()

    @pytest.mark.django_db
    def test_async_false_indexes_synchronously_without_delay(self, settings):
        """``config['async']=False`` indexes in-process and never calls ``.delay``."""
        from icv_search.auto_index import _index_instance

        create_index("articles")
        instance = MagicMock()
        instance.pk = 1
        instance.to_search_document.return_value = {"id": "1", "title": "Hello"}

        with (
            patch("icv_search.tasks.add_documents.delay") as mock_delay,
            patch("icv_search.services.documents.index_documents") as mock_index,
        ):
            _index_instance(
                instance,
                "articles",
                {"async": False, "auto_create": False},
            )

            mock_index.assert_called_once()
            mock_delay.assert_not_called()

    @pytest.mark.django_db
    def test_async_none_follows_icv_search_async_indexing_setting(self, settings):
        """When ``config['async']`` is unset, ``ICV_SEARCH_ASYNC_INDEXING`` decides."""
        from icv_search.auto_index import _index_instance

        index = create_index("articles")
        instance = MagicMock()
        instance.pk = 1
        instance.to_search_document.return_value = {"id": "1", "title": "Hello"}

        settings.ICV_SEARCH_ASYNC_INDEXING = True

        with (
            patch("icv_search.tasks.add_documents.delay") as mock_delay,
            patch("icv_search.services.documents.index_documents") as mock_index,
        ):
            _index_instance(
                instance,
                "articles",
                {"auto_create": False},
            )

            mock_delay.assert_called_once_with(str(index.pk), [{"id": "1", "title": "Hello"}])
            mock_index.assert_not_called()
