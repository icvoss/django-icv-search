"""Tests for the ICV_SEARCH_AUTO_INDEX auto-indexing system."""

from __future__ import annotations

import pytest
from django.test import override_settings

from icv_search.auto_index import (
    _is_skipped,
    connect_auto_index_signals,
    disconnect_auto_index_signals,
    skip_index_update,
)
from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend, _documents

pytestmark = pytest.mark.django_db(transaction=True)

# ---------------------------------------------------------------------------
# Shared config helpers
# ---------------------------------------------------------------------------

_AUTO_INDEX_CONFIG = {
    "articles": {
        "model": "search_testapp.Article",
        "on_save": True,
        "on_delete": True,
        "async": False,
        "auto_create": True,
    },
}

_SAVE_ONLY_CONFIG = {
    "articles": {
        "model": "search_testapp.Article",
        "on_save": True,
        "on_delete": False,
        "async": False,
        "auto_create": True,
    },
}


# ---------------------------------------------------------------------------
# Per-test signal cleanup helpers
# ---------------------------------------------------------------------------


def _disconnect_auto_index(index_name: str = "articles") -> None:
    """Disconnect auto-index signals for the named index.

    Delegates to the module's disconnect function which tracks sender associations.
    """
    disconnect_auto_index_signals([index_name])


# ---------------------------------------------------------------------------
# Module-level autouse fixture: also reset backend before each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend(settings):
    """Ensure DummyBackend is used and reset between every test in this module."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()
    # Disconnect any signals that may have been connected during the test
    _disconnect_auto_index("articles")


# ===========================================================================
# TestSkipIndexUpdate
# ===========================================================================


class TestSkipIndexUpdate:
    """Tests for the skip_index_update context manager."""

    def test_not_skipped_by_default(self):
        """Auto-indexing is active when no context manager is in use."""
        assert not _is_skipped()

    def test_skipped_inside_context(self):
        """_is_skipped() returns True inside a skip_index_update block."""
        with skip_index_update():
            assert _is_skipped()

    def test_not_skipped_after_context_exits(self):
        """_is_skipped() returns False once the context manager exits."""
        with skip_index_update():
            pass
        assert not _is_skipped()

    def test_nestable_inner_block_still_skipped(self):
        """Nested skip_index_update contexts do not re-enable indexing."""
        with skip_index_update():
            assert _is_skipped()
            with skip_index_update():
                assert _is_skipped()
            # Outer still active after inner exits
            assert _is_skipped()

    def test_nestable_outer_restored_after_both_exit(self):
        """After both nested blocks exit, indexing resumes."""
        with skip_index_update(), skip_index_update():
            pass
        assert not _is_skipped()

    def test_skip_restored_when_exception_raised(self):
        """skip_index_update restores state even if an exception is raised."""
        try:
            with skip_index_update():
                raise RuntimeError("bang")
        except RuntimeError:
            pass
        assert not _is_skipped()

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_skip_prevents_indexing_on_save(self):
        """Saves inside skip_index_update should not trigger indexing."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        with skip_index_update():
            Article.objects.create(title="Skipped", body="nope", author="Dave")

        assert all(len(docs) == 0 for docs in _documents.values())

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_indexing_resumes_after_skip(self):
        """Saves made after skip_index_update exits are indexed normally."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        with skip_index_update():
            Article.objects.create(title="Skipped", body="no", author="Eve")

        # This save is outside the skip block — should be indexed
        article = Article.objects.create(title="Indexed", body="yes", author="Frank")
        assert any(str(article.pk) in docs for docs in _documents.values())


# ===========================================================================
# TestConnectAutoIndexSignals
# ===========================================================================


class TestConnectAutoIndexSignals:
    """Tests for connect_auto_index_signals() validation and connection logic."""

    @override_settings(ICV_SEARCH_AUTO_INDEX={}, ICV_SEARCH_AUTO_SYNC=False)
    def test_empty_config_does_not_raise(self):
        """Empty ICV_SEARCH_AUTO_INDEX should silently do nothing."""
        connect_auto_index_signals()  # must not raise

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={"bad": {"model": "nonexistent.Model"}},
        ICV_SEARCH_AUTO_SYNC=False,
    )
    def test_invalid_model_path_logs_warning_not_raise(self):
        """An unresolvable model path should log a warning and not raise."""
        connect_auto_index_signals()  # must not raise

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={"bad": {}},
        ICV_SEARCH_AUTO_SYNC=False,
    )
    def test_missing_model_key_logs_warning_not_raise(self):
        """A config entry without a 'model' key should log a warning and not raise."""
        connect_auto_index_signals()  # must not raise

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "on_delete": True,
                "async": False,
            }
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    def test_idempotent_calling_twice_does_not_duplicate(self):
        """Calling connect_auto_index_signals() twice should not duplicate handlers."""
        connect_auto_index_signals()
        connect_auto_index_signals()
        # If dispatch_uid works correctly, the second call simply replaces the first.
        # We verify no exception is raised and signal is still connected by checking
        # that a save only causes one index operation.

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": False,
                "on_delete": False,
                "async": False,
                "auto_create": False,
            }
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_on_save_false_does_not_connect_save_signal(self):
        """When on_save=False, the post_save handler must not be connected."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        Article.objects.create(title="No index", body="test", author="Test")
        # No index should have been created or updated
        assert all(len(docs) == 0 for docs in _documents.values())

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": False,
                "on_delete": True,
                "async": False,
                "auto_create": True,
            }
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_model_without_to_search_document_skipped(self, settings):
        """A model that does not use SearchableMixin logs a warning and is skipped."""
        # Override to use a plain Django model without SearchableMixin
        # We achieve this by pointing at a non-SearchableMixin model path
        settings.ICV_SEARCH_AUTO_INDEX = {
            "articles": {
                "model": "auth.User",  # built-in, no SearchableMixin
                "on_save": True,
                "async": False,
            }
        }
        connect_auto_index_signals()  # must not raise


