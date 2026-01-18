"""
Market Intelligence Agent
Runs daily, weekly, and monthly market intelligence reports.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Dict, Any

from extensions import db
from models import Company, Competitor, MarketSignal, StrategyRecommendation, AgentReport

logger = logging.getLogger(__name__)


class MarketIntelligenceAgent:
    """Agent for structured market intelligence reporting."""

    agent_name = "Market Intelligence Agent"
    agent_type = "market_intelligence"

    def generate_daily_report(self, company_id: int) -> Dict[str, Any]:
        return self._generate_report(company_id, cadence="daily")

    def generate_report(self, company_id: int, cadence: str = "weekly") -> Dict[str, Any]:
        return self._generate_report(company_id, cadence=cadence)

    def generate_weekly_report(self, company_id: int) -> Dict[str, Any]:
        return self._generate_report(company_id, cadence="weekly")

    def generate_monthly_report(self, company_id: int) -> Dict[str, Any]:
        return self._generate_report(company_id, cadence="monthly")

    def _generate_report(self, company_id: int, cadence: str) -> Dict[str, Any]:
        company = Company.query.get(company_id)
        if not company:
            return {"success": False, "error": "Company not found"}

        period_start, period_end = self._resolve_period(cadence)

        competitor_count = Competitor.query.filter_by(company_id=company_id).count()
        signal_query = MarketSignal.query.filter(
            MarketSignal.company_id == company_id,
            MarketSignal.signal_date >= period_start,
            MarketSignal.signal_date <= period_end
        ).order_by(MarketSignal.signal_date.desc())
        signals = signal_query.limit(10).all()

        recommendation_query = StrategyRecommendation.query.filter(
            StrategyRecommendation.company_id == company_id,
            StrategyRecommendation.created_at >= period_start,
            StrategyRecommendation.created_at <= period_end
        ).order_by(StrategyRecommendation.created_at.desc())
        recommendations = recommendation_query.limit(10).all()

        report_payload = {
            "cadence": cadence,
            "company": {"id": company.id, "name": company.name},
            "generated_at": datetime.utcnow().isoformat(),
            "period": {
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
            },
            "metrics": {
                "competitor_count": competitor_count,
                "signals_detected": len(signals),
                "recommendations_generated": len(recommendations),
            },
            "signals": [
                {
                    "id": signal.id,
                    "title": signal.title,
                    "source": signal.source,
                    "severity": signal.severity,
                    "signal_date": signal.signal_date.isoformat() if signal.signal_date else None,
                    "summary": signal.summary,
                }
                for signal in signals
            ],
            "recommendations": [
                {
                    "id": recommendation.id,
                    "title": recommendation.title,
                    "priority": recommendation.priority,
                    "status": recommendation.status,
                    "recommendation_type": recommendation.recommendation_type,
                }
                for recommendation in recommendations
            ],
            "next_actions": [
                "Review high-severity signals and confirm relevance.",
                "Validate competitor updates against current positioning.",
                "Prioritize recommendations for execution planning.",
            ],
        }

        report = AgentReport(
            agent_type=self.agent_type,
            agent_name=self.agent_name,
            report_type=cadence,
            report_title=f"{company.name} {cadence.capitalize()} Market Intelligence Report",
            report_data=report_payload,
            insights="Placeholder insights: review signals and prioritize recommendations.",
            period_start=period_start,
            period_end=period_end,
            created_at=datetime.utcnow(),
        )
        db.session.add(report)
        db.session.commit()

        logger.info("Market intelligence report generated for company_id=%s cadence=%s", company_id, cadence)
        return {"success": True, "report_id": report.id, "report": report_payload}

    @staticmethod
    def _resolve_period(cadence: str) -> tuple[datetime, datetime]:
        end = datetime.utcnow()
        if cadence == "daily":
            start = end - timedelta(days=1)
        elif cadence == "monthly":
            start = end - timedelta(days=30)
        else:
            start = end - timedelta(days=7)
        return start, end
