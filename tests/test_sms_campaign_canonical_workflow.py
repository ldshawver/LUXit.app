import os
import pytest
from app import create_app
from extensions import db
from models import Company, Contact, Segment, SMSCampaign, TwilioPhoneNumber, User, user_company
from services.contact_audience import resolve_sms_campaign_recipients
from services.sms_service import SMSService

@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    app = create_app(); app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all(); yield app; db.session.remove(); db.drop_all()

@pytest.fixture
def world(app):
    co = Company(name="MyOrder tenant"); other = Company(name="Other tenant")
    user = User(username="sms-owner", email="sms-owner@example.test", is_admin=True)
    user.password_hash = "x"; db.session.add_all([co, other, user]); db.session.flush()
    user.default_company_id = co.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=co.id, is_default=True))
    tag = Segment(company_id=co.id, name="  MyOrder   Customer ")
    line = TwilioPhoneNumber(company_id=co.id, phone_number="+19165989519", is_active=True, sms_enabled=True, campaign_sender_enabled=True)
    inactive = TwilioPhoneNumber(company_id=co.id, phone_number="+19165550000", is_active=False, sms_enabled=True, campaign_sender_enabled=True)
    foreign = TwilioPhoneNumber(company_id=other.id, phone_number="+19165550001", is_active=True, sms_enabled=True, campaign_sender_enabled=True)
    db.session.add_all([tag, line, inactive, foreign]); db.session.commit()
    return user, co, other, tag, line, inactive, foreign

def login(client, user):
    with client.session_transaction() as sess: sess["_user_id"] = str(user.id); sess["_fresh"] = True

def contact(company, phone, **kw):
    values = dict(company_id=company.id, tags="myorder customer", phone=phone, is_active=True,
                  sms_marketing_opt_in=True, sms_consent_status="opted_in")
    values.update(kw); return Contact(**values)

def campaign(company, tag, line):
    return SMSCampaign(company_id=company.id, name="MyOrder", message="Hi STOP", segment="MyOrder Customer",
                       selected_tag_ids=[tag.id], audience_filter={"selected_tag_ids": [tag.id]},
                       from_phone_number_id=line.id, from_phone_number=line.phone_number, status="draft")

def test_sender_api_display_and_server_authorization(app, world):
    user, co, _, tag, line, inactive, foreign = world; client=app.test_client(); login(client,user)
    payload=client.get("/api/marketing/sms-senders").get_json()
    assert payload["senders"] == [{"id":line.id,"phone_number":"+19165989519","display":"916-598-9519","friendly_name":None}]
    for bad in (inactive.id, foreign.id):
        response=client.post("/api/marketing/sms-campaigns", json={"segment":"MyOrder Customer","from_phone_number_id":bad})
        assert response.status_code == 403

def test_exact_counts_duplicates_and_tenant_isolation(app, world, monkeypatch):
    _, co, other, tag, line, *_ = world
    phones=[f"+14155551{i:03d}" for i in range(10)]
    db.session.add_all([contact(co,p) for p in phones] + [contact(other,f"+14156661{i:03d}") for i in range(11)])
    c=campaign(co,tag,line); db.session.add(c); db.session.commit()
    result=resolve_sms_campaign_recipients(c, materialize=True)
    assert result["counts"]["matching_contacts"] == 10
    assert result["counts"]["unique_phone_numbers"] == 10
    assert result["counts"]["eligible_recipients"] == 10
    attempts=[]
    monkeypatch.setattr(SMSService,"send_sms",classmethod(lambda cls,to,message,**kw: attempts.append(to) or {"success":True,"message_sid":f"SM{len(attempts)}"}))
    assert SMSService.send_campaign(c.id)["sent"] == 10
    assert len(attempts) == 10 and not any(p.startswith("+1415666") for p in attempts)
    scheduled=campaign(co,tag,line); scheduled.status="scheduled"
    scheduled.scheduled_eligible_recipient_count=10
    db.session.add(scheduled); db.session.commit()
    scheduled_result=SMSService.execute_scheduled_campaign(scheduled.id)
    assert scheduled_result["sent"] == 10
    assert len(attempts) == 20
    assert scheduled.execution_recipient_count == 10
    assert scheduled.execution_count_delta == 0

def test_exclusion_accounting_exact(app, world):
    _, co, _, tag, line, *_ = world
    rows=[contact(co,"+14155552001"), contact(co,None), contact(co,"bad"),
          contact(co,"+14155552002"), contact(co,"(415) 555-2002"),
          contact(co,"+14155552003",sms_opted_out=True),
          contact(co,"+14155552004",sms_marketing_opt_in=False,sms_consent_status="unknown"),
          contact(co,"+14155552005",is_active=False), contact(co,"+14155552006"),
          contact(co,"+14155552007"), contact(co,"+14155552008"), contact(co,"+14155552009")]
    db.session.add_all(rows); c=campaign(co,tag,line);db.session.add(c);db.session.commit()
    x=resolve_sms_campaign_recipients(c)["counts"]
    assert {k:x[k] for k in ("matching_contacts","contacts_with_phone","unique_phone_numbers","eligible_recipients","missing_phone_numbers","invalid_phone_numbers","duplicate_phone_numbers","opted_out_contacts","missing_sms_consent","archived_or_suppressed")} == {
      "matching_contacts":12,"contacts_with_phone":11,"unique_phone_numbers":9,"eligible_recipients":6,
      "missing_phone_numbers":1,"invalid_phone_numbers":1,"duplicate_phone_numbers":1,"opted_out_contacts":1,
      "missing_sms_consent":1,"archived_or_suppressed":1}

def test_duplicate_normalization_and_zero_does_not_send(app, world, monkeypatch):
    _,co,_,tag,line,*_=world
    db.session.add_all([contact(co,p) for p in ["9165989519","(916) 598-9519","+1 916-598-9519"]])
    c=campaign(co,tag,line);db.session.add(c);db.session.commit()
    x=resolve_sms_campaign_recipients(c,materialize=True)["counts"]
    assert (x["matching_contacts"],x["unique_phone_numbers"],x["eligible_recipients"]) == (3,1,1)
    attempts=[];monkeypatch.setattr(SMSService,"send_sms",classmethod(lambda cls,*a,**k: attempts.append(a) or {"success":True,"message_sid":"SM1"}))
    assert SMSService.send_campaign(c.id)["sent"] == 1 and len(attempts)==1
    for row in Contact.query.filter_by(company_id=co.id).all(): row.sms_opted_out=True
    zero=campaign(co,tag,line);zero.status="scheduled";db.session.add(zero);db.session.commit()
    result=SMSService.execute_scheduled_campaign(zero.id)
    assert result["success"] is False and attempts == [attempts[0]]
