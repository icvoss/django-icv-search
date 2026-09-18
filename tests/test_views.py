"""Tests for icv_search.views, in particular the click-tracking endpoint.

Regression coverage for #9: tenant attribution on ``icv_search_click`` must
never be taken from client-supplied JSON.
"""

from __future__ import annotations

import json
import sys
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


def _metadata_at_sizeof(target: int) -> dict:
    """Build metadata whose ``sys.getsizeof(str(...))`` equals ``target``.

    The click endpoint measures metadata with ``sys.getsizeof``, not UTF-8
    length (#55). A JSON round-trip must preserve that size.
    """
    for length in range(max(0, target - 100), target + 100):
        candidate = {"payload": "x" * length}
        if sys.getsizeof(str(candidate)) == target:
            # Mirror the view's json.loads path so the measured object matches.
            round_tripped = json.loads(json.dumps(candidate))
            if sys.getsizeof(str(round_tripped)) == target:
                return round_tripped
    raise AssertionError(f"could not build metadata with sizeof {target}")


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


class TestIcvSearchClickValidationBoundaries:
    """Field length and metadata sizeof boundaries on the click endpoint (#55)."""

    @pytest.mark.parametrize("field_name", ("index_name", "query", "document_id"))
    def test_field_at_max_length_is_accepted(self, rf, settings, field_name):
        settings.ICV_SEARCH_CLICK_TRACKING = True
        body = {
            "index_name": "products",
            "query": "shoes",
            "document_id": "42",
            "position": 0,
        }
        body[field_name] = "a" * 500

        with patch("icv_search.services.click_tracking.log_click"):
            response = _post_click(rf, body)

        assert response.status_code == 204

    @pytest.mark.parametrize("field_name", ("index_name", "query", "document_id"))
    def test_field_over_max_length_returns_400(self, rf, settings, field_name):
        settings.ICV_SEARCH_CLICK_TRACKING = True
        body = {
            "index_name": "products",
            "query": "shoes",
            "document_id": "42",
            "position": 0,
        }
        body[field_name] = "a" * 501

        response = _post_click(rf, body)

        assert response.status_code == 400

    def test_metadata_at_max_sizeof_is_accepted(self, rf, settings):
        settings.ICV_SEARCH_CLICK_TRACKING = True
        metadata = _metadata_at_sizeof(10240)
        assert sys.getsizeof(str(metadata)) == 10240

        with patch("icv_search.services.click_tracking.log_click"):
            response = _post_click(
                rf,
                {
                    "index_name": "products",
                    "query": "shoes",
                    "document_id": "42",
                    "position": 0,
                    "metadata": metadata,
                },
            )

        assert response.status_code == 204

    def test_metadata_over_max_sizeof_returns_400(self, rf, settings):
        settings.ICV_SEARCH_CLICK_TRACKING = True
        metadata = _metadata_at_sizeof(10241)
        assert sys.getsizeof(str(metadata)) == 10241
        # UTF-8 length of the JSON body value is not the gate the view uses.
        assert len(json.dumps(metadata).encode("utf-8")) != 10241

        response = _post_click(
            rf,
            {
                "index_name": "products",
                "query": "shoes",
                "document_id": "42",
                "position": 0,
                "metadata": metadata,
            },
        )

        assert response.status_code == 400
