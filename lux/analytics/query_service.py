"""Analytics query service with tenant-scoped metrics."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import case, func

from lux.extensions import db
from lux.models.analytics import AnalyticsDailyRollup, ConsentSuppressed, RawEvent, Session


class AnalyticsQueryService:
    """Central analytics query helper."""

    @staticmethod
    def events_by_day(company_id: int, start: datetime, end: datetime) -> list[dict]:
        rows = (
            db.session.query(
                AnalyticsDailyRollup.day,
                AnalyticsDailyRollup.total_events,
                AnalyticsDailyRollup.page_views,
            )
            .filter(
                AnalyticsDailyRollup.company_id == company_id,
                AnalyticsDailyRollup.day >= start.date(),
                AnalyticsDailyRollup.day <= end.date(),
            )
            .order_by(AnalyticsDailyRollup.day.asc())
            .all()
        )
        if rows:
            return [
                {
                    "day": row.day.isoformat(),
                    "total_events": row.total_events,
                    "page_views": row.page_views,
                }
                for row in rows
            ]

        raw_rows = (
            db.session.query(
                func.date(RawEvent.occurred_at).label("day"),
                func.count(RawEvent.id).label("total_events"),
                func.sum(case((RawEvent.event_name == "page_view", 1), else_=0)).label("page_views"),
            )
            .filter(
                RawEvent.company_id == company_id,
                RawEvent.occurred_at >= start,
                RawEvent.occurred_at <= end,
            )
            .group_by(func.date(RawEvent.occurred_at))
            .order_by(func.date(RawEvent.occurred_at).asc())
            .all()
        )
        return [
            {
                "day": row.day.isoformat(),
                "total_events": int(row.total_events or 0),
                "page_views": int(row.page_views or 0),
            }
            for row in raw_rows
        ]

    @staticmethod
    def sessions_by_day(company_id: int, start: datetime, end: datetime) -> list[dict]:
        rows = (
            db.session.query(
                func.date(Session.started_at).label("day"),
                func.count(Session.id).label("sessions"),
            )
            .filter(
                Session.company_id == company_id,
                Session.started_at >= start,
                Session.started_at <= end,
            )
            .group_by(func.date(Session.started_at))
            .order_by(func.date(Session.started_at))
            .all()
        )
        return [
            {
                "day": row.day.isoformat(),
                "sessions": int(row.sessions or 0),
            }
            for row in rows
        ]

    @staticmethod
    def top_pages(company_id: int, start: datetime, end: datetime, limit: int = 10) -> list[dict]:
        rows = (
            db.session.query(RawEvent.page_url, func.count(RawEvent.id).label("hits"))
            .filter(
                RawEvent.company_id == company_id,
                RawEvent.occurred_at >= start,
                RawEvent.occurred_at <= end,
                RawEvent.page_url.isnot(None),
            )
            .group_by(RawEvent.page_url)
            .order_by(func.count(RawEvent.id).desc())
            .limit(limit)
            .all()
        )
        return [{"page": row.page_url, "hits": int(row.hits or 0)} for row in rows]

    @staticmethod
    def top_referrers(company_id: int, start: datetime, end: datetime, limit: int = 10) -> list[dict]:
        rows = (
            db.session.query(RawEvent.referrer, func.count(RawEvent.id).label("hits"))
            .filter(
                RawEvent.company_id == company_id,
                RawEvent.occurred_at >= start,
                RawEvent.occurred_at <= end,
                RawEvent.referrer.isnot(None),
            )
            .group_by(RawEvent.referrer)
            .order_by(func.count(RawEvent.id).desc())
            .limit(limit)
            .all()
        )
        return [{"referrer": row.referrer, "hits": int(row.hits or 0)} for row in rows]

    @staticmethod
    def utm_breakdown(company_id: int, start: datetime, end: datetime) -> dict:
        rows = (
            db.session.query(
                RawEvent.utm_source,
                RawEvent.utm_medium,
                RawEvent.utm_campaign,
                func.count(RawEvent.id).label("hits"),
            )
            .filter(
                RawEvent.company_id == company_id,
                RawEvent.occurred_at >= start,
                RawEvent.occurred_at <= end,
            )
            .group_by(RawEvent.utm_source, RawEvent.utm_medium, RawEvent.utm_campaign)
            .all()
        )
        grouped = defaultdict(int)
        for row in rows:
            key = f"{row.utm_source or 'direct'} / {row.utm_medium or 'none'} / {row.utm_campaign or 'none'}"
            grouped[key] += int(row.hits or 0)
        return dict(grouped)

    @staticmethod
    def consent_suppressed(company_id: int, start: datetime, end: datetime) -> int:
        total = (
            db.session.query(func.sum(ConsentSuppressed.count))
            .filter(
                ConsentSuppressed.company_id == company_id,
                ConsentSuppressed.day >= start.date(),
                ConsentSuppressed.day <= end.date(),
            )
            .scalar()
        )
        return int(total or 0)

    @staticmethod
    def summary(company_id: int, start: datetime, end: datetime) -> dict:
        events = AnalyticsQueryService.events_by_day(company_id, start, end)
        sessions = AnalyticsQueryService.sessions_by_day(company_id, start, end)
        total_events = sum(item["total_events"] for item in events)
        total_sessions = sum(item["sessions"] for item in sessions)
        return {
            "total_events": total_events,
            "total_sessions": total_sessions,
            "top_pages": AnalyticsQueryService.top_pages(company_id, start, end),
            "top_referrers": AnalyticsQueryService.top_referrers(company_id, start, end),
            "utm_breakdown": AnalyticsQueryService.utm_breakdown(company_id, start, end),
            "consent_suppressed": AnalyticsQueryService.consent_suppressed(company_id, start, end),
        }
