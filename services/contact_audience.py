"""CRM contact upsert, source tagging, segment resolution, and SMS recipients."""
from __future__ import annotations

import csv, io, re
from datetime import datetime
from email.utils import parseaddr
from sqlalchemy import or_, func
from extensions import db
from models import Contact, Segment, SegmentMember, SMSRecipient

PHONE_SOURCE_TAG_RULES = [{"phone_number": "+19165989519", "tag": "MyOrder Customer"}]
SYSTEM_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "HELP", "INFO", "START", "UNSTOP"}


def normalize_phone(number: str | None) -> str:
    raw = (number or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+") and raw[1:].isdigit():
        return raw
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if 10 <= len(digits) <= 15 else ""


def _split_tags(tags):
    if not tags:
        return []
    if isinstance(tags, (list, tuple, set)):
        vals = tags
    else:
        vals = re.split(r"[,;|]", str(tags))
    out = []
    for v in vals:
        v = str(v).strip()
        if v and v.lower() not in [x.lower() for x in out]:
            out.append(v)
    return out


def add_contact_tag(contact: Contact, tag: str):
    tags = _split_tags(contact.tags)
    if tag and tag.lower() not in [t.lower() for t in tags]:
        tags.append(tag)
        contact.tags = ", ".join(tags)


def source_tag_rules(config: dict | None = None):
    return (config or {}).get("phone_source_tag_rules") or PHONE_SOURCE_TAG_RULES


def apply_source_tags(contact: Contact, source_phone_number: str | None, config: dict | None = None):
    source_e164 = normalize_phone(source_phone_number)
    for rule in source_tag_rules(config):
        if normalize_phone(rule.get("phone_number")) == source_e164:
            add_contact_tag(contact, rule.get("tag"))


def upsert_contact_from_source(company_id: int, phone: str | None = None, email: str | None = None, *,
                               tenant_id: int | None = None, first_name=None, last_name=None, full_name=None,
                               company=None, tags=None, source_channel="sms", source_phone_number=None,
                               source_provider="twilio", source_context=None, sms_opt_in=None,
                               email_opt_in=None, preserve_opt_out=True) -> Contact:
    now = datetime.utcnow()
    norm = normalize_phone(phone)
    q = Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True))
    contact = None
    if norm:
        contact = q.filter(or_(Contact.normalized_phone == norm, Contact.phone == norm, Contact.phone == phone)).first()
    if not contact and email:
        contact = q.filter(func.lower(Contact.email) == email.strip().lower()).first()
    if not contact:
        contact = Contact(company_id=company_id, tenant_id=tenant_id or company_id, is_active=True, is_subscribed=True, created_at=now)
        db.session.add(contact)
    if not contact.phone and norm:
        contact.phone = norm
    if norm and not contact.normalized_phone:
        contact.normalized_phone = norm
    if email and (not contact.email or "@" not in contact.email):
        contact.email = email.strip()
    if full_name and not contact.name:
        contact.name = full_name.strip()
    if first_name and not contact.first_name:
        contact.first_name = first_name.strip()
    if last_name and not contact.last_name:
        contact.last_name = last_name.strip()
    if company and not contact.company:
        contact.company = company.strip()
    for tag in _split_tags(tags):
        add_contact_tag(contact, tag)
    contact.source_channel = source_channel or contact.source_channel
    contact.source_provider = source_provider or contact.source_provider
    contact.source_context = source_context or contact.source_context
    if source_phone_number:
        contact.source_phone_number = normalize_phone(source_phone_number)
    contact.source = contact.source or source_channel
    contact.source_detail = contact.source_detail or source_context
    contact.first_seen_at = contact.first_seen_at or now
    contact.last_seen_at = now
    contact.tenant_id = contact.tenant_id or tenant_id or company_id
    if sms_opt_in is True and not (preserve_opt_out and (contact.sms_opted_out or contact.do_not_sms or contact.sms_opt_out_at)):
        contact.sms_marketing_opt_in = True; contact.sms_consent_status = "opted_in"; contact.sms_marketing_opt_in_at = contact.sms_marketing_opt_in_at or now
    elif sms_opt_in is False:
        contact.sms_marketing_opt_in = False
    if email_opt_in is True and not (preserve_opt_out and (contact.email_unsubscribed or contact.do_not_email)):
        contact.email_opt_in = True; contact.email_subscribed = True; contact.email_unsubscribed = False
    elif email_opt_in is False:
        contact.email_opt_in = False
    apply_source_tags(contact, source_phone_number)
    db.session.flush()
    return contact