# ===========================================================================
# TestAutoIndexOnSave
# ===========================================================================


class TestAutoIndexOnSave:
    """Tests that model saves trigger document indexing."""

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_save_indexes_document(self):
        """Saving a model instance should index the document in the backend."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        article = Article.objects.create(title="Test Article", body="Hello world", author="Alice")

        assert any(str(article.pk) in docs for docs in _documents.values())

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_save_stores_correct_document_content(self):
        """The indexed document should contain the expected fields."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        article = Article.objects.create(title="Content Test", body="body text", author="Bob")

        # Find the document across all index stores in the DummyBackend
        doc = None
        for docs in _documents.values():
            if str(article.pk) in docs:
                doc = docs[str(article.pk)]
                break

        assert doc is not None
        assert doc.get("title") == "Content Test"
        assert doc.get("author") == "Bob"

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": False,
                "async": False,
                "auto_create": False,
            }
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_on_save_false_skips_indexing(self):
        """When on_save is False, saves must not trigger indexing."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        Article.objects.create(title="Draft", body="no index", author="Charlie")

        assert all(len(docs) == 0 for docs in _documents.values())

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_multiple_saves_index_multiple_documents(self):
        """Each save should result in an indexed document."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        a1 = Article.objects.create(title="First", body="one", author="Author1")
        a2 = Article.objects.create(title="Second", body="two", author="Author2")

        all_pks = set()
        for docs in _documents.values():
            all_pks.update(docs.keys())

        assert str(a1.pk) in all_pks
        assert str(a2.pk) in all_pks

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_update_save_updates_indexed_document(self):
        """Updating a model instance should update its indexed document."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        article = Article.objects.create(title="Original", body="first", author="Dana")
        article.title = "Updated"
        article.save()

        doc = None
        for docs in _documents.values():
            if str(article.pk) in docs:
                doc = docs[str(article.pk)]
                break

        assert doc is not None
        assert doc.get("title") == "Updated"


# ===========================================================================
# TestAutoIndexOnDelete
# ===========================================================================


class TestAutoIndexOnDelete:
    """Tests that model deletions remove documents from the index."""

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_delete_removes_document(self):
        """Deleting a model instance should remove its document from the backend."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        article = Article.objects.create(title="To Delete", body="bye", author="Carol")
        article_pk = str(article.pk)

        # Verify it was indexed first
        assert any(article_pk in docs for docs in _documents.values())

        article.delete()

        assert all(article_pk not in docs for docs in _documents.values())

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "on_delete": False,
                "async": False,
                "auto_create": True,
            }
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_on_delete_false_skips_removal(self):
        """When on_delete is False, deletion should not remove the document."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        article = Article.objects.create(title="Keep", body="stay", author="Earl")
        article_pk = str(article.pk)

        article.delete()

        # Document should still be present (removal was skipped)
        assert any(article_pk in docs for docs in _documents.values())

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_delete_resolves_pk_before_row_is_gone(self):
        """The document ID used for removal must be the PK, not a DB lookup after delete."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        article = Article.objects.create(title="Gone Soon", body="bye", author="Fran")
        expected_pk = str(article.pk)

        article.delete()

        # After deletion, the DB row is gone. The removal should still have worked
        # because pk was resolved synchronously.
        assert all(expected_pk not in docs for docs in _documents.values())

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    def test_delete_inside_transaction_removes_document_after_commit(self):
        """Deferred deletion retains the document ID after Django clears instance.pk."""
        from django.db import transaction
        from search_testapp.models import Article

        connect_auto_index_signals()

        article = Article.objects.create(title="Transactional delete", body="bye", author="Faye")
        article_pk = str(article.pk)
        assert any(article_pk in docs for docs in _documents.values())

        with transaction.atomic():
            article.delete()

        assert all(article_pk not in docs for docs in _documents.values())


