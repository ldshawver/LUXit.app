"""Native Feedback & Bug Reporting module.

Endpoints (all under blueprint ``feedback_bp``):

  GET  /feedback                  — user's own submissions list
  GET  /feedback/admin            — admin dashboard (platform admin OR company admin)
  GET  /feedback/<id>             — ticket detail (visibility-checked)
  POST /api/feedback              — submit a new ticket (multipart for screenshot)
  POST /api/feedback/<id>/comment — add a comment (admin can set is_internal=true)
  POST /api/feedback/<id>/status  — admin only: update status
  POST /api/feedback/<id>/assign  — admin only: assign owner
  POST /api/feedback/<id>/priority — admin only: toggle priority_fix flag

Visibility rules (enforced by ``_can_view_ticket`` / ``_can_admin_ticket``):
  - Platform admin (User.is_admin) — full read/write on all tickets.
  - Company admin (UserCompanyAccess role owner/admin for ticket.company_id)
    — full read/write on tickets in that company.
  - Regular user — read-only access to their own submissions; can comment on
    their own ticket but cannot change status / assignment / priority.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from flask import (
    Blueprint, abort, current_app, jsonify, redirect, render_template,
    request, send_from_directory, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import desc, or_
from werkzeug.utils import secure_filename

from extensions import db
from models import (
    Company, FeedbackTicket, FeedbackTicketComment, User,
)

logger = logging.getLogger(__name__)
feedback_bp = Blueprint("feedback", __name__)

ALLOWED_SCREENSHOT_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024  # 5 MB
# Stored OUTSIDE /static so screenshots are not publicly fetchable —
# served via an authenticated route instead. See `screenshot()` below.
SCREENSHOT_DIR_REL = "uploads/feedback"

# Magic-byte signatures for the image formats we accept. The first 16 bytes of
# the upload must match one of these — extension-only checks are not enough
# (e.g. an executable renamed to `.png`).
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff",       "jpg"),  # JPEG/JFIF/Exif all start with FFD8FF
    (b"GIF87a",             "gif"),
    (b"GIF89a",             "gif"),
    # WebP: "RIFF????WEBP"
)


def _detect_image_kind(head: bytes) -> Optional[str]:
    for sig, kind in _IMAGE_MAGIC:
        if head.startswith(sig):
            return kind
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


# ── Authorization helpers ───────────────────────────────────────────────────

def _is_platform_admin(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and getattr(user, "is_admin", False))


def _is_company_admin(user, company_id: Optional[int]) -> bool:
    """True if user has admin/owner role on the given company."""
    if not (user and getattr(user, "is_authenticated", False) and company_id):
        return False
    if _is_platform_admin(user):
        return True
    try:
        return bool(user.can_admin_company(company_id))
    except Exception:
        return False


def _can_view_ticket(user, ticket: FeedbackTicket) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.id == ticket.user_id:
        return True
    if _is_platform_admin(user):
        return True
    if ticket.company_id and _is_company_admin(user, ticket.company_id):
        return True
    return False


def _can_admin_ticket(user, ticket: FeedbackTicket) -> bool:
    if _is_platform_admin(user):
        return True
    if ticket.company_id and _is_company_admin(user, ticket.company_id):
        return True
    return False


def _admin_companies_for(user) -> list[int]:
    """Return company_ids on which ``user`` has admin/owner role."""
    from models import UserCompanyAccess
    if _is_platform_admin(user):
        return []  # caller treats empty list + is_platform_admin=True as "all"
    rows = (
        UserCompanyAccess.query
        .filter(
            UserCompanyAccess.user_id == user.id,
            UserCompanyAccess.role.in_([
                UserCompanyAccess.ROLE_OWNER,
                UserCompanyAccess.ROLE_ADMIN,
            ]),
        ).all()
    )
    return [r.company_id for r in rows]


# ── Notification helpers ────────────────────────────────────────────────────

def _notify(user_id: int, title: str, message: str, link: str,
            company_id: Optional[int] = None, category: str = "feedback"):
    """Best-effort in-app notification + optional email. Never raises."""
    try:
        from routes import create_notification
        create_notification(
            user_id=user_id,
            title=title, message=message,
            category=category, icon="message-circle",
            link=link, company_id=company_id,
        )
    except Exception:
        logger.warning("feedback._notify: in-app notification failed for user=%s", user_id, exc_info=True)

    # Best-effort email — only if EmailService is wired up. Failures must not
    # block ticket submission or status changes.
    try:
        u = User.query.get(user_id)
        if not u or not getattr(u, "email", None):
            return
        from email_service import EmailService  # type: ignore
        EmailService().send_email(
            u.email, f"[LUX] {title}",
            f"<p>{message}</p><p><a href='{link}'>View ticket</a></p>",
        )
    except Exception:
        logger.debug("feedback._notify: email send skipped/failed", exc_info=True)


def _notify_admins_on_submit(ticket: FeedbackTicket):
    """Ping platform admins + company admins about a new ticket."""
    from models import UserCompanyAccess
    recipients: set[int] = set()
    try:
        for u in User.query.filter_by(is_admin=True).all():
            if u.id != ticket.user_id:
                recipients.add(u.id)
    except Exception:
        logger.warning("feedback: lookup of platform admins failed", exc_info=True)
    if ticket.company_id:
        try:
            rows = UserCompanyAccess.query.filter(
                UserCompanyAccess.company_id == ticket.company_id,
                UserCompanyAccess.role.in_([
                    UserCompanyAccess.ROLE_OWNER, UserCompanyAccess.ROLE_ADMIN,
                ]),
            ).all()
            for r in rows:
                if r.user_id != ticket.user_id:
                    recipients.add(r.user_id)
        except Exception:
            logger.warning("feedback: company-admin lookup failed", exc_info=True)

    link = url_for("feedback.ticket_detail", ticket_id=ticket.id)
    title = f"New {ticket.ticket_type.replace('_', ' ')}: {ticket.title}"
    msg = f"Severity: {ticket.severity}. Submitted by user #{ticket.user_id}."
    for uid in recipients:
        _notify(uid, title, msg, link, company_id=ticket.company_id)


# ── Screenshot upload ───────────────────────────────────────────────────────

def _screenshot_storage_root() -> str:
    """Return the absolute root directory where screenshots are stored.

    Lives under Flask's ``instance_path`` (NOT ``/static``) so files are not
    served by the static handler — they must go through ``screenshot()``.
    """
    return os.path.join(current_app.instance_path, SCREENSHOT_DIR_REL)


def _save_screenshot(file_storage, ticket_id: int) -> Optional[str]:
    """Persist an uploaded screenshot. Returns a relative ticket-scoped path
    (e.g. ``"42/abc.png"``) or None on rejection. The file is stored under
    Flask's instance path and is NOT publicly fetchable."""
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    name = secure_filename(file_storage.filename or "")
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_SCREENSHOT_EXT:
        return None

    # Enforce a hard size cap.
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size <= 0 or size > MAX_SCREENSHOT_BYTES:
        return None

    # Magic-byte validation: extension can lie. Read the first 16 bytes,
    # confirm they match a known image header, AND that the detected kind
    # is consistent with the claimed extension (jpeg/jpg are aliases).
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    kind = _detect_image_kind(head)
    if kind is None:
        return None
    if not (kind == ext or (kind == "jpg" and ext in ("jpg", "jpeg"))):
        return None

    abs_target_dir = os.path.join(_screenshot_storage_root(), str(ticket_id))
    os.makedirs(abs_target_dir, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(abs_target_dir, safe_name))
    # Return a ticket-scoped relative path; absolute disk path is rebuilt
    # at serve time from `_screenshot_storage_root()` so we can never be
    # tricked into serving outside the upload root.
    return f"{ticket_id}/{safe_name}"


