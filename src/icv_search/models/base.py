"""Resolved abstract model base for icv-search (ADR-052).

Every concrete model in this package inherits from ``BaseModel`` as returned
by ``icv_search.conf.get_base_model()``. The default is the bundled
``icv_search._compat.BaseModel``; a stack consumer sets ``ICV_BASE_MODEL``
(or ``ICV_SEARCH_BASE_MODEL``) once. There is no guarded ``icv_core`` import.
"""

from __future__ import annotations

from icv_search.conf import get_base_model

# Resolved at class-definition time from ICV_SEARCH_BASE_MODEL / ICV_BASE_MODEL
# / the bundled icv_search._compat.BaseModel default, never imported from
# icv_core (ADR-052).
BaseModel = get_base_model()

__all__ = ["BaseModel"]