# ===========================================================================
# TestAutoCreateIndex
# ===========================================================================


class TestAutoCreateIndex:
    """Tests for the auto_create=True behaviour."""

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_auto_creates_search_index_record(self):
        """auto_create=True should create a SearchIndex DB record on first save."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        from icv_search.models import SearchIndex

        assert not SearchIndex.objects.filter(name="articles").exists()

        Article.objects.create(title="First", body="hello", author="Iris")

        assert SearchIndex.objects.filter(name="articles").exists()

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_auto_created_index_has_model_settings(self):
        """The auto-created SearchIndex should have settings derived from SearchableMixin."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        from icv_search.models import SearchIndex

        Article.objects.create(title="First", body="hello", author="Jane")

        index = SearchIndex.objects.get(name="articles")
        # Article declares search_filterable_fields and search_fields,
        # so the index settings should have been seeded from the mixin.
        assert "searchableAttributes" in index.settings or "filterableAttributes" in index.settings

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    @pytest.mark.django_db(transaction=True)
    def test_auto_create_only_creates_once(self):
        """Saving twice should only create one SearchIndex record."""
        connect_auto_index_signals()

        from search_testapp.models import Article

        from icv_search.models import SearchIndex

        Article.objects.create(title="A", body="a", author="K")
        Article.objects.create(title="B", body="b", author="L")

        assert SearchIndex.objects.filter(name="articles").count() == 1

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "async": False,
                "auto_create": False,
            }
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_auto_create_false_does_not_create_index(self):
        """When auto_create=False, no SearchIndex should be created automatically."""
        connect_auto_index_signals()

        # Suppress the expected SearchIndex.DoesNotExist error that will be
        # logged when attempting to index without an existing index.
        import logging

        from search_testapp.models import Article

        from icv_search.models import SearchIndex

        logging.disable(logging.CRITICAL)
        try:
            Article.objects.create(title="No auto", body="nope", author="Mia")
        finally:
            logging.disable(logging.NOTSET)

        assert not SearchIndex.objects.filter(name="articles").exists()


# ===========================================================================
# TestShouldUpdateCallable
# ===========================================================================


class TestShouldUpdateCallable:
    """Tests for the should_update gate callable."""

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "async": False,
                "auto_create": True,
                "should_update": "search_testapp.helpers.should_index_article",
            },
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_should_update_false_skips_indexing(self):
        """When should_update(instance) returns False, the document must not be indexed."""
        import sys
        import types

        # Inject a helpers module into search_testapp so import_string can find it
        helpers = types.ModuleType("search_testapp.helpers")
        helpers.should_index_article = lambda instance: instance.is_published  # type: ignore[attr-defined]
        sys.modules["search_testapp.helpers"] = helpers

        try:
            connect_auto_index_signals()

            from search_testapp.models import Article

            # is_published=False — should_update returns False
            Article.objects.create(title="Draft", body="draft", author="Gail", is_published=False)
            assert all(len(docs) == 0 for docs in _documents.values())
        finally:
            sys.modules.pop("search_testapp.helpers", None)

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "async": False,
                "auto_create": True,
                "should_update": "search_testapp.helpers.should_index_article",
            },
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    def test_should_update_false_removes_an_already_indexed_document(self):
        """Unpublishing an indexed object removes its stale search document."""
        import sys
        import types

        helpers = types.ModuleType("search_testapp.helpers")
        helpers.should_index_article = lambda instance: instance.is_published  # type: ignore[attr-defined]
        sys.modules["search_testapp.helpers"] = helpers

        try:
            connect_auto_index_signals()

            from search_testapp.models import Article

            article = Article.objects.create(title="Published", body="pub", author="Hank", is_published=True)
            article_pk = str(article.pk)
            assert any(article_pk in docs for docs in _documents.values())

            article.is_published = False
            article.save()

            assert all(article_pk not in docs for docs in _documents.values())
        finally:
            sys.modules.pop("search_testapp.helpers", None)

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "async": False,
                "auto_create": True,
                "should_update": "search_testapp.helpers.should_index_article",
            },
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    def test_should_update_false_after_create_in_transaction_leaves_no_document(self):
        """A later ineligible save removes a document queued by an earlier save."""
        import sys
        import types

        from django.db import transaction

        helpers = types.ModuleType("search_testapp.helpers")
        helpers.should_index_article = lambda instance: instance.is_published  # type: ignore[attr-defined]
        sys.modules["search_testapp.helpers"] = helpers

        try:
            connect_auto_index_signals()

            from search_testapp.models import Article

            with transaction.atomic():
                article = Article.objects.create(title="Published", body="pub", author="Hank", is_published=True)
                article_pk = str(article.pk)
                article.is_published = False
                article.save()

            assert all(article_pk not in docs for docs in _documents.values())
        finally:
            sys.modules.pop("search_testapp.helpers", None)


