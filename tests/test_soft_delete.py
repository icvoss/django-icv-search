"""Tests for soft-delete awareness in SearchableMixin and auto-indexing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.test import override_settings

from icv_search.auto_index import (
    connect_auto_index_signals,
    disconnect_auto_index_signals,
)
from icv_search.backends import reset_search_backend
from icv_search.backends.dummy import DummyBackend, _documents

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend(settings):
    """Ensure DummyBackend is used and reset between every test."""
    settings.ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
    settings.ICV_SEARCH_AUTO_SYNC = False
    reset_search_backend()
    DummyBackend.reset()
    yield
    DummyBackend.reset()
    reset_search_backend()
    disconnect_auto_index_signals()


# ---------------------------------------------------------------------------
# get_search_queryset — is_deleted field
# ---------------------------------------------------------------------------


class TestGetSearchQuerysetIsDeleted:
    """get_search_queryset() excludes is_deleted=True by default."""

    @pytest.mark.django_db
    def test_excludes_deleted_records(self):
        """Records with is_deleted=True should not appear in the queryset."""
        from search_testapp.models import SoftDeleteIsDeletedArticle

        SoftDeleteIsDeletedArticle.objects.create(title="Active", is_deleted=False)
        SoftDeleteIsDeletedArticle.objects.create(title="Deleted", is_deleted=True)

        qs = SoftDeleteIsDeletedArticle.get_search_queryset()
        assert qs.count() == 1
        assert qs.first().title == "Active"

    @pytest.mark.django_db
    def test_includes_non_deleted_records(self):
        """Records with is_deleted=False should appear in the queryset."""
        from search_testapp.models import SoftDeleteIsDeletedArticle

        a1 = SoftDeleteIsDeletedArticle.objects.create(title="First", is_deleted=False)
        a2 = SoftDeleteIsDeletedArticle.objects.create(title="Second", is_deleted=False)

        qs = SoftDeleteIsDeletedArticle.get_search_queryset()
        pks = list(qs.values_list("pk", flat=True))
        assert a1.pk in pks
        assert a2.pk in pks

    @pytest.mark.django_db
    def test_returns_queryset_type(self):
        """Returns a QuerySet, not a list."""
        from django.db.models.query import QuerySet
        from search_testapp.models import SoftDeleteIsDeletedArticle

        assert isinstance(SoftDeleteIsDeletedArticle.get_search_queryset(), QuerySet)


# ---------------------------------------------------------------------------
# get_search_queryset — deleted_at field
# ---------------------------------------------------------------------------


class TestGetSearchQuerysetDeletedAt:
    """get_search_queryset() excludes records with a non-null deleted_at."""

    @pytest.mark.django_db
    def test_excludes_records_with_deleted_at_set(self):
        """Records with a non-null deleted_at should be excluded."""
        from search_testapp.models import SoftDeleteDeletedAtArticle

        SoftDeleteDeletedAtArticle.objects.create(title="Active", deleted_at=None)
        SoftDeleteDeletedAtArticle.objects.create(
            title="Deleted",
            deleted_at=datetime(2024, 1, 1, tzinfo=UTC),
        )

        qs = SoftDeleteDeletedAtArticle.get_search_queryset()
        assert qs.count() == 1
        assert qs.first().title == "Active"

    @pytest.mark.django_db
    def test_includes_records_with_null_deleted_at(self):
        """Records with deleted_at=None should appear in the queryset."""
        from search_testapp.models import SoftDeleteDeletedAtArticle

        a = SoftDeleteDeletedAtArticle.objects.create(title="Active", deleted_at=None)
        qs = SoftDeleteDeletedAtArticle.get_search_queryset()
        assert qs.filter(pk=a.pk).exists()


# ---------------------------------------------------------------------------
# get_search_queryset — opt-out
# ---------------------------------------------------------------------------


class TestGetSearchQuerysetOptOut:
    """search_exclude_soft_deleted=False returns all records."""

    @pytest.mark.django_db
    def test_opt_out_includes_deleted(self):
        """When search_exclude_soft_deleted=False, deleted records are included."""
        from search_testapp.models import OptOutSoftDeleteArticle

        OptOutSoftDeleteArticle.objects.create(title="Active", is_deleted=False)
        OptOutSoftDeleteArticle.objects.create(title="Deleted", is_deleted=True)

        qs = OptOutSoftDeleteArticle.get_search_queryset()
        assert qs.count() == 2

    @pytest.mark.django_db
    def test_model_without_soft_delete_fields(self):
        """A model with neither is_deleted nor deleted_at returns all records."""
        from search_testapp.models import NoSoftDeleteArticle

        NoSoftDeleteArticle.objects.create(title="One")
        NoSoftDeleteArticle.objects.create(title="Two")

        qs = NoSoftDeleteArticle.get_search_queryset()
        assert qs.count() == 2


# ---------------------------------------------------------------------------
# Auto-index: soft-delete triggers removal, not indexing
# ---------------------------------------------------------------------------


_SOFT_DELETE_AUTO_INDEX_CONFIG = {
    "soft_delete_articles": {
        "model": "search_testapp.SoftDeleteIsDeletedArticle",
        "on_save": True,
        "on_delete": True,
        "async": False,
        "auto_create": True,
    },
}


class TestAutoIndexSoftDelete:
    """_handle_post_save removes soft-deleted instances from the index."""

    @override_settings(
        ICV_SEARCH_AUTO_INDEX=_SOFT_DELETE_AUTO_INDEX_CONFIG,
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_saving_with_is_deleted_true_removes_from_index(self):
        """When is_deleted is set to True and save() is called, the document
        must be removed from the search index rather than re-indexed."""
        connect_auto_index_signals()

        from search_testapp.models import SoftDeleteIsDeletedArticle

        # Create and index an active article
        article = SoftDeleteIsDeletedArticle.objects.create(title="Will be deleted", is_deleted=False)
        article_pk = str(article.pk)

        # Verify it was indexed
        assert any(article_pk in docs for docs in _documents.values()), "Article should be indexed after initial save"

        # Soft-delete by setting is_deleted=True and saving
        article.is_deleted = True
        article.save()

        # The document should have been removed from the index
        assert all(article_pk not in docs for docs in _documents.values()), (
            "Soft-deleted article should be removed from the search index"
        )

    @override_settings(
        ICV_SEARCH_AUTO_INDEX=_SOFT_DELETE_AUTO_INDEX_CONFIG,
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db
    def test_creating_with_is_deleted_true_does_not_index(self):
        """Creating a record that is already soft-deleted must not add it to the
        search index."""
        connect_auto_index_signals()

        from search_testapp.models import SoftDeleteIsDeletedArticle

        # Create already-deleted
        article = SoftDeleteIsDeletedArticle.objects.create(title="Born deleted", is_deleted=True)
        article_pk = str(article.pk)

        assert all(article_pk not in docs for docs in _documents.values()), (
            "A record created with is_deleted=True must not be indexed"
        )

    @override_settings(
        ICV_SEARCH_AUTO_INDEX=_SOFT_DELETE_AUTO_INDEX_CONFIG,
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_active_save_still_indexes(self):
        """Saving an active (non-deleted) record must still trigger indexing."""
        connect_auto_index_signals()

        from search_testapp.models import SoftDeleteIsDeletedArticle

        article = SoftDeleteIsDeletedArticle.objects.create(title="Active article", is_deleted=False)
        assert any(str(article.pk) in docs for docs in _documents.values()), "Active article should be indexed on save"


# ---------------------------------------------------------------------------
# Auto-index: deleted_at soft-delete
# ---------------------------------------------------------------------------


_DELETED_AT_AUTO_INDEX_CONFIG = {
    "deleted_at_articles": {
        "model": "search_testapp.SoftDeleteDeletedAtArticle",
        "on_save": True,
        "on_delete": True,
        "async": False,
        "auto_create": True,
    },
}


class TestAutoIndexDeletedAt:
    """Auto-index handles deleted_at-based soft-delete correctly."""

    @override_settings(
        ICV_SEARCH_AUTO_INDEX=_DELETED_AT_AUTO_INDEX_CONFIG,
        ICV_SEARCH_AUTO_SYNC=False,
    )
    @pytest.mark.django_db(transaction=True)
    def test_setting_deleted_at_triggers_removal(self):
        """When deleted_at is set to a non-null value and save() is called, the
        document must be removed from the index."""
        connect_auto_index_signals()

        from search_testapp.models import SoftDeleteDeletedAtArticle

        article = SoftDeleteDeletedAtArticle.objects.create(title="Active", deleted_at=None)
        article_pk = str(article.pk)

        assert any(article_pk in docs for docs in _documents.values()), "Article should be indexed after initial save"

        article.deleted_at = datetime(2024, 6, 1, tzinfo=UTC)
        article.save()

        assert all(article_pk not in docs for docs in _documents.values()), (
            "Article with deleted_at set should be removed from the index"
        )
