# LUX Marketing Analytics Module — Codex Instructions

## Role & Mission
You are Codex, acting as a senior full-stack engineer + data engineer working inside the LUX Marketing codebase.

Your mission is to design and implement a complete marketing analytics system that:

- Is accurate, auditable, and extensible
- Uses Postgres as the single source of truth
- Avoids vendor lock-in
- Prioritizes business decisions, not vanity metrics
- Is safe for compliance (CCPA/CPRA aware)
- Can be built incrementally without breaking production

Favor clarity, correctness, and durability over cleverness.

---

## Hard Constraints (Non‑Negotiable)

- **No third‑party analytics SDKs** (GA4, Segment, Mixpanel, etc.)
  - All tracking must go through a first‑party `/e` event endpoint.
- **Postgres is the truth layer**
  - No analytics logic should live only in the frontend.
- **Append‑only raw events**
  - Never mutate or delete raw events once written.
- **Multi‑tenant safe**
  - Every analytics record must be scoped by `company_id`.
- **Incremental delivery**
  - Each phase must run independently and safely.
- **Readable code > clever code**
  - Favor explicitness over abstraction.

---

## Phased Implementation Plan (Follow in Order)

### Phase 1 — Event Foundation
**Goal:** Create a reliable, minimal event ingestion system.

**Deliverables**
- `/e` tracking endpoint (POST, JSON)
- `raw_events` table
- Basic session handling
- Minimal JS tracking snippet

**Requirements**
- Accept JSON payloads
- Generate `visitor_id` if missing
- Attach `session_id`
- Parse UTM parameters
- Store:
  - `event_name`
  - `occurred_at`
  - `visitor_id`
  - `session_id`
  - `user_id` (nullable)
  - `company_id`
  - `page_url`
  - `referrer`
  - `user_agent`
  - `ip_hash` (NOT raw IP)
  - `properties` (jsonb)

**Files to create**
```
lux_marketing/
  analytics/
    routes.py        # /e endpoint
    models.py        # raw_events, sessions
    identity.py      # visitor ↔ user resolution
```

---

### Phase 2 — Commerce & Lead Events
**Goal:** Track actions that map directly to revenue.

**Required events**
- `page_view`
- `add_to_cart`
- `begin_checkout`
- `purchase`
- `lead_submit`
- `signup`
- `login`

**Requirements**
- Purchase events must include:
  - `order_id`
  - `value` + `currency`
- Orders must also be importable from WooCommerce/Stripe
- Events and orders must be linkable via visitor/session/customer

---

### Phase 3 — Attribution (V1)
**Goal:** Enable trustworthy revenue attribution.

**Attribution rules**
- Lookback window: 30 days
- First‑touch: earliest session in window
- Last‑touch: most recent session within 72 hours pre‑purchase

**Store attribution on the order record**

**Deliverables**
- Attribution logic module
- Fields on orders table:
  - `first_touch_channel`
  - `last_touch_channel`
  - `first_touch_campaign`
  - `last_touch_campaign`

**Files**
```
analytics/
  attribution.py
```

---

### Phase 4 — Core Metrics & Rollups
**Goal:** Produce metrics marketing teams actually use.

**Metrics to implement**
- Revenue by channel
- CAC
- ROAS
- Funnel conversion rates
- Drop‑off by step
- Time‑to‑conversion
- Qualified session rate
- Repeat purchase rate
- LTV (rolling + cohort)

**Requirements**
- Use daily rollup tables
- Never compute heavy metrics directly from raw events at runtime

**Files**
```
analytics/
  rollups.py
  jobs.py     # scheduled tasks
```

---

### Phase 5 — Email & SMS Analytics
**Goal:** Measure owned‑channel performance.

**Required table**
- `message_sends`

**Required metrics**
- Delivery rate
- Open rate (email)
- CTR
- Revenue per send
- Unsubscribe rate
- Complaint rate

**Attribution**
- Last‑touch within 5 days of purchase

---

### Phase 6 — Creative & Fatigue Analytics
**Goal:** Understand what messaging works and when it dies.

**Requirements**
- Track `creative_id` on events
- Track `concept_angle`/tag

**Compute**
- CTR by creative
- Performance decay over time
- Fatigue detection (CTR drop with rising impressions)

---

### Phase 7 — Alerts & Insights Engine
**Goal:** Turn analytics into action.

**Required alerts**
- CPA ↑ > 20% WoW
- Funnel step conversion ↓
- Checkout abandonment spike
- Revenue anomaly
- Email complaints > threshold
- Creative fatigue detected

**Delivery**
- Alerts via email (Slack later)
- Human‑readable explanation included

**Files**
```
analytics/
  alerts.py
```

---

## Dashboards (Do Not Skip)
Provide queries/views for:

- Executive KPI dashboard
- Funnel dashboard
- Acquisition & quality
- Retention & cohorts
- Email/SMS
- Creative performance
- Attribution paths
- Ops velocity (test speed, win rate)

Dashboards may be:
- Metabase‑compatible SQL, **or**
- Internal admin templates

---

## Compliance Requirements
- Hash emails before storage
- Never store raw IPs
- Respect consent flags
- Allow tracking suppression per user/company
- Be compatible with Global Privacy Control (GPC)

---

## Coding Standards
- Python 3.11+
- Flask blueprint‑based routing
- SQLAlchemy models (or raw SQL if existing project uses it)
- Alembic migrations if present
- Type hints where helpful
- No silent failures
- Log errors explicitly

---

## Output Expectations
When implementing:

- Output complete files, not fragments
- Include comments explaining **why**, not just **what**
- Provide sample payloads and example queries
- Do not assume undocumented context

---

## Final Rule
If something is ambiguous:

- Choose the simplest, safest default
- Document the assumption clearly in code comments
