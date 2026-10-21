"""Create or update the Opik operations dashboard for the refund agent.

    python -m scripts.opik_dashboards

Reads the same OPIK_* settings as the backend (self-hosted or Comet-hosted).
Idempotent: if a dashboard with the same name exists, its sections are rebuilt
in place, so the URL stays stable. Run it once per environment, and again
after changing the widgets below.

Every widget charts data the backend already sends (`app/tracing.py`): token
usage and estimated cost on LLM spans, the tags on each trace, and the 0/1
outcome scores, whose averages are rates.
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.tracing import opik_host, quiet_sdk

NAME = "Refund agent: operations"


def _sections():
    from opik import dashboard as d

    def card(title, metric, source=d.TraceDataType.TRACES):
        return d.DashboardWidget(
            type=d.WidgetType.PROJECT_STATS_CARD,
            title=title,
            config=d.ProjectStatsCardConfig(source=source, metric=metric),
        )

    def chart(
        title,
        metric_type,
        *,
        chart_type=d.ChartType.LINE,
        breakdown=None,
        scores=None,
        span_filters=None,
    ):
        return d.DashboardWidget(
            type=d.WidgetType.PROJECT_METRICS,
            title=title,
            config=d.ProjectMetricsConfig(
                metric_type=metric_type,
                chart_type=chart_type,
                breakdown=d.BreakdownConfig(field=breakdown) if breakdown else None,
                feedback_scores=scores,
                span_filters=span_filters,
            ),
        )

    llm_spans = [{"field": "type", "operator": "=", "value": "llm"}]

    m, s = d.ProjectMetricType, d.StatsCardMetric
    return {
        "Cost and usage": [
            card("Estimated spend", s.TOTAL_ESTIMATED_COST_SUM),
            card("Turns", s.TRACE_COUNT),
            card("Conversations", s.THREAD_COUNT),
            # On traces this is the average number of LLM calls per turn.
            card("LLM calls per turn", s.LLM_SPAN_COUNT),
            chart("Cost", m.COST),
            # Trace cost can't be split by model (Opik 2.2.95), but each trace is
            # tagged with its provider and prompt version.
            chart("Cost by tag (provider, prompt)", m.COST, breakdown=d.BreakdownField.TAGS),
            # A fallback model showing up here means the primary provider failed.
            chart(
                "LLM calls by model",
                m.SPAN_COUNT,
                breakdown=d.BreakdownField.MODEL,
                span_filters=llm_spans,
            ),
            chart("Token usage", m.TOKEN_USAGE, chart_type=d.ChartType.BAR),
            # Tags: live vs scripted, demo, provider, prompt version.
            chart("Turns by tag", m.TRACE_COUNT, breakdown=d.BreakdownField.TAGS),
        ],
        "Outcomes": [
            card("Approval rate", "feedback_scores.refund_approved"),
            card("Escalation rate", "feedback_scores.refund_escalated"),
            chart(
                "Refund decisions",
                m.FEEDBACK_SCORES,
                scores=["refund_approved", "refund_denied", "refund_escalated"],
            ),
            chart(
                "Reply corrections",
                m.FEEDBACK_SCORES,
                scores=["sanitizer_correction"],
            ),
        ],
        "Latency and reliability": [
            card("Latency p50", s.DURATION_P50),
            card("Latency p99", s.DURATION_P99),
            card("Errors", s.ERROR_COUNT),
            chart("Turn duration", m.DURATION),
            chart("LLM call duration by model", m.SPAN_DURATION, breakdown=d.BreakdownField.MODEL),
            chart("Fallbacks and errors", m.FEEDBACK_SCORES, scores=["fallback_used", "turn_error"]),
        ],
    }


def main() -> int:
    settings = get_settings()
    if not settings.opik_enabled:
        print("Opik is not configured: set OPIK_URL_OVERRIDE or OPIK_API_KEY.", file=sys.stderr)
        return 1

    quiet_sdk()
    import opik
    from opik import dashboard as d

    client = opik.Opik(
        project_name=settings.opik_project_name,
        workspace=settings.opik_workspace or None,
        host=opik_host(),
        api_key=settings.opik_api_key or None,
    )
    layout = _sections()
    empty = [d.DashboardSection(title=title) for title in layout]

    existing = [x for x in client.get_dashboards(name=NAME) if x.name == NAME]
    if existing:
        dash = existing[0]
        dash.replace_sections(empty)
    else:
        dash = client.create_dashboard(
            name=NAME,
            type=d.DashboardType.MULTI_PROJECT,
            description="Cost, outcomes and reliability of the refund agent (app/tracing.py).",
            project_name=settings.opik_project_name,
            sections=empty,
        )

    section_ids = {section.title: section.id for section in dash.sections}
    for title, widgets in layout.items():
        for widget in widgets:
            dash.add_widget(widget, section_id=section_ids[title])

    count = sum(len(w) for w in layout.values())
    print(f"{'Updated' if existing else 'Created'} '{NAME}' ({count} widgets), id {dash.id}")
    client.end()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
