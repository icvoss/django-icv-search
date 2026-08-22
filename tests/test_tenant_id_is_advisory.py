"""Pin the documented behaviour of the advisory ``tenant_id`` column (#30).

The owner ruling on this issue was to **document honestly** rather than build
isolation. These tests exist so the documentation cannot silently drift from
the code: each one asserts a behaviour the model docstrings and the README's
"Multi-Tenancy" section now state explicitly.

They are deliberately written to pass on the CURRENT behaviour. If a later
change adds real scoping, they fail, and that failure is the prompt to update
the documentation in the same pass rather than leave it describing a package
that no longer exists.
"""

import pytest

from icv_search.models.analytics import SearchQueryLog
from icv_search.models.click_tracking import SearchClick
from icv_search.services import analytics


def test_no_custom_manager_ships_on_the_analytics_models():
    """The docs say "this package ships no custom manager, so nothing filters
    it by default". Assert that, so adding one forces a docs update."""
    assert type(SearchQueryLog.objects) is not None
    assert SearchQueryLog._meta.default_manager_name is None
    assert SearchClick._meta.default_manager_name is None
    # The default Manager, not a scoped subclass.
    from django.db.models import Manager

    assert type(SearchQueryLog._default_manager) is Manager
    assert type(SearchClick._default_manager) is Manager


def test_tenant_id_has_no_foreign_key_or_constraint():
    """The docs say it is "a plain indexed string with no foreign key and no
    constraint, nothing validates it"."""
    for model in (SearchQueryLog, SearchClick):
        field = model._meta.get_field("tenant_id")
        assert not field.is_relation, f"{model.__name__}.tenant_id became a relation"
        assert field.db_index is True
        assert field.default == ""
        # No constraint anywhere references it.
        for constraint in model._meta.constraints:
            assert "tenant_id" not in str(constraint), (
                f"{model.__name__} gained a constraint on tenant_id; update the docs"
            )


@pytest.mark.django_db
def test_analytics_fail_open_when_tenant_id_is_omitted():
    """The load-bearing claim: omitting tenant_id returns EVERY tenant's rows.

    This is the sentence the README and both docstrings now state, and it is
    the reason the column is called advisory rather than an isolation
    boundary. Asserting it here means the claim is verified rather than
    merely written down.
    """
    SearchQueryLog.objects.create(index_name="products", query="alpha", tenant_id="tenant-a")
    SearchQueryLog.objects.create(index_name="products", query="beta", tenant_id="tenant-b")

    unscoped = analytics.get_popular_queries("products", days=30)
    queries = {row["query"] for row in unscoped}
    assert queries == {"alpha", "beta"}, (
        "get_popular_queries no longer fails open; the advisory-column "
        "documentation in the model docstrings and README is now wrong"
    )

    scoped = analytics.get_popular_queries("products", days=30, tenant_id="tenant-a")
    assert {row["query"] for row in scoped} == {"alpha"}


@pytest.mark.django_db
def test_direct_model_access_is_unscoped():
    """The docs say "anything reaching this model directly is unscoped"."""
    SearchQueryLog.objects.create(index_name="products", query="alpha", tenant_id="tenant-a")
    SearchQueryLog.objects.create(index_name="products", query="beta", tenant_id="tenant-b")

    assert SearchQueryLog.objects.count() == 2


def test_the_advisory_wording_is_present_in_both_help_texts():
    """Guard the documentation itself.

    A future edit that "tidies" the help text back to something reassuring
    like "Tenant identifier for multi-tenant setups" reintroduces exactly the
    misleading assurance this issue was about. Pin the load-bearing phrase.
    """
    for model in (SearchQueryLog, SearchClick):
        help_text = str(model._meta.get_field("tenant_id").help_text)
        assert "NOT an isolation boundary" in help_text, f"{model.__name__}.tenant_id help_text lost its caveat"
        assert "FAIL OPEN" in help_text


def test_no_per_subject_erasure_path_ships():
    """The docs say retention is time-based only, with no per-user or
    per-tenant erasure path. Assert the shape of what does ship."""
    import inspect

    signature = inspect.signature(analytics.clear_query_logs)
    assert set(signature.parameters) == {"days_older_than"}, (
        "clear_query_logs gained parameters; if it can now erase by subject or "
        "tenant, the README's erasure caveat must be updated"
    )
