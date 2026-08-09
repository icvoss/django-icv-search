"""Health check views for icv-search."""

from __future__ import annotations

import sys

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from icv_search.backends import get_search_backend
from icv_search.middleware import get_current_tenant_id

_MAX_FIELD_LENGTH = 500
_MAX_METADATA_BYTES = 10240  # 10 KB


@require_GET
def icv_search_health(request):
    """Health check endpoint for the search backend.

    Returns 200 with {"status": "ok"} when the backend is reachable,
    or 503 with {"status": "unavailable"} when it is not.

    Intended for load balancer health probes and monitoring dashboards.
    """
    backend = get_search_backend()
    try:
        healthy = backend.health()
    except Exception:
        healthy = False

    if healthy:
        return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "unavailable"}, status=503)


@require_POST
@csrf_exempt  # Intentional: this endpoint is called by client-side JS on search result pages.
# Rate limiting is not implemented in this package and must be configured at
# the reverse proxy layer or added via a project-level middleware/decorator
# in front of this view. This is a deliberate deployment responsibility, not
# an oversight: a shared library should not impose a rate-limit backend
# (cache, Redis, etc.) on every consumer. See 04-interfaces.md "Click
# Tracking Endpoint" for the documented expectation.
def icv_search_click(request):
    """Record a search result click event.

    Accepts JSON with ``index_name``, ``query``, ``document_id``, and
    ``position``. Returns 204 on success, 400 on validation error, or 403
    when click tracking is disabled.

    Tenant attribution for the logged click is always derived server-side
    from :func:`icv_search.middleware.get_current_tenant_id` (populated by
    :class:`icv_search.middleware.ICVSearchTenantMiddleware`), never from
    the request body. This endpoint is CSRF-exempt and publicly reachable;
    trusting a client-supplied tenant identifier would let any caller
    attribute click events to an arbitrary tenant.
    """
    from django.conf import settings as django_settings

    if not getattr(django_settings, "ICV_SEARCH_CLICK_TRACKING", False):
        return JsonResponse({"error": "Click tracking is disabled."}, status=403)

    try:
        import json

        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    required = ("index_name", "query", "document_id", "position")
    missing = [f for f in required if f not in data]
    if missing:
        return JsonResponse({"error": f"Missing required fields: {', '.join(missing)}"}, status=400)

    # Validate position is an integer.
    try:
        position = int(data["position"])
    except (ValueError, TypeError):
        return JsonResponse({"error": "position must be an integer"}, status=400)

    # Validate field lengths.
    for field_name in ("index_name", "query", "document_id"):
        if len(str(data[field_name])) > _MAX_FIELD_LENGTH:
            return JsonResponse(
                {"error": f"{field_name} exceeds maximum length of {_MAX_FIELD_LENGTH}"},
                status=400,
            )

    # Validate metadata.
    metadata = data.get("metadata", {})
    if metadata is not None and not isinstance(metadata, dict):
        return JsonResponse({"error": "metadata must be a JSON object"}, status=400)
    if metadata and sys.getsizeof(str(metadata)) > _MAX_METADATA_BYTES:
        return JsonResponse({"error": "metadata exceeds maximum size"}, status=400)

    from icv_search.services.click_tracking import log_click

    # Tenant attribution is derived server-side from the trusted request
    # context (ICVSearchTenantMiddleware / ICV_SEARCH_TENANT_PREFIX_FUNC),
    # never from client-supplied JSON. This endpoint is CSRF-exempt and
    # publicly reachable, so a client-controlled "tenant_id" field would let
    # any caller attribute click events to an arbitrary tenant. Any
    # "tenant_id" present in the request body is silently ignored.
    log_click(
        index_name=data["index_name"],
        query=data["query"],
        document_id=str(data["document_id"]),
        position=position,
        tenant_id=get_current_tenant_id(),
        metadata=metadata or None,
    )

    return HttpResponse(status=204)
