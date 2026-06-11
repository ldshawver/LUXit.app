"""
Tests for market intelligence models and agent reporting.
"""

import pytest

from app import app, db
from agents.market_intelligence_agent import MarketIntelligenceAgent
from models import (
    Company,
    Competitor,
    CompetitorContent,
    MarketSignal,
    StrategyRecommendation,
    AgentReport,
)


@pytest.fixture
def app_context():
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def test_competitor_crud(app_context):
    company = Company(name='Test Co')
    db.session.add(company)
    db.session.commit()

    competitor = Competitor(
        company_id=company.id,
        name='Rival Co',
        website_url='https://rival.example.com',
        industry='Retail'
    )
    db.session.add(competitor)
    db.session.commit()

    fetched = Competitor.query.filter_by(company_id=company.id, name='Rival Co').first()
    assert fetched is not None
    assert fetched.status == 'active'

    fetched.status = 'watchlist'
    db.session.commit()

    updated = db.session.get(Competitor, fetched.id)
    assert updated.status == 'watchlist'


def test_competitor_content_relationship(app_context):
    company = Company(name='Content Co')
    competitor = Competitor(company=company, name='Signal Rival')
    db.session.add_all([company, competitor])
    db.session.commit()

    content = CompetitorContent(
        competitor_id=competitor.id,
        content_type='blog',
        title='Launch update',
        url='https://rival.example.com/blog',
        summary='New product launch announced.'
    )
    db.session.add(content)
    db.session.commit()

    refreshed = db.session.get(Competitor, competitor.id)
    assert len(refreshed.content_items) == 1
    assert refreshed.content_items[0].title == 'Launch update'


def test_market_signal_and_strategy_recommendation(app_context):
    company = Company(name='Signal Co')
    db.session.add(company)
    db.session.commit()

    signal = MarketSignal(
        company_id=company.id,
        source='reddit',
        signal_type='sentiment',
        title='Pricing feedback',
        summary='Thread indicates price sensitivity.'
    )
    db.session.add(signal)
    db.session.commit()

    recommendation = StrategyRecommendation(
        company_id=company.id,
        related_signal_id=signal.id,
        title='Test pricing incentives',
        recommendation_type='pricing',
        priority='high',
        rationale='Address price sensitivity in community feedback.'
    )
    db.session.add(recommendation)
    db.session.commit()

    fetched = db.session.get(StrategyRecommendation, recommendation.id)
    assert fetched.related_signal_id == signal.id
    assert fetched.priority == 'high'


def test_market_intelligence_agent_report_generation(app_context):
    company = Company(name='Report Co')
    db.session.add(company)
    db.session.commit()

    competitor = Competitor(company_id=company.id, name='Rival One')
    signal = MarketSignal(
        company_id=company.id,
        source='trends',
        signal_type='demand_shift',
        title='Search volume spike',
        summary='Seasonal demand uptick.'
    )
    recommendation = StrategyRecommendation(
        company_id=company.id,
        title='Adjust messaging',
        recommendation_type='messaging',
        priority='medium'
    )
    db.session.add_all([competitor, signal, recommendation])
    db.session.commit()

    agent = MarketIntelligenceAgent()
    result = agent.generate_report(company.id, cadence='weekly')

    assert result['success'] is True
    report = db.session.get(AgentReport, result['report_id'])
    assert report is not None
    assert report.report_data['metrics']['competitor_count'] == 1
    assert report.report_data['metrics']['signals_detected'] == 1
