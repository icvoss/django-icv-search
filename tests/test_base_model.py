"""Tests for base model fallback when icv-core is not installed."""

from __future__ import annotations

import uuid

from django.db import models


class TestBaseModelFallback:
    """Test the standalone BaseModel when icv-core is not installed."""

    def test_uses_icv_core_base_model_when_available(self):
        """Should import from icv-core when available."""
        # In this test environment, icv-core might be available
        # We'll just verify the import works
        from icv_search.models.base import BaseModel

        assert BaseModel is not None

    def test_fallback_base_model_has_uuid_primary_key(self):
        """Fallback BaseModel should use UUID primary key."""
        # Simulate icv-core not being available
        import sys

        original_modules = sys.modules.copy()

        # Remove icv_core from sys.modules to force fallback
        if "icv_core" in sys.modules:
            del sys.modules["icv_core"]
        if "icv_core.models" in sys.modules:
            del sys.modules["icv_core.models"]

        try:
            # Force reimport to use fallback
            import importlib

            from icv_search.models import base

            importlib.reload(base)
            BaseModel = base.BaseModel

            # Create a test model using the fallback
            class TestModel(BaseModel):
                name = models.CharField(max_length=100)

                class Meta:
                    app_label = "test"

            # Check it has the expected fields
            assert hasattr(TestModel, "id")
            assert hasattr(TestModel, "created_at")
            assert hasattr(TestModel, "updated_at")

            # Check id field is UUIDField
            id_field = TestModel._meta.get_field("id")
            assert isinstance(id_field, models.UUIDField)
            assert id_field.primary_key is True
            assert callable(id_field.default)
            assert isinstance(id_field.default(), uuid.UUID)
            assert id_field.editable is False

        finally:
            # Restore original modules
            sys.modules.clear()
            sys.modules.update(original_modules)

    def test_fallback_base_model_has_timestamps(self):
        """Fallback BaseModel should have created_at and updated_at."""
        import sys

        original_modules = sys.modules.copy()

        if "icv_core" in sys.modules:
            del sys.modules["icv_core"]
        if "icv_core.models" in sys.modules:
            del sys.modules["icv_core.models"]

        try:
            import importlib

            from icv_search.models import base

            importlib.reload(base)
            BaseModel = base.BaseModel

            class TestModel(BaseModel):
                name = models.CharField(max_length=100)

                class Meta:
                    app_label = "test"

            created_field = TestModel._meta.get_field("created_at")
            assert isinstance(created_field, models.DateTimeField)
            assert created_field.auto_now_add is True
            assert created_field.db_index is True

            updated_field = TestModel._meta.get_field("updated_at")
            assert isinstance(updated_field, models.DateTimeField)
            assert updated_field.auto_now is True

        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)

    def test_fallback_base_model_is_abstract(self):
        """Fallback BaseModel should be abstract."""
        import sys

        original_modules = sys.modules.copy()

        if "icv_core" in sys.modules:
            del sys.modules["icv_core"]
        if "icv_core.models" in sys.modules:
            del sys.modules["icv_core.models"]

        try:
            import importlib

            from icv_search.models import base

            importlib.reload(base)
            BaseModel = base.BaseModel

            assert BaseModel._meta.abstract is True

        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)

    def test_fallback_base_model_has_no_default_ordering(self):
        """Fallback BaseModel should not impose a default ordering (ADR-066)."""
        import sys

        original_modules = sys.modules.copy()

        if "icv_core" in sys.modules:
            del sys.modules["icv_core"]
        if "icv_core.models" in sys.modules:
            del sys.modules["icv_core.models"]

        try:
            import importlib

            from icv_search.models import base

            importlib.reload(base)
            BaseModel = base.BaseModel

            assert BaseModel._meta.ordering == []

        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)
