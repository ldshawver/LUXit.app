"""Durable, tenant-scoped SMS-to-Tuya notification operations."""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    TuyaNotificationActivation,
    TuyaNotificationEvent,
    TuyaNotificationWorkerHeartbeat,
)
from services.tuya_client import TuyaClient, TuyaConfig, TuyaError

logger = logging.getLogger(__name__)
MAX_RETRIES = 5
RECONCILIATION_INTERVAL_SECONDS = 1
WORKER_HEALTHY_SECONDS = 45
WORKER_STALE_SECONDS = 120
OPERATION_LEASE_SECONDS = 30
_client = None
_warned_configuration = False
_worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_worker_started_at = datetime.now(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def configured(*, require_enabled: bool = True) -> bool:
    enabled = os.getenv("TUYA_NOTIFICATION_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    try:
        timeout_valid = float(os.getenv("TUYA_HTTP_TIMEOUT_SECONDS", "10")) > 0
        duration_valid = int(os.getenv("TUYA_NOTIFICATION_DURATION_SECONDS", "60")) > 0
    except ValueError:
        timeout_valid = duration_valid = False
    complete = all(
        (
            os.getenv("TUYA_ACCESS_ID"),
            os.getenv("TUYA_ACCESS_SECRET"),
            os.getenv("TUYA_NOTIFICATION_DEVICE_ID"),
            timeout_valid,
            duration_valid,
        )
    )
    return complete and (enabled or not require_enabled)


def _config(*, require_enabled: bool = True):
    global _warned_configuration
    if not configured(require_enabled=require_enabled):
        if (
            os.getenv("TUYA_NOTIFICATION_ENABLED", "false").lower()
            in {"1", "true", "yes"}
            and not _warned_configuration
        ):
            logger.warning(
                "Tuya notification integration skipped: configuration incomplete"
            )
            _warned_configuration = True
        return None
    try:
        timeout = float(os.getenv("TUYA_HTTP_TIMEOUT_SECONDS", "10"))
    except ValueError:
        if not _warned_configuration:
            logger.warning(
                "Tuya notification integration skipped: invalid timeout configuration"
            )
            _warned_configuration = True
        return None
    return TuyaConfig(
        base_url=os.getenv("TUYA_BASE_URL", "https://openapi.tuyaus.com"),
        access_id=os.environ["TUYA_ACCESS_ID"],
        access_secret=os.environ["TUYA_ACCESS_SECRET"],
        device_id=os.environ["TUYA_NOTIFICATION_DEVICE_ID"],
        switch_code=os.getenv("TUYA_NOTIFICATION_SWITCH_CODE", "switch_1"),
        timeout_seconds=timeout,
    )


def get_client(*, require_enabled: bool = True):
    global _client
    config = _config(require_enabled=require_enabled)
    if not config:
        return None
    if _client is None or _client.config != config:
        _client = TuyaClient(config)
    return _client


def _normalize(number: str) -> str:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return (number or "").strip()


def destination() -> str:
    return _normalize(os.getenv("TUYA_NOTIFICATION_PHONE", "+19165989519"))


def accepts_destination(to_number: str) -> bool:
    return configured() and _normalize(to_number) == destination()


def company_owns_integration(company_id: int) -> bool:
    """Bind the single environment-managed device to the owner of its Twilio number."""
    from models import TwilioAccount, TwilioPhoneNumber

    target = destination()
    owned = any(
        _normalize(row.phone_number) == target
        for row in TwilioPhoneNumber.query.filter_by(
            company_id=company_id, is_active=True
        ).all()
    )
    if owned:
        return True
    return any(
        _normalize(row.from_phone) == target
        for row in TwilioAccount.query.filter_by(company_id=company_id).all()
    )


def _masked_phone(number: str | None) -> str | None:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else None


def accept_inbound(
    company_id: int,
    message_sid: str,
    to_number: str,
    now=None,
    *,
    customer_phone: str | None = None,
) -> bool:
    if (
        not message_sid
        or not accepts_destination(to_number)
        or not company_owns_integration(company_id)
    ):
        return False
    return queue_pulse(
        company_id, message_sid, "inbound_sms", now=now, customer_phone=customer_phone
    )


def queue_pulse(
    company_id: int,
    event_id: str,
    source: str,
    *,
    now=None,
    customer_phone=None,
    user_id=None,
    _retry_activation_race=True,
) -> bool:
    """Persist the work before returning; this activation row is the durable queue."""
    now = now or utcnow()
    duration = max(1, int(os.getenv("TUYA_NOTIFICATION_DURATION_SECONDS", "60")))
    try:
        activation = (
            TuyaNotificationActivation.query.filter_by(company_id=company_id)
            .with_for_update()
            .first()
        )
        if not activation:
            activation = TuyaNotificationActivation(company_id=company_id)
            db.session.add(activation)
            db.session.flush()
        if source == "manual_test":
            recent = TuyaNotificationEvent.query.filter(
                TuyaNotificationEvent.company_id == company_id,
                TuyaNotificationEvent.trigger_source == "manual_test",
                TuyaNotificationEvent.received_at >= now - timedelta(minutes=1),
            ).first()
            if recent:
                db.session.rollback()
                return False
        deadline = now + timedelta(seconds=duration)
        event = TuyaNotificationEvent(
            company_id=company_id,
            message_sid=event_id,
            activation_id=activation.id,
            trigger_source=source,
            customer_phone_masked=_masked_phone(customer_phone),
            requested_state=True,
            result="queued",
            off_deadline=deadline,
            initiated_by_user_id=user_id,
            received_at=now,
        )
        db.session.add(event)
        activation.desired_state = True
        activation.off_requested = False
        activation.off_deadline = deadline
        activation.last_event_sid = event_id
        activation.last_operation = "on"
        activation.last_operation_status = "queued"
        activation.retry_count = 0
        activation.last_error = None
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        duplicate = TuyaNotificationEvent.query.filter_by(
            company_id=company_id, message_sid=event_id
        ).first()
        if not duplicate and _retry_activation_race:
            return queue_pulse(
                company_id,
                event_id,
                source,
                now=now,
                customer_phone=customer_phone,
                user_id=user_id,
                _retry_activation_race=False,
            )
        return False
    return True


def queue_manual_test(company_id: int, user_id: int, now=None) -> bool:
    if not configured() or not company_owns_integration(company_id):
        return False
    now = now or utcnow()
    return queue_pulse(
        company_id,
        f"manual:{uuid.uuid4().hex}",
        "manual_test",
        now=now,
        user_id=user_id,
    )


def queue_manual_off(company_id: int, user_id: int, now=None) -> bool:
    now = now or utcnow()
    activation = (
        TuyaNotificationActivation.query.filter_by(company_id=company_id)
        .with_for_update()
        .first()
    )
    if not activation:
        db.session.rollback()
        return False
    activation.desired_state = True  # remains safety-pending until confirmed OFF
    activation.off_requested = True
    activation.off_deadline = now
    activation.last_operation = "off"
    activation.last_operation_status = "queued"
    activation.retry_count = 0
    event = TuyaNotificationEvent(
        company_id=company_id,
        message_sid=f"manual-off:{uuid.uuid4().hex}",
        activation_id=activation.id,
        trigger_source="manual_off",
        requested_state=False,
        result="queued",
        off_deadline=now,
        initiated_by_user_id=user_id,
        received_at=now,
    )
    db.session.add(event)
    activation.last_event_sid = event.message_sid
    db.session.commit()
    return True


def _latest_event(activation, requested_state):
    return (
        TuyaNotificationEvent.query.filter_by(
            activation_id=activation.id, requested_state=requested_state
        )
        .order_by(TuyaNotificationEvent.received_at.desc())
        .first()
    )


def _failure(activation, operation: str, exc: Exception, now: datetime):
    activation.last_operation = operation
    activation.last_operation_status = "failed"
    activation.last_error = str(exc)[:500]
    activation.retry_count = min(MAX_RETRIES, (activation.retry_count or 0) + 1)
    activation.last_attempt_at = now
    event = _latest_event(activation, operation == "on")
    if event:
        event.result = "failed"
        event.attempts = activation.retry_count
        event.sanitized_error = activation.last_error
    db.session.commit()


def run_on(activation_id: int, now=None) -> bool:
    now = now or utcnow()
    activation = (
        TuyaNotificationActivation.query.filter_by(id=activation_id)
        .with_for_update()
        .first()
    )
    if not activation or not activation.desired_state:
        db.session.rollback()
        return True
    if (activation.retry_count or 0) >= MAX_RETRIES:
        db.session.rollback()
        return False
    if (
        activation.off_requested
        or not activation.off_deadline
        or as_utc(activation.off_deadline) <= now
    ):
        db.session.rollback()
        return run_off_check(activation_id, now=now)
    if (
        activation.last_operation == "on"
        and activation.last_operation_status == "succeeded"
    ):
        db.session.rollback()
        return True
    if (
        activation.last_operation_status == "in_progress"
        and activation.last_attempt_at
        and as_utc(activation.last_attempt_at)
        + timedelta(seconds=OPERATION_LEASE_SECONDS)
        > now
    ):
        db.session.rollback()
        return True
    client = get_client()
    if not client:
        db.session.rollback()
        return False
    activation.last_operation = "on"
    activation.last_operation_status = "in_progress"
    activation.last_attempt_at = now
    db.session.commit()  # release the claim lock before bounded provider I/O
    try:
        client.set_switch(True)
        activation = (
            TuyaNotificationActivation.query.filter_by(id=activation_id)
            .with_for_update()
            .first()
        )
        if not activation:
            db.session.rollback()
            return True
        manual_off_pending = activation.off_requested
        activation.last_operation = "on"
        activation.last_operation_status = "succeeded"
        activation.last_error = None
        activation.retry_count = 0
        activation.last_attempt_at = now
        activation.last_on_at = now
        event = _latest_event(activation, True)
        if event:
            event.result = "succeeded"
            event.attempts += 1
        db.session.commit()
        if manual_off_pending:
            return run_off_check(activation_id, now=utcnow())
        return True
    except TuyaError as exc:
        activation = (
            TuyaNotificationActivation.query.filter_by(id=activation_id)
            .with_for_update()
            .first()
        )
        if not activation:
            db.session.rollback()
            return False
        _failure(activation, "on", exc, now)
        logger.error(
            "Tuya ON failed activation_id=%s retry=%s",
            activation.id,
            activation.retry_count,
        )
        return False


def run_off_check(activation_id: int, now=None) -> bool:
    now = now or utcnow()
    activation = (
        TuyaNotificationActivation.query.filter_by(id=activation_id)
        .with_for_update()
        .first()
    )
    if not activation or not activation.desired_state:
        db.session.rollback()
        return True
    if (activation.retry_count or 0) >= MAX_RETRIES:
        db.session.rollback()
        return False
    if (
        not activation.off_requested
        and activation.off_deadline
        and as_utc(activation.off_deadline) > now
    ):
        db.session.rollback()
        return False
    if (
        activation.last_operation == "off"
        and activation.last_operation_status == "in_progress"
        and activation.last_attempt_at
        and as_utc(activation.last_attempt_at)
        + timedelta(seconds=OPERATION_LEASE_SECONDS)
        > now
    ):
        db.session.rollback()
        return True
    # Safety OFF may use configured credentials even after triggering is disabled.
    client = get_client(require_enabled=False)
    if not client:
        activation.last_operation = "off"
        activation.last_operation_status = "blocked_configuration"
        activation.last_error = "Safety OFF unavailable: Tuya configuration incomplete"
        activation.last_attempt_at = now
        db.session.commit()
        logger.critical(
            "Tuya safety OFF blocked by incomplete configuration activation_id=%s",
            activation.id,
        )
        return False
    claimed_event_sid = activation.last_event_sid
    activation.last_operation = "off"
    activation.last_operation_status = "in_progress"
    activation.last_attempt_at = now
    db.session.commit()  # release the claim lock before bounded provider I/O
    try:
        client.set_switch(False)
        activation = (
            TuyaNotificationActivation.query.filter_by(id=activation_id)
            .with_for_update()
            .first()
        )
        if not activation:
            db.session.rollback()
            return True
        newer_on_pending = (
            activation.last_event_sid != claimed_event_sid
            and not activation.off_requested
            and activation.off_deadline is not None
            and as_utc(activation.off_deadline) > utcnow()
        )
        activation.desired_state = newer_on_pending
        activation.off_requested = False
        activation.last_operation = "on" if newer_on_pending else "off"
        activation.last_operation_status = "queued" if newer_on_pending else "succeeded"
        activation.last_error = None
        activation.retry_count = 0
        activation.last_attempt_at = now
        activation.last_off_at = now
        event = _latest_event(activation, False)
        if event:
            event.result = "succeeded"
            event.attempts += 1
        db.session.commit()
        if newer_on_pending:
            return run_on(activation_id, now=utcnow())
        return True
    except TuyaError as exc:
        activation = (
            TuyaNotificationActivation.query.filter_by(id=activation_id)
            .with_for_update()
            .first()
        )
        if not activation:
            db.session.rollback()
            return False
        _failure(activation, "off", exc, now)
        logger.critical(
            "Tuya OFF failed activation_id=%s retry=%s; relay may remain on",
            activation.id,
            activation.retry_count,
        )
        return False


def heartbeat(now=None) -> None:
    now = now or utcnow()
    row = db.session.get(TuyaNotificationWorkerHeartbeat, _worker_id)
    if not row:
        row = TuyaNotificationWorkerHeartbeat(
            worker_id=_worker_id, started_at=_worker_started_at, last_seen_at=now
        )
        db.session.add(row)
    else:
        row.last_seen_at = now
    db.session.commit()


def worker_health(now=None) -> dict:
    now = now or utcnow()
    rows = TuyaNotificationWorkerHeartbeat.query.order_by(
        TuyaNotificationWorkerHeartbeat.last_seen_at.desc()
    ).all()
    if not rows:
        return {"status": "unknown", "last_seen_at": None, "multiple": False}
    age = max(0, (now - as_utc(rows[0].last_seen_at)).total_seconds())
    status = (
        "healthy"
        if age <= WORKER_HEALTHY_SECONDS
        else ("stale" if age <= WORKER_STALE_SECONDS else "offline")
    )
    active = [
        row
        for row in rows
        if (now - as_utc(row.last_seen_at)).total_seconds() <= WORKER_HEALTHY_SECONDS
    ]
    return {
        "status": status,
        "last_seen_at": rows[0].last_seen_at,
        "multiple": len(active) > 1,
    }


def reconcile(now=None) -> None:
    now = now or utcnow()
    heartbeat(now)
    records = TuyaNotificationActivation.query.filter_by(desired_state=True).all()
    for activation in records:
        activation.last_reconciled_at = now
        if (activation.retry_count or 0) >= MAX_RETRIES:
            logger.critical(
                "Tuya activation requires operator intervention activation_id=%s",
                activation.id,
            )
            db.session.commit()
            continue
        if activation.retry_count and activation.last_attempt_at:
            backoff = min(300, 15 * (2**activation.retry_count))
            if as_utc(activation.last_attempt_at) + timedelta(seconds=backoff) > now:
                db.session.commit()
                continue
        if (
            activation.off_requested
            or not activation.off_deadline
            or as_utc(activation.off_deadline) <= now
        ):
            run_off_check(activation.id, now=now)
        elif configured() and not (
            activation.last_operation == "on"
            and activation.last_operation_status == "succeeded"
        ):
            run_on(activation.id, now=now)
        else:
            db.session.commit()


def masked_configuration() -> dict:
    access_id = os.getenv("TUYA_ACCESS_ID", "")
    device_id = os.getenv("TUYA_NOTIFICATION_DEVICE_ID", "")

    def mask(value):
        return ("•" * 8 + value[-4:]) if value else "Not configured"

    return {
        "enabled": os.getenv("TUYA_NOTIFICATION_ENABLED", "false").lower()
        in {"1", "true", "yes"},
        "complete": configured(require_enabled=False),
        "base_url": os.getenv("TUYA_BASE_URL", "https://openapi.tuyaus.com"),
        "access_id": mask(access_id),
        "secret_status": (
            "Configured" if os.getenv("TUYA_ACCESS_SECRET") else "Not configured"
        ),
        "device_id": mask(device_id),
        "device_label": "Tuya notification relay",
        "switch_code": os.getenv("TUYA_NOTIFICATION_SWITCH_CODE", "switch_1"),
        "phone": destination(),
        "duration": int(os.getenv("TUYA_NOTIFICATION_DURATION_SECONDS", "60")),
        "timeout": os.getenv("TUYA_HTTP_TIMEOUT_SECONDS", "10"),
        "max_retries": MAX_RETRIES,
        "reconciliation_interval": RECONCILIATION_INTERVAL_SECONDS,
    }
