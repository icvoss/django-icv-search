"""Tests for icv_search.checks.check_base_model_is_abstract (icv_search.E005).

Unit-level, patching icv_search.conf.get_base_model directly rather than
driving a genuinely misconfigured ICV_BASE_MODEL through django.setup(). A
concrete target can fail at model class-definition time before E005 runs;
patching after a successful django.setup() exercises the check's own logic
for all three misconfiguration shapes it must catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def restore_get_base_model():
    """Patch icv_search.conf.get_base_model and restore it afterwards."""
    import icv_search.conf as conf_module

    original = conf_module.get_base_model
    yield conf_module
    conf_module.get_base_model = original


class TestDefaultConfigurationHasNoError:
    def test_default_base_model_produces_no_e005(self):
        from icv_search.checks import _base_model_not_abstract_error

        assert _base_model_not_abstract_error() is None

    def test_run_checks_reports_no_e005_by_default(self):
        from django.core.checks import run_checks

        errors = run_checks(databases=[])
        e005 = [e for e in errors if e.id == "icv_search.E005"]

        assert not e005


class TestGetBaseModelDefault:
    def test_compat_base_model_is_abstract(self):
        from icv_search._compat import BaseModel

        assert BaseModel._meta.abstract is True

    def test_compat_base_model_has_uuid_and_timestamps(self):
        import uuid

        from django.db import models

        from icv_search._compat import BaseModel

        id_field = BaseModel._meta.get_field("id")
        assert isinstance(id_field, models.UUIDField)
        assert id_field.primary_key is True
        assert callable(id_field.default)
        assert isinstance(id_field.default(), uuid.UUID)
        assert id_field.editable is False

        created_field = BaseModel._meta.get_field("created_at")
        assert created_field.auto_now_add is True
        assert created_field.db_index is True

        updated_field = BaseModel._meta.get_field("updated_at")
        assert updated_field.auto_now is True

    def test_compat_base_model_has_no_default_ordering(self):
        """Bundled base must not impose a default ordering (ADR-066)."""
        from icv_search._compat import BaseModel

        assert BaseModel._meta.ordering == []

    def test_get_base_model_resolves_to_compat_by_default(self):
        from icv_search._compat import BaseModel
        from icv_search.conf import get_base_model

        assert get_base_model() is BaseModel

    def test_package_models_inherit_resolved_base(self):
        from icv_search._compat import BaseModel
        from icv_search.models import IndexSyncLog, SearchIndex

        assert issubclass(SearchIndex, BaseModel)
        assert issubclass(IndexSyncLog, BaseModel)


class TestE005FiresForConcreteModel:
    def test_error_returned_for_a_concrete_model(self, restore_get_base_model):
        from django.contrib.auth.models import User

        restore_get_base_model.get_base_model = lambda: User

        from icv_search.checks import _base_model_not_abstract_error

        error = _base_model_not_abstract_error()

        assert error is not None
        assert error.id == "icv_search.E005"
        assert "CONCRETE" in error.msg
        assert error.obj is User

    def test_error_is_a_django_error_not_a_warning(self, restore_get_base_model):
        from django.contrib.auth.models import User
        from django.core.checks import Error as DjangoError

        restore_get_base_model.get_base_model = lambda: User

        from icv_search.checks import _base_model_not_abstract_error

        error = _base_model_not_abstract_error()

        assert isinstance(error, DjangoError)


class TestE005FiresForNonModelClass:
    def test_error_returned_for_a_plain_class(self, restore_get_base_model):
        restore_get_base_model.get_base_model = lambda: object

        from icv_search.checks import _base_model_not_abstract_error

        error = _base_model_not_abstract_error()

        assert error is not None
        assert error.id == "icv_search.E005"
        assert "not a Django model class" in error.msg


class TestE005FiresForUnimportablePath:
    def test_error_returned_when_resolution_raises(self, restore_get_base_model):
        def _raise():
            raise ImportError("no module named nonexistent")

        restore_get_base_model.get_base_model = _raise

        from icv_search.checks import _base_model_not_abstract_error

        error = _base_model_not_abstract_error()

        assert error is not None
        assert error.id == "icv_search.E005"
        assert "does not resolve to an importable class" in error.msg


class TestCheckEntryPointWiring:
    def test_check_function_returns_a_list(self, restore_get_base_model):
        from django.contrib.auth.models import User

        restore_get_base_model.get_base_model = lambda: User

        from icv_search.checks import check_base_model_is_abstract

        result = check_base_model_is_abstract(app_configs=None)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].id == "icv_search.E005"

    def test_check_function_returns_empty_list_when_valid(self):
        from icv_search.checks import check_base_model_is_abstract

        assert check_base_model_is_abstract(app_configs=None) == []

    def test_check_is_registered_with_django(self):
        """IcvSearchConfig.ready() must register the check."""
        from django.core.checks.registry import registry

        from icv_search.checks import check_base_model_is_abstract

        assert check_base_model_is_abstract in registry.registered_checks

    def test_no_try_except_icv_core_in_base_source(self):
        base_path = Path(__file__).resolve().parents[1] / "src" / "icv_search" / "models" / "base.py"
        source = base_path.read_text()
        assert "from icv_core.models import BaseModel" not in source
        assert "get_base_model" in source