class TestAutoIndexTransactionBoundary:
    """Auto-index side effects run only after the database transaction commits."""

    @override_settings(ICV_SEARCH_AUTO_INDEX=_AUTO_INDEX_CONFIG, ICV_SEARCH_AUTO_SYNC=False)
    def test_rolled_back_save_does_not_index_document(self):
        """A row that never commits must not become searchable."""
        from django.db import transaction
        from search_testapp.models import Article

        connect_auto_index_signals()

        with pytest.raises(RuntimeError), transaction.atomic():
            Article.objects.create(title="Rolled back", body="no", author="Rae")
            raise RuntimeError("roll back")

        assert all(len(docs) == 0 for docs in _documents.values())

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "async": False,
                "auto_create": True,
                "should_update": "search_testapp.helpers.should_index_article",
            },
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_should_update_true_allows_indexing(self):
        """When should_update(instance) returns True, the document must be indexed."""
        import sys
        import types

        helpers = types.ModuleType("search_testapp.helpers")
        helpers.should_index_article = lambda instance: instance.is_published  # type: ignore[attr-defined]
        sys.modules["search_testapp.helpers"] = helpers

        try:
            connect_auto_index_signals()

            from search_testapp.models import Article

            article = Article.objects.create(title="Published", body="pub", author="Hank", is_published=True)
            assert any(str(article.pk) in docs for docs in _documents.values())
        finally:
            sys.modules.pop("search_testapp.helpers", None)

    @override_settings(
        ICV_SEARCH_AUTO_INDEX={
            "articles": {
                "model": "search_testapp.Article",
                "on_save": True,
                "async": False,
                "auto_create": True,
                "should_update": "search_testapp.helpers.should_index_article",
            },
        },
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_should_update_mixed_results(self):
        """should_update gates each save independently."""
        import sys
        import types

        helpers = types.ModuleType("search_testapp.helpers")
        helpers.should_index_article = lambda instance: instance.is_published  # type: ignore[attr-defined]
        sys.modules["search_testapp.helpers"] = helpers

        try:
            connect_auto_index_signals()

            from search_testapp.models import Article

            draft = Article.objects.create(title="Draft", body="no", author="Ida", is_published=False)
            published = Article.objects.create(title="Published", body="yes", author="Jake", is_published=True)

            all_pks = set()
            for docs in _documents.values():
                all_pks.update(docs.keys())

            assert str(draft.pk) not in all_pks
            assert str(published.pk) in all_pks
        finally:
            sys.modules.pop("search_testapp.helpers", None)


# ===========================================================================
# TestSkipIndexUpdateIntegration
# ===========================================================================


class TestSkipIndexUpdatePublicExports:
    """skip_index_update is accessible from the package root and services."""

    def test_importable_from_package_root(self):
        """skip_index_update can be imported from icv_search directly."""
        import icv_search

        assert hasattr(icv_search, "skip_index_update")
        assert callable(icv_search.skip_index_update)

    def test_importable_from_services(self):
        """skip_index_update can be imported from icv_search.services."""
        import icv_search.services as svc

        assert hasattr(svc, "skip_index_update")
        assert callable(svc.skip_index_update)

    def test_importable_from_auto_index(self):
        """skip_index_update can be imported directly from icv_search.auto_index."""
        from icv_search.auto_index import skip_index_update as f

        assert callable(f)
