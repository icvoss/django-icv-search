"""Django system checks for icv_search.

icv_search.E001 to E004: backend / URL / timeout configuration (existing).
icv_search.E005: the resolved model base (ADR-052,
icv_search.conf.get_base_model(), driven by ICV_SEARCH_BASE_MODEL /
ICV_BASE_MODEL) is not an abstract Django model. Per ADR-052, the base is
resolved by dotted-path import of an ABSTRACT class, not apps.get_model,
precisely because an abstract base has no app-label/model-name and cannot
be a swappable concrete model. A misconfigured ICV_BASE_MODEL (pointing
at a concrete model, a non-model class, or something that fails to import
at all) must fail loudly at ``manage.py check`` time, not deep inside the
ORM the first time a search model is defined.
"""

from __future__ import annotations

from django.core.checks import Error, Tags, register
from django.utils.module_loading import import_string

E005_ID = "icv_search.E005"


def _base_model_not_abstract_error() -> Error | None:
    """Return an E005 Error if the resolved base model (ADR-052) is not an
    abstract Django model, else None.

    Covers three failure shapes under one check id: the dotted path fails
    to import at all, imports to something that is not a Django model, or
    imports to a concrete (non-abstract) model.
    """
    import inspect

    from icv_search.conf import get_base_model

    try:
        base = get_base_model()
    except Exception as exc:
        return Error(
            f"ICV_SEARCH_BASE_MODEL/ICV_BASE_MODEL does not resolve to an importable class: {exc}",
            hint="Set ICV_SEARCH_BASE_MODEL or ICV_BASE_MODEL to a dotted path "
            "('module.path.ClassName') naming an abstract Django model, or leave both unset "
            "to use the bundled icv_search._compat.BaseModel default.",
            id=E005_ID,
        )

    if not (inspect.isclass(base) and hasattr(base, "_meta")):
        return Error(
            f"ICV_SEARCH_BASE_MODEL/ICV_BASE_MODEL resolves to {base!r}, which is not a Django model class.",
            hint="Point the setting at a Django model class, not a plain class, function, or instance.",
            id=E005_ID,
        )

    if not base._meta.abstract:
        return Error(
            f"ICV_SEARCH_BASE_MODEL/ICV_BASE_MODEL resolves to {base.__name__!r}, which is a "
            "CONCRETE model (Meta.abstract is not True).",
            hint="The base every icv-search model inherits from must be abstract (it has no "
            "app-label/model-name of its own and cannot be a swappable concrete model, ADR-052 "
            "section 2). Point the setting at an abstract model, e.g. icv_core.models.BaseModel.",
            obj=base,
            id=E005_ID,
        )

    return None


def check_base_model_is_abstract(app_configs, **kwargs):  # type: ignore[no-untyped-def]
    """Django system check entry point: error if the resolved ADR-052 base model is not abstract."""
    error = _base_model_not_abstract_error()
    return [error] if error is not None else []


@register(Tags.models)
def check_icv_search_configuration(app_configs, **kwargs):
    """Validate icv_search configuration at Django startup."""
    from icv_search.conf import (
        ICV_SEARCH_BACKEND,
        ICV_SEARCH_TIMEOUT,
        ICV_SEARCH_URL,
    )

    errors = []

    # ICV_SEARCH_BACKEND: must be a valid import path
    if not ICV_SEARCH_BACKEND:
        errors.append(
            Error(
                "ICV_SEARCH_BACKEND is not set.",
                hint="Set ICV_SEARCH_BACKEND to a backend class path (e.g., 'icv_search.backends.meilisearch.MeilisearchBackend').",
                id="icv_search.E001",
            )
        )
    else:
        try:
            import_string(ICV_SEARCH_BACKEND)
        except ImportError as e:
            errors.append(
                Error(
                    f"ICV_SEARCH_BACKEND cannot be imported: {e}",
                    hint=f"Current value: {ICV_SEARCH_BACKEND!r}. Ensure the path is correct.",
                    id="icv_search.E002",
                )
            )

    # ICV_SEARCH_URL: validate format if using Meilisearch backend
    if "meilisearch" in ICV_SEARCH_BACKEND.lower() and (
        not ICV_SEARCH_URL or not ICV_SEARCH_URL.startswith(("http://", "https://"))
    ):
        errors.append(
            Error(
                "ICV_SEARCH_URL must be a valid HTTP/HTTPS URL when using Meilisearch backend.",
                hint=f"Current value: {ICV_SEARCH_URL!r}. Set to 'http://localhost:7700' or your Meilisearch URL.",
                id="icv_search.E003",
            )
        )

    # ICV_SEARCH_TIMEOUT: must be positive
    if not isinstance(ICV_SEARCH_TIMEOUT, int) or ICV_SEARCH_TIMEOUT <= 0:
        errors.append(
            Error(
                "ICV_SEARCH_TIMEOUT must be a positive integer.",
                hint=f"Current value: {ICV_SEARCH_TIMEOUT}. Set to timeout in seconds (e.g., 30).",
                id="icv_search.E004",
            )
        )

    return errors


__all__ = ["check_base_model_is_abstract", "check_icv_search_configuration"]
