"""Generate a search intelligence report."""

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Generate a search intelligence report for one or more indexes."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--index",
            type=str,
            help="Index name to report on.",
        )
        group.add_argument(
            "--all-indexes",
            action="store_true",
            default=False,
            help="Run for all known index names in SearchQueryAggregate.",
        )
        parser.add_argument("--days", type=int, default=30, help="Lookback window in days.")
        parser.add_argument(
            "--min-volume",
            type=int,
            default=None,
            help="Minimum query volume to include.",
        )
        parser.add_argument("--output", type=str, default="", help="File path for JSON output.")
        parser.add_argument("--tenant-id", type=str, default="", help="Tenant scope.")
        parser.add_argument(
            "--format",
            type=str,
            choices=["text", "json"],
            default="text",
            help="Output format.",
        )

    def handle(self, *args, **options):
        from icv_search.models.aggregates import SearchQueryAggregate
        from icv_search.services.analytics import get_popular_queries, get_zero_result_queries
        from icv_search.services.intelligence import get_demand_signals

        if options["all_indexes"]:
            indexes = list(
                SearchQueryAggregate.objects.order_by("index_name")
                .values_list("index_name", flat=True)
                .distinct()
            )
            if not indexes:
                raise CommandError("No index names found in SearchQueryAggregate.")
        else:
            indexes = [options["index"]]

        days = options["days"]
        tenant_id = options["tenant_id"]
        min_volume = options["min_volume"]

        all_reports = {}

        for index_name in indexes:
            popular = get_popular_queries(index_name, days=days, limit=20, tenant_id=tenant_id)
            zero_result = get_zero_result_queries(index_name, days=days, limit=20, tenant_id=tenant_id)

            demand_kwargs = {"days": days, "tenant_id": tenant_id}
            if min_volume is not None:
                demand_kwargs["min_volume"] = min_volume
            demand = get_demand_signals(index_name, **demand_kwargs)

            # Optionally include clusters and synonyms if pg_trgm is available
            clusters = []
            synonyms = []
            try:
                from icv_search.services.intelligence import cluster_queries, suggest_synonyms

                clusters = cluster_queries(index_name, days=days, tenant_id=tenant_id)[:10]
                synonyms = suggest_synonyms(index_name, days=days, tenant_id=tenant_id)
            except Exception:
                pass  # pg_trgm not available, skip clustering and synonyms

            report = {
                "index_name": index_name,
                "days": days,
                "popular_queries": popular[:20],
                "zero_result_queries": zero_result[:20],
                "demand_signals": demand[:20],
                "query_clusters": clusters,
                "synonym_suggestions": synonyms,
            }
            all_reports[index_name] = report

        if options["format"] == "json":
            output = json.dumps(all_reports, indent=2, default=str)
        else:
            output = self._format_text(all_reports)

        if options["output"]:
            with open(options["output"], "w") as f:
                f.write(output)
            self.stdout.write(self.style.SUCCESS(f"Report written to {options['output']}"))
        else:
            self.stdout.write(output)

    def _format_text(self, reports: dict) -> str:
        lines = []
        for index_name, report in reports.items():
            lines.append(f"\n{'=' * 60}")
            lines.append(f"Search Intelligence Report: {index_name}")
            lines.append(f"Window: {report['days']} days")
            lines.append(f"{'=' * 60}")

            lines.append("\n--- Popular Queries (top 20) ---")
            for q in report["popular_queries"]:
                lines.append(f"  {q['query']}: {q['count']} searches")

            lines.append("\n--- Zero-Result Queries (top 20) ---")
            for q in report["zero_result_queries"]:
                lines.append(f"  {q['query']}: {q['count']} zero-result searches")

            lines.append("\n--- Demand Signals (top 20 by gap score) ---")
            for s in report["demand_signals"][:20]:
                lines.append(
                    f"  {s['query']}: gap_score={s['gap_score']:.1f}, "
                    f"volume={s['volume']}, zero_rate={s['zero_result_rate']:.2f}, "
                    f"trend={s['trend']:.1f}%"
                )

            if report["query_clusters"]:
                lines.append("\n--- Query Clusters (top 10 by volume) ---")
                for c in report["query_clusters"]:
                    members = ", ".join(c["member_queries"][:5])
                    lines.append(f"  [{c['representative_query']}] ({c['total_volume']} total): {members}")

            if report["synonym_suggestions"]:
                lines.append("\n--- Synonym Suggestions ---")
                for s in report["synonym_suggestions"]:
                    lines.append(
                        f'  "{s["source_query"]}" -> "{s["suggested_synonym"]}" (confidence: {s["confidence"]:.2f})'
                    )

        return "\n".join(lines)
