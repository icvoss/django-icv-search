"""Root conftest for icv-search tests."""

import pytest


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Ensure the ``pg_trgm`` extension exists in the test database.

    ``CREATE EXTENSION`` cannot run inside a migration for this package because
    ``MIGRATION_MODULES`` disables migrations for ``icv_search`` in tests (the
    test app uses Django's syncdb path instead), and a package-level migration
    would force every consuming project to carry it too. Running it here, once
    the real pytest-django test database exists, works identically whether the
    suite runs locally against a developer's PostgreSQL instance or in CI: it
    is plain Python, not a CI-only step. On SQLite (``DB_ENGINE=sqlite3``) this
    is a no-op, since ``pg_trgm`` is a PostgreSQL-only extension.
    """
    from django.db import connection

    if connection.vendor == "postgresql":
        with django_db_blocker.unblock(), connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    yield


def pytest_configure(config):
    """Ensure search_testapp is in INSTALLED_APPS when running from the root."""
    from django.conf import settings

    if not settings.configured:
        return
    if "search_testapp" not in settings.INSTALLED_APPS:
        settings.INSTALLED_APPS = [*settings.INSTALLED_APPS, "search_testapp"]
    if not hasattr(settings, "MIGRATION_MODULES"):
        settings.MIGRATION_MODULES = {}
    settings.MIGRATION_MODULES.setdefault("search_testapp", None)

    # Remove boundary's TenantMiddleware when running in the sandbox settings.
    # icv-search is tenant-agnostic; the strict-mode tenant middleware blocks
    # all requests without a tenant context, breaking URL-routing tests.
    _tenant_mw = "boundary.middleware.TenantMiddleware"
    if hasattr(settings, "MIDDLEWARE") and _tenant_mw in settings.MIDDLEWARE:
        settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if m != _tenant_mw]


@pytest.fixture(autouse=True)
def _reset_dummy_backend():
    """Reset the DummyBackend between tests."""
    from icv_search.backends import reset_search_backend
    from icv_search.backends.dummy import DummyBackend

    DummyBackend.reset()
    reset_search_backend()
    yield
    DummyBackend.reset()
    reset_search_backend()
