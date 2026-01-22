"""Main blueprint routes - placeholder for dashboard and core routes."""
import csv
import hashlib
import io
from datetime import date, datetime, timezone

from flask import Blueprint, current_app, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from lux.analytics.query_service import AnalyticsQueryService
from lux.analytics.time_range import parse_time_range
from lux.extensions import db
from lux.models.analytics import (
    AgentReport,
    AnalyticsDailyRollup,
    ConsentSuppressed,
    IntegrationStatus,
    RawEvent,
    Session,
)
from agents.executive_strategy_agent import ExecutiveStrategyAgent

main_bp = Blueprint('main', __name__, template_folder='../../templates')


@main_bp.route('/')
def index():
    """Landing page."""
    return render_template('index.html')


@main_bp.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard."""
    return render_template('dashboard.html')


def _hash_value(value: str) -> str:
    salt = current_app.config.get("SECRET_KEY", "dev-secret")
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


def _update_rollup(company_id: int, day_value: date, event_name: str, session_seen: bool) -> None:
    rollup = (
        AnalyticsDailyRollup.query.filter_by(company_id=company_id, day=day_value).first()
    )
    if not rollup:
        rollup = AnalyticsDailyRollup(
            company_id=company_id,
            day=day_value,
            total_events=0,
            page_views=0,
            sessions=0,
        )
        db.session.add(rollup)
    rollup.total_events += 1
    if event_name == "page_view":
        rollup.page_views += 1
    if session_seen:
        rollup.sessions += 1


@main_bp.route("/e", methods=["POST"])
def collect_event():
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        company_id = None
    if not company_id:
        return jsonify({"error": "company_id required"}), 400

    consent = payload.get("consent", False)
    gpc = request.headers.get("Sec-GPC") == "1"
    if not consent or gpc:
        today = date.today()
        suppressed = ConsentSuppressed.query.filter_by(company_id=company_id, day=today).first()
        if not suppressed:
            suppressed = ConsentSuppressed(company_id=company_id, day=today, count=0)
            db.session.add(suppressed)
        suppressed.count += 1
        db.session.commit()
        return ("", 204)

    event_name = payload.get("event_name") or "event"
    occurred_at = datetime.now(timezone.utc)
    session_id = payload.get("session_id")
    user_id = payload.get("user_id")
    page_url = payload.get("page_url")
    referrer = payload.get("referrer")
    utm_source = payload.get("utm_source")
    utm_medium = payload.get("utm_medium")
    utm_campaign = payload.get("utm_campaign")
    properties = payload.get("properties", {})
    email = payload.get("email")
    email_hash = _hash_value(email.lower()) if email else None
    ip_hash = _hash_value(request.remote_addr) if request.remote_addr else None
    device_type = payload.get("device_type")
    viewport_width = payload.get("viewport_width")
    orientation = payload.get("orientation")

    raw_event = RawEvent(
        company_id=company_id,
        event_name=event_name,
        occurred_at=occurred_at,
        session_id=session_id,
        user_id=user_id,
        page_url=page_url,
        referrer=referrer,
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
        properties=properties,
        ip_hash=ip_hash,
        email_hash=email_hash,
        device_type=device_type,
        viewport_width=viewport_width,
        orientation=orientation,
    )
    db.session.add(raw_event)

    session_seen = False
    if session_id:
        session_record = Session.query.filter_by(company_id=company_id, session_id=session_id).first()
        if not session_record:
            session_record = Session(
                company_id=company_id,
                session_id=session_id,
                user_id=user_id,
                started_at=occurred_at,
                last_seen_at=occurred_at,
            )
            db.session.add(session_record)
            session_seen = True
        else:
            session_record.last_seen_at = occurred_at

    _update_rollup(company_id, occurred_at.date(), event_name, session_seen)
    db.session.commit()
    return ("", 204)


@main_bp.route("/analytics/report")
@login_required
def analytics_report():
    company_id = request.args.get("company_id", type=int)
    if not company_id and getattr(current_user, "get_default_company", None):
        company = current_user.get_default_company()
        company_id = company.id if company else None
    if not company_id:
        flash("company_id required", "error")
        return redirect(url_for("main.dashboard"))

    range_preset = request.args.get("range", "last_month")
    compare_preset = request.args.get("compare", "previous_period")
    custom_start = request.args.get("start")
    custom_end = request.args.get("end")
    compare_start = request.args.get("compare_start")
    compare_end = request.args.get("compare_end")

    def _parse_date(value):
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None

    range_result = parse_time_range(
        range_preset,
        compare_preset,
        custom_start=_parse_date(custom_start),
        custom_end=_parse_date(custom_end),
        compare_start=_parse_date(compare_start),
        compare_end=_parse_date(compare_end),
    )
    summary = AnalyticsQueryService.summary(company_id, range_result.start, range_result.end)
    events_by_day = AnalyticsQueryService.events_by_day(company_id, range_result.start, range_result.end)
    sessions_by_day = AnalyticsQueryService.sessions_by_day(company_id, range_result.start, range_result.end)

    return render_template(
        "analytics_report.html",
        summary=summary,
        events_by_day=events_by_day,
        sessions_by_day=sessions_by_day,
        range_result=range_result,
        range_preset=range_preset,
        compare_preset=compare_preset,
        company_id=company_id,
    )


@main_bp.route("/analytics/report/print")
@login_required
def analytics_report_print():
    company_id = request.args.get("company_id", type=int)
    if not company_id and getattr(current_user, "get_default_company", None):
        company = current_user.get_default_company()
        company_id = company.id if company else None
    if not company_id:
        return redirect(url_for("main.dashboard"))

    range_preset = request.args.get("range", "last_month")
    compare_preset = request.args.get("compare", "previous_period")
    range_result = parse_time_range(range_preset, compare_preset)
    summary = AnalyticsQueryService.summary(company_id, range_result.start, range_result.end)

    return render_template(
        "analytics_report_print.html",
        summary=summary,
        range_result=range_result,
        generated_at=datetime.utcnow(),
    )


@main_bp.route("/analytics/report/export/<string:fmt>")
@login_required
def analytics_report_export(fmt):
    company_id = request.args.get("company_id", type=int)
    if not company_id and getattr(current_user, "get_default_company", None):
        company = current_user.get_default_company()
        company_id = company.id if company else None
    if not company_id:
        return redirect(url_for("main.dashboard"))

    range_preset = request.args.get("range", "last_month")
    compare_preset = request.args.get("compare", "previous_period")
    range_result = parse_time_range(range_preset, compare_preset)
    summary = AnalyticsQueryService.summary(company_id, range_result.start, range_result.end)

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["LUX Marketing Analytics Report"])
        writer.writerow(["Range", f"{range_result.start.date()} to {range_result.end.date()}"])
        writer.writerow([])
        writer.writerow(["Total Events", summary["total_events"]])
        writer.writerow(["Total Sessions", summary["total_sessions"]])
        writer.writerow(["Consent Suppressed", summary["consent_suppressed"]])
        writer.writerow([])
        writer.writerow(["Top Pages", "Hits"])
        for row in summary["top_pages"]:
            writer.writerow([row["page"], row["hits"]])
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=analytics_report.csv"
        response.headers["Content-Type"] = "text/csv"
        return response

    if fmt == "excel":
        try:
            from openpyxl import Workbook
        except ImportError:
            flash("Excel export requires openpyxl package", "error")
            return redirect(url_for("main.analytics_report"))
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Analytics Report"
        sheet.append(["LUX Marketing Analytics Report"])
        sheet.append(["Range", f"{range_result.start.date()} to {range_result.end.date()}"])
        sheet.append([])
        sheet.append(["Total Events", summary["total_events"]])
        sheet.append(["Total Sessions", summary["total_sessions"]])
        sheet.append(["Consent Suppressed", summary["consent_suppressed"]])
        sheet.append([])
        sheet.append(["Top Pages", "Hits"])
        for row in summary["top_pages"]:
            sheet.append([row["page"], row["hits"]])
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=analytics_report.xlsx"
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return response

    if fmt == "pdf":
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfgen import canvas
        except ImportError:
            flash("PDF export requires reportlab package", "error")
            return redirect(url_for("main.analytics_report"))
        output = io.BytesIO()
        c = canvas.Canvas(output, pagesize=letter)
        text = c.beginText(40, 750)
        text.textLine("LUX Marketing Analytics Report")
        text.textLine(f"Range: {range_result.start.date()} to {range_result.end.date()}")
        text.textLine("")
        text.textLine(f"Total Events: {summary['total_events']}")
        text.textLine(f"Total Sessions: {summary['total_sessions']}")
        text.textLine(f"Consent Suppressed: {summary['consent_suppressed']}")
        text.textLine("")
        text.textLine("Top Pages:")
        for row in summary["top_pages"]:
            text.textLine(f"- {row['page']} ({row['hits']})")
        c.drawText(text)
        c.showPage()
        c.save()
        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=analytics_report.pdf"
        response.headers["Content-Type"] = "application/pdf"
        return response

    return redirect(url_for("main.analytics_report"))


@main_bp.route("/api/analytics/summary")
@login_required
def analytics_summary_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id and getattr(current_user, "get_default_company", None):
        company = current_user.get_default_company()
        company_id = company.id if company else None
    if not company_id:
        return jsonify({"error": "company_id required"}), 400
    range_preset = request.args.get("range", "last_month")
    compare_preset = request.args.get("compare", "previous_period")
    range_result = parse_time_range(range_preset, compare_preset)
    summary = AnalyticsQueryService.summary(company_id, range_result.start, range_result.end)
    return jsonify({"summary": summary})


@main_bp.route("/api/system/integrations-status")
@login_required
def integrations_status():
    company_id = request.args.get("company_id", type=int)
    if not company_id and getattr(current_user, "get_default_company", None):
        company = current_user.get_default_company()
        company_id = company.id if company else None
    is_admin = getattr(current_user, "is_admin_user", False) or getattr(current_user, "is_admin", False)
    if not company_id or not is_admin:
        return jsonify({"error": "admin access required"}), 403
    records = IntegrationStatus.query.filter_by(company_id=company_id).all()
    payload = [
        {
            "integration": record.integration_name,
            "configured": record.is_configured,
            "last_success_at": record.last_success_at.isoformat() if record.last_success_at else None,
            "last_webhook_at": record.last_webhook_at.isoformat() if record.last_webhook_at else None,
            "error_count_24h": record.error_count_24h,
        }
        for record in records
    ]
    return jsonify({"company_id": company_id, "integrations": payload})


@main_bp.route("/api/agents/executive/generate", methods=["POST"])
@login_required
def generate_executive_report():
    company_id = request.args.get("company_id", type=int)
    if not company_id and getattr(current_user, "get_default_company", None):
        company = current_user.get_default_company()
        company_id = company.id if company else None
    is_admin = getattr(current_user, "is_admin_user", False) or getattr(current_user, "is_admin", False)
    if not company_id or not is_admin:
        return jsonify({"error": "admin access required"}), 403
    range_preset = request.args.get("range", "last_month")
    compare_preset = request.args.get("compare", "previous_period")
    range_result = parse_time_range(range_preset, compare_preset)
    agent = ExecutiveStrategyAgent()
    report_payload = agent.build_report(
        company_id,
        range_result.start,
        range_result.end,
        range_result.compare_start,
        range_result.compare_end,
    )
    report = AgentReport(
        company_id=company_id,
        agent_type=agent.agent_type,
        period_start=range_result.start,
        period_end=range_result.end,
        compare_start=range_result.compare_start,
        compare_end=range_result.compare_end,
        content=report_payload,
    )
    db.session.add(report)
    db.session.commit()
    return jsonify({"report_id": report.id, "content": report_payload})
