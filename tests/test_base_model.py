"""Tests for the ADR-052 settings-resolved BaseModel default."""

from __future__ import annotations

import uuid

from django.db import models


class TestCompatBaseModel:
    """The bundled ``_compat.BaseModel`` is the default that actually runs."""

    def test_models_base_resolves_to_compat_by_default(self):
        from icv_search._compat import BaseModel as CompatBase
        from icv_search.models.base import BaseModel

        assert BaseModel is CompatBase

    def test_compat_has_uuid_primary_key(self):
        from icv_search._compat import BaseModel

        class TestModel(BaseModel):
            name = models.CharField(max_length=100)

            class Meta:
                app_label = "test_base_model_compat"

        id_field = TestModel._meta.get_field("id")
        assert isinstance(id_field, models.UUIDField)
        assert id_field.primary_key is True
        assert callable(id_field.default)
        assert isinstance(id_field.default(), uuid.UUID)
        assert id_field.editable is False

    def test_compat_has_timestamps_with_byte_compat_kwargs(self):
        from icv_search._compat import BaseModel

        class TestModel(BaseModel):
            name = models.CharField(max_length=100)

            class Meta:
                app_label = "test_base_model_compat_ts"

        created_field = TestModel._meta.get_field("created_at")
        assert isinstance(created_field, models.DateTimeField)
        assert created_field.auto_now_add is True
        assert created_field.db_index is True

        updated_field = TestModel._meta.get_field("updated_at")
        assert isinstance(updated_field, models.DateTimeField)
        assert updated_field.auto_now is True

    def test_compat_is_abstract(self):
        from icv_search._compat import BaseModel

        assert BaseModel._meta.abstract is True

    def test_compat_has_no_default_ordering(self):
        """Bundled base must not impose a default ordering (ADR-066)."""
        from icv_search._compat import BaseModel

        assert BaseModel._meta.ordering == []
