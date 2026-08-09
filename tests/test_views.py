"""Tests for icv_search.views, in particular the click-tracking endpoint.

Regression coverage for #9: tenant attribution on ``icv_search_click`` must
never be taken from client-supplied JSON.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import RequestFactory


@pytest.fixture
def rf():
    return RequestFactory()


def _post_click(rf, body: dict, content_type: str = "application/json"):
    from icv_search.views import icv_search_click

    request = rf.post(
        "/click/",
        data=json.dumps(body),
        content_type=content_type,
    )
    return icv_search_click(request)


class TestIcvSearchClickTenantAttribution:
    """Tenant attribution must be derived server-side, never from the client."""

    def test_client_supplied_tenant_id_is_ignored(self, rf, settings):
        """A tenant_id in the JSON body must never reach log_click().

        This is the exact shape of #9: a client attributing a click event
        to an arbitrary tenant by adding "tenant_id" to the POST body.
        """
        settings.ICV_SEARCH_CLICK_TRACKING = True

        with patch("icv_search.services.click_tracking.log_click") as mock_log_click:
            response = _post_click(
                rf,
                {
                    "index_name": "products",
                    "query": "shoes",
                    "document_id": "42",
                    "position": 0,
                    "tenant_id": "attacker-controlled-tenant",
                },
            )

        assert response.status_code == 204
        mock_log_click.assert_called_once()
        called_tenant_id = mock_log_click.call_args.kwargs["tenant_id"]
        assert called_tenant_id != "attacker-controlled-tenant"

    def test_tenant_id_is_derived_from_trusted_request_context(self, rf, settings):
        """The tenant passed to log_click() must come from get_current_tenant_id()."""
        settings.ICV_SEARCH_CLICK_TRACKING = True

        with (
            patch("icv_search.views.get_current_tenant_id", return_value="trusted-tenant"),
            patch("icv_search.services.click_tracking.log_click") as mock_log_click,
        ):
            response = _post_click(
                rf,
                {
                    "index_name": "products",
                    "query": "shoes",
                    "document_id": "42",
                    "position": 0,
                    "tenant_id": "attacker-controlled-tenant",
                },
            )

        assert response.status_code == 204
        mock_log_click.assert_called_once()
        assert mock_log_click.call_args.kwargs["tenant_id"] == "trusted-tenant"

    def test_no_tenant_id_in_body_still_uses_trusted_context(self, rf, settings):
        """Omitting tenant_id entirely must still resolve from the trusted context."""
        settings.ICV_SEARCH_CLICK_TRACKING = True

        with (
            patch("icv_search.views.get_current_tenant_id", return_value="trusted-tenant"),
            patch("icv_search.services.click_tracking.log_click") as mock_log_click,
        ):
            response = _post_click(
                rf,
                {
                    "index_name": "products",
                    "query": "shoes",
                    "document_id": "42",
                    "position": 0,
                },
            )

        assert response.status_code == 204
        assert mock_log_click.call_args.kwargs["tenant_id"] == "trusted-tenant"


class TestIcvSearchClickBasics:
    """Existing validation behaviour must be unaffected by the tenant fix."""

    def test_disabled_returns_403(self, rf, settings):
        settings.ICV_SEARCH_CLICK_TRACKING = False

        response = _post_click(rf, {"index_name": "products", "query": "shoes", "document_id": "42", "position": 0})

        assert response.status_code == 403

    def test_missing_required_field_returns_400(self, rf, settings):
        settings.ICV_SEARCH_CLICK_TRACKING = True

        response = _post_click(rf, {"index_name": "products", "query": "shoes"})

        assert response.status_code == 400

    def test_invalid_json_returns_400(self, rf, settings):
        settings.ICV_SEARCH_CLICK_TRACKING = True

        from icv_search.views import icv_search_click

        request = rf.post("/click/", data="not json", content_type="application/json")
        response = icv_search_click(request)

        assert response.status_code == 400

    def test_valid_request_returns_204(self, rf, settings):
        settings.ICV_SEARCH_CLICK_TRACKING = True

        with patch("icv_search.services.click_tracking.log_click"):
            response = _post_click(
                rf,
                {"index_name": "products", "query": "shoes", "document_id": "42", "position": 0},
            )

        assert response.status_code == 204