# ── Submit + list ───────────────────────────────────────────────────────────

@feedback_bp.route("/api/feedback", methods=["POST"])
@login_required
def submit_ticket():
    """Create a new feedback ticket. Accepts multipart (with optional screenshot)
    or JSON. The submitter's default company becomes the ticket's tenant."""
    if request.is_json:
        body = request.get_json(silent=True) or {}
        screenshot = None
    else:
        body = request.form.to_dict()
        screenshot = request.files.get("screenshot")

    ticket_type = (body.get("ticket_type") or "general").strip().lower()
    if ticket_type not in FeedbackTicket.TYPES:
        return jsonify({"error": "invalid ticket_type",
                        "allowed": list(FeedbackTicket.TYPES)}), 400
    severity = (body.get("severity") or "medium").strip().lower()
    if severity not in FeedbackTicket.SEVERITIES:
        return jsonify({"error": "invalid severity",
                        "allowed": list(FeedbackTicket.SEVERITIES)}), 400
    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    if not title or not description:
        return jsonify({"error": "title and description are required"}), 400
    if len(title) > 200:
        title = title[:200]

    page_url = (body.get("page_url") or "").strip() or None
    user_agent = (request.headers.get("User-Agent") or body.get("user_agent") or "").strip() or None

    company = None
    try:
        company = current_user.get_default_company()
    except Exception:
        company = None

    ticket = FeedbackTicket(
        ticket_type=ticket_type,
        title=title, description=description,
        page_url=page_url, user_agent=user_agent,
        severity=severity, status="new",
        user_id=current_user.id,
        company_id=(company.id if company else None),
    )
    db.session.add(ticket)
    db.session.flush()  # assign id for screenshot dir

    if screenshot is not None:
        try:
            rel = _save_screenshot(screenshot, ticket.id)
            if rel:
                ticket.screenshot_path = rel
        except Exception:
            logger.exception("feedback.submit: screenshot save failed")

    db.session.commit()
    _notify_admins_on_submit(ticket)
    return jsonify({"ok": True, "ticket": ticket.to_dict()}), 201


