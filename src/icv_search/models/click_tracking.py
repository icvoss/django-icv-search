"""Click tracking models for search result interaction analytics."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from icv_search.models.base import BaseModel


class SearchClick(BaseModel):
    """Records an individual click on a search result.

    .. warning::

       Rows here hold end-user data: the verbatim ``query`` string and a
       free-form ``metadata`` JSON field whose own help text invites session
       identifiers.

       ``tenant_id`` is an **advisory label, not an isolation boundary**. It
       is a plain string with no FK and no constraint; this package ships no
       custom manager, so nothing filters it by default. The service-layer
       click functions filter by it only when the caller passes a non-empty
       value and **fail open** when it is omitted. Anything reaching this
       model directly is unscoped. See :class:`~icv_search.models.analytics.SearchQueryLog`
       for the full note, which applies identically here.

    One record is written per click event.  Use :class:`SearchClickAggregate`
    for dashboard queries — it provides pre-rolled daily counts without
    scanning this table.
    """

    index_name = models.CharField(
        max_length=200,
        db_index=True,
        verbose_name=_("index name"),
        help_text=_("Logical search index name (e.g. 'products')."),
    )
    query = models.CharField(
        max_length=500,
        verbose_name=_("query"),
        help_text=_("The search query string that produced the result page."),
    )
    document_id = models.CharField(
        max_length=500,
        db_index=True,
        verbose_name=_("document ID"),
        help_text=_("Identifier of the document that was clicked."),
    )
    position = models.PositiveSmallIntegerField(
        verbose_name=_("position"),
        help_text=_("Zero-based position of the clicked document in the result list."),
    )
    tenant_id = models.CharField(
        max_length=200,
        default="",
        blank=True,
        db_index=True,
        verbose_name=_("tenant ID"),
        help_text=_(
            "Advisory tenant label. NOT an isolation boundary: it is a plain "
            "indexed string with no foreign key and no constraint, nothing "
            "validates it, and no manager filters on it. The service-layer "
            "click functions filter by it only when a caller passes a "
            "non-empty tenant_id, and they FAIL OPEN when it is omitted, "
            "returning every tenant's rows. A caller reaching these models "
            "directly is unscoped entirely. Treat it as a grouping label for "
            "reporting, never as a security control."
        ),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("metadata"),
        help_text=_("Extra context such as source page, session ID, or A/B test variant."),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("search click")
        verbose_name_plural = _("search clicks")
        indexes = [
            models.Index(
                fields=["index_name", "query", "created_at"],
                name="icv_srch_clk_qry_dt",
            ),
            models.Index(
                fields=["index_name", "document_id"],
                name="icv_srch_clk_doc",
            ),
        ]

    def __str__(self) -> str:
        return f'Click on "{self.document_id}" from "{self.query}" in {self.index_name}'


class SearchClickAggregate(BaseModel):
    """Daily rollup of click events keyed on (index_name, query, document_id, date, tenant_id).

    Rather than scanning :class:`SearchClick` rows, dashboards and CTR
    calculations read this pre-aggregated table.  One row accumulates all
    clicks for a given document/query pair on a given day.
    """

    index_name = models.CharField(
        max_length=200,
        db_index=True,
        verbose_name=_("index name"),
        help_text=_("Logical search index name (e.g. 'products')."),
    )
    query = models.CharField(
        max_length=500,
        verbose_name=_("query"),
        help_text=_("Normalised (lowercased) search query string."),
    )
    document_id = models.CharField(
        max_length=500,
        db_index=True,
        verbose_name=_("document ID"),
        help_text=_("Identifier of the document that was clicked."),
    )
    date = models.DateField(
        db_index=True,
        verbose_name=_("date"),
        help_text=_("Calendar date on which these clicks occurred."),
    )
    click_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("click count"),
        help_text=_("Total number of clicks on this document for this query on this date."),
    )
    tenant_id = models.CharField(
        max_length=200,
        default="",
        blank=True,
        db_index=True,
        verbose_name=_("tenant ID"),
        help_text=_("Tenant identifier for multi-tenant setups."),
    )

    class Meta:
        ordering = ["-date"]
        verbose_name = _("search click aggregate")
        verbose_name_plural = _("search click aggregates")
        constraints = [
            models.UniqueConstraint(
                fields=["index_name", "query", "document_id", "date", "tenant_id"],
                name="icv_search_click_agg_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["index_name", "query", "date"],
                name="icv_srch_clkagg_qry_dt",
            ),
            models.Index(
                fields=["index_name", "date", "tenant_id"],
                name="icv_srch_clkagg_tnt_dt",
            ),
        ]

    def __str__(self) -> str:
        return f'"{self.query}" \u2192 {self.document_id} on {self.index_name} ({self.date})'