def _valid_sms(contact):
    return bool(normalize_phone(contact.normalized_phone or contact.phone)) and not (contact.sms_opted_out or contact.do_not_sms or contact.sms_opt_out_at)


def _condition(contact, c):
    field=(c.get('field') or c.get('property') or '').lower(); op=(c.get('operator') or 'is').lower(); val=c.get('value')
    tags=[t.lower() for t in _split_tags(contact.tags)]
    actual = {
      'tag': tags, 'source_phone_number': normalize_phone(contact.source_phone_number), 'source_channel': contact.source_channel,
      'customer_type': contact.segment, 'customer_status': contact.segment, 'sms_opt_in': contact.sms_marketing_opt_in,
      'sms_not_opted_out': not (contact.sms_opted_out or contact.do_not_sms or contact.sms_opt_out_at), 'email_opt_in': getattr(contact,'email_opt_in', False) or (not contact.email_unsubscribed and contact.is_subscribed),
      'imported_list': contact.imported_list, 'company': contact.company_id,
    }.get(field)
    if field == 'tag': ok = str(val).lower() in actual
    elif field == 'source_phone_number': ok = actual == normalize_phone(val)
    else: ok = str(actual).lower() == str(val).lower()
    return (not ok) if op in ('is_not','not','!=') else ok


def resolve_segment_contacts(company_id: int, segment=None, audience_filter: dict | None = None):
    q = Contact.query.filter_by(company_id=company_id, is_active=True)
    filters = audience_filter or {}
    if segment:
        seg = Segment.query.filter_by(company_id=company_id, name=segment).first()
        if seg:
            ids=[m.contact_id for m in SegmentMember.query.filter_by(segment_id=seg.id, is_excluded=False).all()]
            q=q.filter(Contact.id.in_(ids or [-1]))
            filters = filters or {'conditions': seg.conditions or [], 'match_mode': seg.match_mode or 'all'}
        else:
            q=q.filter(or_(Contact.segment == segment, Contact.tags.ilike(f"%{segment}%")))
    contacts=q.all()
    conds=filters.get('conditions') or filters.get('include') or []
    excludes=filters.get('exclude') or filters.get('exclude_conditions') or []
    mode=(filters.get('match_mode') or 'all').lower()
    if conds:
        contacts=[c for c in contacts if (any(_condition(c,x) for x in conds) if mode in ('any','or') else all(_condition(c,x) for x in conds))]
    if excludes:
        contacts=[c for c in contacts if not any(_condition(c,x) for x in excludes)]
    return contacts


def build_sms_recipient_snapshot(campaign):
    matched=resolve_segment_contacts(campaign.company_id, campaign.segment, campaign.audience_filter or {})
    seen=set(); counts={"total_matched":len(matched),"duplicates_removed":0,"invalid_numbers_removed":0,"opt_outs_removed":0,"final_recipients":0}; final=[]
    for c in matched:
        phone=normalize_phone(c.normalized_phone or c.phone)
        if not phone: counts['invalid_numbers_removed']+=1; continue
        lowered = [t.lower() for t in _split_tags(c.tags)]
        if c.sms_opted_out or c.do_not_sms or c.sms_opt_out_at or 'sms_opt_out' in lowered or 'blocked' in lowered or 'no_sms' in lowered:
            counts['opt_outs_removed']+=1; continue
        if phone in seen: counts['duplicates_removed']+=1; continue
        seen.add(phone); final.append((c,phone))
    SMSRecipient.query.filter_by(company_id=campaign.company_id, campaign_id=campaign.id).delete()
    for c, phone in final:
        db.session.add(SMSRecipient(company_id=campaign.company_id, campaign_id=campaign.id, contact_id=c.id, phone_number=phone, status='queued'))
    counts['final_recipients']=len(final); campaign.estimated_recipient_count=len(final)
    if not final:
        counts['explanation'] = 'No recipients found because all matched contacts are missing SMS numbers or are opted out.' if matched else 'No recipients found because the selected audience did not match any contacts.'
    return counts

HEADER_ALIASES={
 'first_name':['first name','firstname'], 'last_name':['last name','lastname'], 'full_name':['name','full name'], 'company':['company','company name'],
 'email':['email address','email','e-mail address'], 'phone':['phone','mobile phone','cell phone','home phone','work phone','phone 1 - value'],
 'tags':['tags','groups','lists'], 'source':['source','lifecycle stage','lead status'], 'created_at':['create date','created date'], 'notes':['notes']}

