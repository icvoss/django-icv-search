"""icv-search's bundled standalone model base (ADR-052).

BaseModel is the DEFAULT that actually runs, not a fallback: a pure
django.db.models.Model subclass (UUID primary key, auto-managed
timestamps), with no reference of any kind to icv-core. Adopting
icv-core's own BaseModel (or any other package's abstract base) is done
by setting ICV_BASE_MODEL (or the package-specific ICV_SEARCH_BASE_MODEL
override) to its dotted path; see icv_search.conf.get_base_model(), which
resolves the setting and is the only place icv-search reads it. This
module itself never imports or references any other package.

Field kwargs are byte-compatible with the committed migration state and
with icv_core.models.base.BaseModel's deconstructed shape (ADR-052
section 4): id carries verbose_name="ID", created_at carries
db_index=True and verbose_name="created at", and updated_at carries
verbose_name="updated at". This is a binding invariant, not a cosmetic
match: a consumer switching ICV_BASE_MODEL from this default to
icv_core.models.BaseModel must see no makemigrations --check drift on
those fields.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class BaseModel(models.Model):
    """Standalone base model: UUID primary key and auto-managed timestamps."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name=_("ID"),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name=_("created at"),
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_("updated at"),
    )

    class Meta:
        abstract = True


__all__ = ["BaseModel"]
