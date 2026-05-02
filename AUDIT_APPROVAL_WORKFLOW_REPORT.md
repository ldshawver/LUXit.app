# AUDIT — Approval Workflow
**Date:** 2026-05-02

---

## EXECUTIVE SUMMARY

The approval workflow system is well-implemented. The `ApprovalQueue` model, routes, and template are all present. Agent deliverables flow into the queue. Approvers can approve, reject, edit, cancel, and schedule content. The main gap is that approved content is not always automatically connected to the publishing/sending workflow.

---

## APPROVAL QUEUE — IMPLEMENTED FEATURES

| Feature | Route | Status |
|---------|-------|--------|
| View queue | `GET /approval-queue` | ✅ |
| Get queue items (JSON) | `GET /api/approval-queue` | ✅ |
| Get item detail | `GET /api/approval-queue/<id>` | ✅ |
| Approve item | `POST /api/approval-queue/<id>/approve` | ✅ |
| Reject item (with feedback) | `POST /api/approval-queue/<id>/reject` | ✅ |
| Edit item | `POST /api/approval-queue/<id>/edit` | ✅ |
| Cancel item | `POST /api/approval-queue/<id>/cancel` | ✅ |
| Queue stats | `GET /api/approval-queue/stats` | ✅ |
| Audit log | `ApprovalAuditLog` model | ✅ |

---

## APPROVAL QUEUE MODEL

```python
class ApprovalQueue(db.Model):
    id, company_id, submitted_by, content_type, title, content
    status          # pending | approved | rejected | canceled | scheduled
    scheduled_for   # DateTime
    approved_by, approved_at
    rejected_by, rejected_at, rejection_reason
    priority        # low | normal | high | urgent
    tags, metadata (JSON)
    created_at, updated_at
```

```python
class ApprovalAuditLog(db.Model):
    id, approval_id, user_id, action
    old_status, new_status, notes, created_at
```

---

## CONTENT TYPES SUPPORTED

| content_type | Description | Publishing Hook |
|-------------|-------------|----------------|
| email_campaign | Email campaign draft | ⚠️ Manual scheduling |
| sms_campaign | SMS campaign draft | ⚠️ Manual scheduling |
| social_post | Social media post | ⚠️ Manual scheduling |
| blog_post | Blog article | ⚠️ Manual publishing |
| ad_copy | Advertisement copy | ❌ No direct hook |
| campaign_strategy | Strategy document | ❌ Reference only |
| press_release | PR draft | ⚠️ Manual publishing |
| agent_report | Agent analysis | ✅ Stored in agent_report |
| landing_page | Landing page copy | ❌ No direct hook |

---

## AGENT DELIVERABLE → APPROVAL FLOW

### Current Flow
```
Agent runs scheduled task
  → Generates content via OpenAI
  → AgentDeliverable(status='draft', company_id=...) created
  → ApprovalQueue(content_type=..., status='pending') created
  → Notification created for company users
```

### Approval Flow
```
Admin views /approval-queue
  → Sees pending items with content preview
  → Actions:
    APPROVE → status='approved', approved_by, approved_at set
              → Notification sent
              → (Gap) Should trigger auto-publish for scheduled items
    REJECT  → status='rejected', rejection_reason stored
              → Notification sent
              → (Gap) Should feed rejection reason back to agent memory
    EDIT    → Content updated, stays in queue
    CANCEL  → status='canceled'
    SCHEDULE → scheduled_for set (content goes live at that time)
```

---

## GAPS

| Gap | Priority | Fix |
|-----|----------|-----|
| Approved email campaigns not auto-sent | P1 | On approve, if content_type='email_campaign', call campaign send |
| Approved social posts not auto-scheduled | P1 | On approve, create SocialPost record with scheduled_for |
| Rejection feedback not stored in AgentMemory | P2 | On reject, create AgentMemory entry with lesson |
| No scheduler that checks `scheduled_for` | P1 | Add APScheduler job to check approved+scheduled items |
| No notification to approvers when agent submits | P2 | Check notification creation — already in ApprovalQueue creation |

---

## RECOMMENDATIONS

```python
# On approval of email_campaign:
if queue_item.content_type == 'email_campaign':
    campaign = Campaign(company_id=..., subject=..., body=queue_item.content, status='approved')
    db.session.add(campaign)

# On approval of social_post:
if queue_item.content_type == 'social_post':
    post = SocialPost(company_id=..., content=queue_item.content, scheduled_for=queue_item.scheduled_for)
    db.session.add(post)

# On rejection — store lesson:
if action == 'reject':
    memory = AgentMemory(
        agent_type=deliverable.agent_type,
        company_id=...,
        memory_type='rejection_lesson',
        content=f"Content rejected: {rejection_reason}"
    )
```
