"""
Minimal Django settings for icv-search tests.

Override ICV_SEARCH_* settings in individual test modules using pytest-django's
``settings`` fixture.

By default the test suite uses PostgreSQL (required for the PostgreSQL backend
tests and the pg_trgm intelligence tests). Set DB_ENGINE=sqlite3 in the
environment to fall back to an in-memory SQLite database when PostgreSQL is
not available.
"""

import getpass
import os

SECRET_KEY = "icv-search-test-secret-key-not-for-production"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "icv_search",
    "search_testapp",
]

# Add icv_core if available
try:
    import icv_core  # noqa: F401

    INSTALLED_APPS.insert(2, "icv_core")
except ImportError:
    pass

if os.environ.get("DB_ENGINE", "").endswith("sqlite3"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "icv_django"),
            "USER": os.environ.get("DB_USER", getpass.getuser()),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

# Use syncdb so pytest-django picks up test models without migrations
MIGRATION_MODULES: dict = {
    "icv_search": None,
    "search_testapp": None,
    "auth": None,
    "contenttypes": None,
}

# Suppress icv_core migrations only when icv_core is installed
try:
    import icv_core  # noqa: F401

    MIGRATION_MODULES["icv_core"] = None
except ImportError:
    pass

# Use DummyBackend for all tests
ICV_SEARCH_BACKEND = "icv_search.backends.dummy.DummyBackend"
ICV_SEARCH_URL = "http://localhost:7700"
ICV_SEARCH_API_KEY = ""
ICV_SEARCH_AUTO_SYNC = False  # Disable auto-sync in tests by default
ICV_SEARCH_ASYNC_INDEXING = False  # Disable async in tests
ICV_SEARCH_INDEX_PREFIX = ""

# Bridge ICV_BASE_MODEL from the environment so an integration environment can
# drive the ADR-052 pairing without a settings change in the package.
if os.environ.get("ICV_BASE_MODEL"):
    ICV_BASE_MODEL = os.environ["ICV_BASE_MODEL"]

# ICV Core
ICV_CORE_AUDIT_ENABLED = False

# Merchandising
ICV_SEARCH_MERCHANDISING_ENABLED = False
ICV_SEARCH_MERCHANDISING_CACHE_TIMEOUT = 0
