"""Executive strategy agent for summary reports."""
from __future__ import annotations

from datetime import datetime

from lux.analytics.query_service import AnalyticsQueryService


class ExecutiveStrategyAgent:
    agent_type = "executive_strategy"

    def build_report(self, company_id: int, start: datetime, end: datetime, compare_start=None, compare_end=None) -> dict:
        summary = AnalyticsQueryService.summary(company_id, start, end)
        return {
            "kpi_summary": summary,
            "compare_period": {
                "start": compare_start.isoformat() if compare_start else None,
                "end": compare_end.isoformat() if compare_end else None,
            },
            "actions_proposed": [],
            "notes": "Executive summary generated from first-party analytics.",
        }
