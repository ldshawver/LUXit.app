"""An SMS campaign whose audience can't be resolved fails cleanly.

Regression: a campaign saved with a legacy segment name that has no
canonical tag ID made preview / analytics answer 500 (AudienceError escaped
the view), and the scheduled-send worker raised, leaving the campaign
"scheduled" forever.
"""
from tests.test_marketing_hub_regression import app, client, tenant_user  # noqa: F401  (fixtures)

from extensions import db
from models import Contact, SMSCampaign


def _legacy_campaign(company, status="draft"):
    db.session.add(Contact(company_id=company.id, phone="+14155550101", tags="sms_consent",
                           is_active=True, is_subscribed=True, segment="vip"))
    campaign = SMSCampaign(company_id=company.id, name="Legacy", message="Hi. Reply STOP to opt out.",
                           segment="vip", status=status)
    db.session.add(campaign)
    db.session.commit()
    return campaign


def test_preview_and_analytics_answer_422_not_500(client, tenant_user):
    _, company = tenant_user
    campaign = _legacy_campaign(company)
    for method, url in (("post", f"/api/marketing/sms-campaigns/{campaign.id}/preview"),
                        ("get", f"/api/marketing/sms-campaigns/{campaign.id}/analytics")):
        r = getattr(client, method)(url)
        assert r.status_code == 422, (url, r.status_code)
        body = r.get_json()
        assert body["code"] == "audience_invalid" and "canonical tag" in body["error"]


def test_scheduled_worker_marks_unresolvable_campaign_failed(client, tenant_user, monkeypatch):
    from services.phone_line_service import PhoneLineService
    from services.sms_service import SMSService

    _, company = tenant_user
    campaign = _legacy_campaign(company, status="scheduled")
    monkeypatch.setattr(PhoneLineService, "resolve_campaign_sender",
                        classmethod(lambda cls, *a, **k: {"success": True}))
    result = SMSService.execute_scheduled_campaign(campaign.id)
    assert result["success"] is False and result["sent"] == 0
    assert db.session.get(SMSCampaign, campaign.id).status == "failed"
