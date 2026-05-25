---
name: Twilio after-hours auto-reply engine
description: Auto-reply rules must be re-sorted to put after_hours rules first before the evaluation loop runs.
---

The `_apply_auto_reply_rules` function in `twilio_sms.py` evaluates rules in priority order. The bug: after_hours rules had lower seeded priority than first_contact rules, so first_contact would fire and return before after_hours was evaluated.

**Fix applied:**
1. Re-sort rules before the loop: `after_hours` trigger_type always sorts first, then by `priority DESC`.
2. `tag` actions do NOT return/break — they continue the loop so other rules can still fire.
3. `reply` actions return (stop loop) unless `trigger_type == 'after_hours'` which has its own `reply_sent` guard.
4. After_hours seed priority bumped to 50 (first_contact is 5).

**How to apply:** Any time rule evaluation order is changed or new trigger types added, verify after_hours rules still sort to position 0.
