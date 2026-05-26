---
name: PWA apiFetch silent failure pattern
description: Root cause of "reply texts not sending" — unhandled exceptions in apiFetch leave isSending=true and send button permanently disabled.
---

## Root cause
`apiFetch` called `res.json()` with no try/catch. When the server returned an HTML error page (500, CSRF failure, gunicorn error page), `JSON.parse` threw a SyntaxError. This propagated as an unhandled promise rejection to `sendMsg`, which also had no try/catch. As a result:
- `state.isSending` stayed `true`
- `sendBtn` stayed `disabled`
- No error toast appeared
- The user could not send any more messages until page reload

## Fix applied
1. `apiFetch` now wraps `fetch()` and `res.json()` in separate try/catch blocks, returning `{success: false, error: "..."}` on any failure instead of throwing.
2. `sendMsg` has `try/catch/finally` — the `finally` block always resets `state.isSending` and the button's disabled state.
3. On send failure, the optimistic message bubble is removed and the draft text is restored to the input.
4. `credentials: 'same-origin'` added explicitly to every fetch (default behaviour, but now explicit).

## How to apply
Any new fetch-based action in the PWA must:
- Wrap the entire apiFetch call in try/catch
- Reset UI state in `finally` (not just in the success path)
- Never leave the send button permanently disabled
