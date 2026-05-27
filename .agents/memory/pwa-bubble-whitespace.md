---
name: PWA SMS bubble multiline rendering
description: Characters were appearing on separate lines; fix is CSS white-space + no BR injection in esc().
---

**Root cause:** The `esc()` JS function in `templates/inbox_pwa/index.html` was converting `\n` to `<br>` AND the `.bubble` CSS had no `white-space` rule. SMS bodies stored with literal `\n` were rendering each character on a new line because the `<br>` insertion was mangling the text flow.

**Fix:**
1. Remove the `\n → <br>` replacement from `esc()` — just HTML-escape special chars.
2. Add `white-space: pre-wrap` to `.bubble` CSS — browser renders `\n` as line breaks natively.

**How to apply:** Any new message rendering in the PWA should use `esc()` and rely on `pre-wrap`, never inject `<br>` tags for newlines.