@feedback_bp.route("/feedback")
@login_required
def my_tickets():
    """Regular users see their own submissions. Admins see a button to the
    full admin dashboard."""
    tickets = (
        FeedbackTicket.query
        .filter_by(user_id=current_user.id)
        .order_by(desc(FeedbackTicket.created_at))
        .all()
    )
    is_any_admin = _is_platform_admin(current_user) or bool(_admin_companies_for(current_user))
    return render_template("feedback/list.html",
                           tickets=tickets, is_any_admin=is_any_admin)


# ── Admin dashboard ─────────────────────────────────────────────────────────

@feedback_bp.route("/feedback/admin")
@login_required
def admin_dashboard():
    """Filterable admin dashboard.

    Platform admins see everything. Company admins see only tickets for the
    companies they admin. Anyone else is sent to their own list.
    """
    is_platform = _is_platform_admin(current_user)
    admin_company_ids = _admin_companies_for(current_user)
    if not is_platform and not admin_company_ids:
        return redirect(url_for("feedback.my_tickets"))

    q = FeedbackTicket.query
    if not is_platform:
        q = q.filter(FeedbackTicket.company_id.in_(admin_company_ids))

    # Filters
    f_type    = request.args.get("type")
    f_status  = request.args.get("status")
    f_company = request.args.get("company_id", type=int)
    f_owner   = request.args.get("assigned_to", type=int)
    f_pri     = request.args.get("priority")  # "1" → priority_fix=True
    f_date    = request.args.get("since")     # ISO date YYYY-MM-DD

    if f_type and f_type in FeedbackTicket.TYPES:
        q = q.filter(FeedbackTicket.ticket_type == f_type)
    if f_status and f_status in FeedbackTicket.STATUSES:
        q = q.filter(FeedbackTicket.status == f_status)
    if f_company:
        if is_platform or f_company in admin_company_ids:
            q = q.filter(FeedbackTicket.company_id == f_company)
    if f_owner:
        q = q.filter(FeedbackTicket.assigned_to_user_id == f_owner)
    if f_pri == "1":
        q = q.filter(FeedbackTicket.priority_fix.is_(True))
    if f_date:
        try:
            since = datetime.fromisoformat(f_date)
            q = q.filter(FeedbackTicket.created_at >= since)
        except ValueError:
            pass

    tickets = q.order_by(desc(FeedbackTicket.priority_fix),
                         desc(FeedbackTicket.created_at)).limit(500).all()

    # Filter dropdown sources
    if is_platform:
        companies = Company.query.order_by(Company.name).all()
    else:
        companies = Company.query.filter(Company.id.in_(admin_company_ids)).all()

    return render_template(
        "feedback/dashboard.html",
        tickets=tickets,
        companies=companies,
        is_platform=is_platform,
        types=FeedbackTicket.TYPES,
        statuses=FeedbackTicket.STATUSES,
        filters={
            "type": f_type or "", "status": f_status or "",
            "company_id": f_company or "", "assigned_to": f_owner or "",
            "priority": f_pri or "", "since": f_date or "",
        },
    )


