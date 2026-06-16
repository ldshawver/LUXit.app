# PWA Phone CI Test Strategy

The PWA phone system should be gated in CI by the focused phone suite plus lightweight regression suites that currently pass in the Python 3.12 environment:

```bash
uv run --python 3.12 pytest tests/test_pwa_phone_system.py tests/test_feedback.py tests/test_market_intelligence.py -q
```

Current status after this change:

- `tests/test_pwa_phone_system.py`: passes and covers PWA/Twilio voice routing, tenant isolation, call action idempotency, settings, SMS guardrails, voicemail/recording/transcription callbacks, and signature rejection.
- `tests/test_feedback.py`: passes as a broad Flask/tenant-auth regression suite.
- `tests/test_market_intelligence.py`: passes after restoring `CompetitorContent`, JSON `AgentReport.report_data`, and null-date signal handling.

The full repository suite can now be collected, but it is not yet a reliable deployment gate because older non-phone tests still fail on unrelated legacy expectations and missing routes/model aliases. The latest broad run was:

```bash
uv run --python 3.12 pytest -q
```

Observed result: `231 passed`, `35 failed`, `24 errors`.

Remaining broad-suite failures are outside the PWA phone changes and include:

- `/admin/diagnostics`, `/admin/whoami`, `/healthz`, `/version`, `/login`, `/calendar`, and some analytics endpoints returning 404 in legacy tests.
- `tests/test_phase_2_6.py` expecting older SEO/event/social model/service constructor aliases.
- `tests/test_template_safety.py` finding existing template calls to `current_user.get_default_company()`.
- Startup/port invariant tests expecting older stdout/config details.

Until those legacy suites are modernized, CI should run the passing focused phone gate above for phone changes and track the broad-suite failures separately.
