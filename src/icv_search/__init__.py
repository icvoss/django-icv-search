"""
icv-search — Pluggable search engine integration for Django.

Provides a backend abstraction layer for search engines, index management,
document indexing, and search query execution.
"""

__version__ = "0.10.0"

default_app_config = "icv_search.apps.IcvSearchConfig"

from icv_search.auto_index import disconnect_auto_index_signals, skip_index_update  # noqa: E402
from icv_search.backends import get_search_backend, reset_search_backend  # noqa: E402
from icv_search.exceptions import (  # noqa: E402
    IcvSearchError,
    IndexNotFoundError,
    SearchBackendError,
    SearchTimeoutError,
)
from icv_search.mixins import SearchableMixin  # noqa: E402
from icv_search.pagination import ICVSearchPage, ICVSearchPaginator  # noqa: E402
from icv_search.query import SearchQuery  # noqa: E402
from icv_search.types import (  # noqa: E402
    IndexStats,
    MerchandisedSearchResult,
    PreprocessedQuery,
    QueryContext,
    SearchResult,
    TaskResult,
)

__all__ = [
    # Backend access
    "get_search_backend",
    "reset_search_backend",
    # Exceptions
    "IcvSearchError",
    "IndexNotFoundError",
    "SearchBackendError",
    "SearchTimeoutError",
    # Model integration
    "SearchableMixin",
    # Pagination
    "ICVSearchPage",
    "ICVSearchPaginator",
    # Response types
    "IndexStats",
    "MerchandisedSearchResult",
    "PreprocessedQuery",
    "QueryContext",
    "SearchResult",
    "TaskResult",
    # Auto-indexing
    "disconnect_auto_index_signals",
    "skip_index_update",
    # Query builder
    "SearchQuery",
]