# ── Ticket detail + actions ─────────────────────────────────────────────────

def _get_or_403(ticket_id: int) -> tuple[Optional[FeedbackTicket], Optional[tuple]]:
    """Return (ticket, error_response). On failure, ticket is None and the
    second element is a (response, status) tuple to return."""
    t = FeedbackTicket.query.get(ticket_id)
    if not t:
        return None, (jsonify({"error": "ticket not found"}), 404)
    if not _can_view_ticket(current_user, t):
        return None, (jsonify({"error": "forbidden"}), 403)
    return t, None


@feedback_bp.route("/feedback/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    t, err = _get_or_403(ticket_id)
    if err is not None:
        # Render a friendly redirect for missing/forbidden, not a JSON dump.
        return redirect(url_for("feedback.my_tickets"))
    can_admin = _can_admin_ticket(current_user, t)
    # Hide internal notes from non-admins.
    visible_comments = [c for c in t.comments if can_admin or not c.is_internal]
    # Assignable users = company members (or platform admins for global tickets).
    assignable = []
    if can_admin and t.company_id:
        from models import UserCompanyAccess
        rows = UserCompanyAccess.query.filter_by(company_id=t.company_id).all()
        ids = [r.user_id for r in rows]
        if ids:
            assignable = User.query.filter(User.id.in_(ids)).order_by(User.username).all()
    return render_template("feedback/detail.html",
                           ticket=t, comments=visible_comments,
                           can_admin=can_admin, assignable=assignable,
                           statuses=FeedbackTicket.STATUSES)


@feedback_bp.route("/feedback/<int:ticket_id>/screenshot")
@login_required
def screenshot(ticket_id):
    """Authenticated screenshot serving.

    Files live under Flask's instance path (NOT /static), so the only way to
    fetch a screenshot is through this route, which enforces the same
    visibility check as the ticket detail page.
    """
    t, err = _get_or_403(ticket_id)
    if err is not None:
        abort(404)
    rel = (t.screenshot_path or "").strip()
    if not rel:
        abort(404)
    # `rel` is a ticket-scoped path like "42/abc.png". Reject anything that
    # tries to escape (path traversal, absolute paths, mismatched ticket id).
    if rel.startswith(("/", "\\")) or ".." in rel.split("/"):
        abort(404)
    parts = rel.split("/", 1)
    if len(parts) != 2 or parts[0] != str(ticket_id):
        abort(404)
    return send_from_directory(_screenshot_storage_root(), rel,
                               as_attachment=False)


@feedback_bp.route("/api/feedback/<int:ticket_id>/comment", methods=["POST"])
@login_required
def add_comment(ticket_id):
    t, err = _get_or_403(ticket_id)
    if err is not None:
        return err
    body = request.get_json(silent=True) or request.form.to_dict()
    text = (body.get("body") or "").strip()
    if not text:
        return jsonify({"error": "body is required"}), 400
    is_internal = bool(body.get("is_internal")) and _can_admin_ticket(current_user, t)
    c = FeedbackTicketComment(
        ticket_id=t.id, author_user_id=current_user.id,
        body=text, is_internal=is_internal,
    )
    db.session.add(c)
    t.updated_at = datetime.utcnow()
    db.session.commit()
    # Notify the other party (don't ping yourself; don't surface internal notes).
    if not is_internal and t.user_id != current_user.id:
        _notify(t.user_id, f"New reply on your feedback: {t.title}",
                text[:200], url_for("feedback.ticket_detail", ticket_id=t.id),
                company_id=t.company_id)
    return jsonify({"ok": True, "comment_id": c.id}), 201


@feedback_bp.route("/api/feedback/<int:ticket_id>/status", methods=["POST"])
@login_required
def change_status(ticket_id):
    t, err = _get_or_403(ticket_id)
    if err is not None:
        return err
    if not _can_admin_ticket(current_user, t):
        return jsonify({"error": "admin only"}), 403
    body = request.get_json(silent=True) or request.form.to_dict()
    new_status = (body.get("status") or "").strip().lower()
    if new_status not in FeedbackTicket.STATUSES:
        return jsonify({"error": "invalid status",
                        "allowed": list(FeedbackTicket.STATUSES)}), 400
    prev = t.status
    t.status = new_status
    if new_status in ("closed", "rejected") and not t.closed_at:
        t.closed_at = datetime.utcnow()
    if new_status not in ("closed", "rejected"):
        t.closed_at = None
    db.session.commit()
    # Notify submitter (skip if admin is the submitter)
    if t.user_id != current_user.id:
        _notify(t.user_id,
                f"Your feedback is now: {new_status.replace('_', ' ')}",
                f"\"{t.title}\" moved from {prev} → {new_status}",
                url_for("feedback.ticket_detail", ticket_id=t.id),
                company_id=t.company_id)
    return jsonify({"ok": True, "status": t.status}), 200


@feedback_bp.route("/api/feedback/<int:ticket_id>/assign", methods=["POST"])
@login_required
def assign_ticket(ticket_id):
    t, err = _get_or_403(ticket_id)
    if err is not None:
        return err
    if not _can_admin_ticket(current_user, t):
        return jsonify({"error": "admin only"}), 403
    body = request.get_json(silent=True) or request.form.to_dict()
    raw = body.get("assigned_to_user_id")
    new_owner = None
    if raw not in (None, "", "0"):
        try:
            new_owner = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid assigned_to_user_id"}), 400
        # The assignee must be reachable on the same company (if scoped).
        if t.company_id:
            from models import UserCompanyAccess
            ok = UserCompanyAccess.query.filter_by(
                user_id=new_owner, company_id=t.company_id).first()
            if not ok and not _is_platform_admin(User.query.get(new_owner)):
                return jsonify({"error": "user is not part of the ticket's company"}), 400
    t.assigned_to_user_id = new_owner
    db.session.commit()
    if new_owner and new_owner != current_user.id:
        _notify(new_owner,
                f"Assigned to you: {t.title}",
                f"You've been assigned a {t.ticket_type.replace('_', ' ')} ticket.",
                url_for("feedback.ticket_detail", ticket_id=t.id),
                company_id=t.company_id)
    return jsonify({"ok": True, "assigned_to_user_id": t.assigned_to_user_id}), 200


@feedback_bp.route("/api/feedback/<int:ticket_id>/priority", methods=["POST"])
@login_required
def toggle_priority(ticket_id):
    t, err = _get_or_403(ticket_id)
    if err is not None:
        return err
    if not _can_admin_ticket(current_user, t):
        return jsonify({"error": "admin only"}), 403
    body = request.get_json(silent=True) or request.form.to_dict()
    val = body.get("priority_fix")
    t.priority_fix = bool(val) if val is not None else not t.priority_fix
    if t.priority_fix and t.status == "new":
        t.status = "priority_fix"
    db.session.commit()
    return jsonify({"ok": True, "priority_fix": t.priority_fix,
                    "status": t.status}), 200
