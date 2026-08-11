"""Base model for icv-search — uses icv-core BaseModel when available, otherwise provides a local equivalent."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

try:
    from icv_core.models import BaseModel
except ImportError:

    class BaseModel(models.Model):  # type: ignore[no-redef]
        """Standalone base model when icv-core is not installed.

        Provides the same UUID primary key and timestamp fields as
        ``icv_core.models.BaseModel`` so icv-search can be used without
        django-icv-core as a dependency.
        """

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