def detect_contact_mapping(headers):
    lower={h.strip().lower():h for h in headers}; mapping={}
    for field, aliases in HEADER_ALIASES.items():
        for a in aliases:
            if a in lower: mapping[field]=lower[a]; break
    return mapping


def _parse_bool(v):
    return str(v or '').strip().lower() in {'1','true','yes','y','subscribed','opted in','opt-in'}


def valid_email(email):
    addr = parseaddr(email or '')[1]
    return addr if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', addr or '') else ''


def preview_contact_import(file_bytes: bytes, filename: str):
    name=(filename or '').lower()
    if name.endswith('.xlsx'):
        from openpyxl import load_workbook
        wb=load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True); ws=wb.active
        rows=list(ws.iter_rows(values_only=True)); headers=[str(x or '').strip() for x in (rows[0] if rows else [])]
        sample=[dict(zip(headers, r)) for r in rows[1:6]]
    else:
        text=file_bytes.decode('utf-8-sig')
        dialect=csv.excel_tab if name.endswith('.tsv') else csv.Sniffer().sniff(text[:2048], delimiters=',\t;')
        reader=csv.DictReader(io.StringIO(text), dialect=dialect); headers=reader.fieldnames or []; sample=[r for _,r in zip(range(5), reader)]
    return {'headers': headers, 'detected_mapping': detect_contact_mapping(headers), 'sample_rows': sample}


def import_contacts(company_id:int, file_bytes:bytes, filename:str, *, mapping=None, source_provider='generic_csv', imported_list=None, apply_tags=None, sms_subscribed=False, email_subscribed=False, tenant_id=None):
    from models import ContactImportBatch, Segment, SegmentMember
    preview=preview_contact_import(file_bytes, filename); mapping=mapping or preview['detected_mapping']
    batch=ContactImportBatch(company_id=company_id, tenant_id=tenant_id or company_id, source_provider=source_provider, filename=filename, imported_list=imported_list, applied_tags=apply_tags or [], field_mapping=mapping)
    db.session.add(batch); db.session.flush()
    name=(filename or '').lower(); rows=[]
    if name.endswith('.xlsx'):
        from openpyxl import load_workbook
        ws=load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True).active
        vals=list(ws.iter_rows(values_only=True)); headers=[str(x or '').strip() for x in vals[0]] if vals else []
        rows=[dict(zip(headers, r)) for r in vals[1:]]
    else:
        text=file_bytes.decode('utf-8-sig'); dialect=csv.excel_tab if name.endswith('.tsv') else csv.Sniffer().sniff(text[:2048], delimiters=',\t;')
        rows=list(csv.DictReader(io.StringIO(text), dialect=dialect))
    errors=[]; ok=0
    segment=None
    if imported_list:
        segment=Segment.query.filter_by(company_id=company_id, name=imported_list).first() or Segment(company_id=company_id, name=imported_list, segment_type='imported_list', match_mode='all')
        db.session.add(segment); db.session.flush()
    for idx,row in enumerate(rows, start=2):
        batch.total_rows += 1
        def get(field): return row.get(mapping.get(field,'')) if mapping.get(field) else None
        phone=normalize_phone(get('phone')); email=valid_email(get('email'))
        if not phone and not email:
            errors.append({'row':idx,'error':'missing valid phone or email'}); continue
        try:
            contact=upsert_contact_from_source(company_id, phone or None, email or None, tenant_id=tenant_id or company_id, first_name=get('first_name'), last_name=get('last_name'), full_name=get('full_name'), company=get('company'), tags=_split_tags(get('tags')) + _split_tags(apply_tags), source_channel='csv_import', source_provider=source_provider, source_context='campaign_import', sms_opt_in=_parse_bool(get('sms_opt_in')) or bool(sms_subscribed), email_opt_in=_parse_bool(get('email_opt_in')) or bool(email_subscribed))
            contact.imported_batch_id=batch.id; contact.imported_list=imported_list or contact.imported_list
            if segment and not SegmentMember.query.filter_by(segment_id=segment.id, contact_id=contact.id).first():
                db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id, source='import'))
            ok += 1
        except Exception as exc:
            errors.append({'row':idx,'error':str(exc)})
    batch.success_count=ok; batch.failure_count=len(errors); batch.error_report=errors
    db.session.flush()
    return {'batch': batch, 'success_count': ok, 'failure_count': len(errors), 'errors': errors, 'mapping': mapping}
