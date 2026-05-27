# LUX IT — Design Token Reference

## Token Architecture

The design system uses a two-tier token structure:

1. **Primitive tokens** — Raw color values, spacing, and effects (`--lux-primary`, `--lux-cyan`, `--lux-border`, etc.)
2. **Semantic tokens** — Purpose-driven mappings that reference primitives (`--lux-bg-card`, `--lux-text-primary`, `--lux-btn-primary-bg`, etc.)

**Rule: All new component styles MUST use semantic tokens.** Primitive tokens exist for definition only and as backward-compatible aliases.

---

## Background Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-bg-main` | Full page / app shell background | `var(--lux-bg)` (#030014) |
| `--lux-bg-section` | Grouped layout sections, panels | `var(--lux-surface)` |
| `--lux-bg-card` | Standard glass card container | `var(--lux-glass)` |
| `--lux-bg-card-solid` | Non-glass opaque card surface | `var(--lux-card-bg-solid)` |
| `--lux-bg-input` | Form input resting state | rgba(12, 14, 30, 0.8) |
| `--lux-bg-input-focus` | Form input focused state | rgba(12, 14, 30, 1) |
| `--lux-bg-surface` | Generic elevated UI surface | `var(--lux-surface)` |
| `--lux-bg-surface-raised` | Emphasized elevated surface | `var(--lux-surface-raised)` |
| `--lux-bg-overlay` | Full-screen overlays, backdrops | `var(--lux-overlay)` |

### When to use which background

- **bg-main**: The `<body>` or app shell. One per page.
- **bg-section**: Chart containers, hero areas, pipeline stages, alert panels — anything that groups content.
- **bg-card**: Standard `.card` / `.glass-card` components.
- **bg-card-solid**: When you need an opaque background without glass transparency.
- **bg-surface / bg-surface-raised**: Generic elevated containers that aren't explicitly cards.
- **bg-input**: Any text input, select, or textarea.
- **bg-overlay**: Modal backdrops, full-page loading overlays.

---

## Text Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-text-primary` | Headings, important labels, active text | `var(--lux-pearl-white)` (#F0F0F5) |
| `--lux-text-body` | Default body text | `var(--lux-text)` (#f0f0f5) |
| `--lux-text-muted` | Secondary descriptions, metadata | `var(--lux-muted)` (#7a8ba8) |
| `--lux-text-placeholder` | Input placeholder text | `var(--lux-dim)` (#4a5568) |

---

## Button Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-btn-primary-bg` | Primary button background | gradient: primary → purple-deep |
| `--lux-btn-primary-text` | Primary button text color | #fff |
| `--lux-btn-primary-border` | Primary button border | `var(--lux-primary)` |
| `--lux-btn-secondary-bg` | Secondary button background | gradient: cyan 15% → 8% |
| `--lux-btn-secondary-text` | Secondary button text color | `var(--lux-cyan)` |
| `--lux-btn-secondary-border` | Secondary button border | rgba(0, 229, 255, 0.3) |

---

## Navigation Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-navbar-bg` | Top navigation bar background | `var(--lux-glass)` |
| `--lux-navbar-text` | Navbar text color | `var(--lux-pearl-white)` |
| `--lux-sidebar-bg` | Sidebar panel background | `var(--lux-surface)` |
| `--lux-sidebar-text` | Sidebar link text (resting) | `var(--lux-polished-silver)` |
| `--lux-sidebar-active` | Sidebar active item background | `var(--lux-primary-15)` |
| `--lux-sidebar-active-text` | Sidebar active item text | `var(--lux-cyan)` |

---

## Border Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-border-default` | Standard borders (cards, sections, panels) | `var(--lux-border)` (rgba 0.08) |
| `--lux-border-focus` | Focused input/element border | `var(--lux-primary)` |
| `--lux-focus-ring` | Focus ring shadow (box-shadow value) | `var(--lux-primary-25)` |

Primitive border tokens (`--lux-border-subtle`, `--lux-border-strong`) remain available for fine-grained control.

---

## Interactive State Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-hover-bg` | Background on hover (list items, table rows) | `var(--lux-primary-10)` |
| `--lux-active-bg` | Background when active/pressed | `var(--lux-primary-15)` |
| `--lux-focus-ring-color` | Color used for focus outlines | `var(--lux-primary)` |

---

## Status Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-status-success` | Success text/icon color | `var(--lux-green)` (#00ffb4) |
| `--lux-status-success-bg` | Success background | rgba(0, 255, 180, 0.08) |
| `--lux-status-success-border` | Success border | rgba(0, 255, 180, 0.3) |
| `--lux-status-warning` | Warning text/icon color | `var(--lux-amber)` (#f59e0b) |
| `--lux-status-warning-bg` | Warning background | rgba(245, 158, 11, 0.08) |
| `--lux-status-warning-border` | Warning border | rgba(245, 158, 11, 0.3) |
| `--lux-status-danger` | Danger text/icon color | `var(--lux-pink)` (#e4055c) |
| `--lux-status-danger-bg` | Danger background | rgba(228, 5, 92, 0.08) |
| `--lux-status-danger-border` | Danger border | rgba(228, 5, 92, 0.3) |
| `--lux-status-info` | Info text/icon color | `var(--lux-cyan)` (#47f5ff) |
| `--lux-status-info-bg` | Info background | rgba(0, 229, 255, 0.08) |
| `--lux-status-info-border` | Info border | rgba(0, 229, 255, 0.3) |

---

## Modal / Table / Dropdown Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-modal-bg` | Modal content background | gradient: dark surfaces |
| `--lux-modal-border` | Modal border color | rgba(168, 85, 247, 0.2) |
| `--lux-table-header-bg` | Table thead background | rgba(168, 85, 247, 0.08) |
| `--lux-table-header-border` | Table thead bottom border | rgba(168, 85, 247, 0.15) |
| `--lux-table-header-text` | Table header text color | `var(--lux-cyan)` |
| `--lux-table-stripe-bg` | Table row hover / stripe bg | `var(--lux-primary-10)` |
| `--lux-dropdown-bg` | Dropdown menu background | rgba(10, 12, 24, 0.97) |
| `--lux-dropdown-hover` | Dropdown item hover background | rgba(168, 85, 247, 0.1) |
| `--lux-dropdown-text` | Dropdown item text color | rgba(255, 255, 255, 0.75) |

---

## Typography & Link Tokens

| Token | Purpose | Default Value |
|---|---|---|
| `--lux-font` | Base font family stack | "Inter", "Segoe UI", system-ui, -apple-system, sans-serif |
| `--lux-link-color` | Default anchor color | `var(--lux-cyan)` |
| `--lux-link-hover` | Anchor hover color | #9af9ff |
| `--lux-text-secondary` | Accent/secondary text (nav labels, tags) | `var(--lux-polished-silver)` |

---

## Legacy Tokens (Backward-Compatible Aliases)

These tokens are **supported but deprecated for new work**. Use the semantic equivalent instead.

| Legacy Token | Preferred Semantic Token |
|---|---|
| `--lux-card-bg` | `--lux-bg-card` |
| `--lux-card-bg-solid` | `--lux-bg-card-solid` |
| `--lux-surface` | `--lux-bg-section` or `--lux-bg-surface` |
| `--lux-surface-raised` | `--lux-bg-surface-raised` |
| `--lux-overlay` | `--lux-bg-overlay` |
| `--lux-glass` | `--lux-bg-card` |
| `--lux-text` | `--lux-text-body` |
| `--lux-pearl-white` | `--lux-text-primary` |
| `--lux-muted` | `--lux-text-muted` |
| `--lux-dim` | `--lux-text-placeholder` |
| `--lux-border` | `--lux-border-default` |

## Brand Kit Override Strategy

When a company has custom brand colors, only `:root` tokens should be overridden.
The semantic token layer ensures overrides cascade correctly without selector-specific `!important` rules.

Tokens that brand kit overrides should set:
- `--lux-primary`, `--lux-secondary`, `--lux-accent` (core brand colors)
- Derived tokens recalculate automatically via `color-mix()` references
- `--lux-font` (optional font family override)

The brand override block in `base.html` uses **only** `:root`-level token injection:
- Sets primitives (`--lux-primary`, `--lux-secondary`, `--lux-accent`)
- Sets all `color-mix()` derived variants (`--lux-primary-10`, `--lux-secondary-15`, etc.)
- Sets semantic overrides (`--lux-btn-primary-bg`, `--lux-border-focus`, `--lux-focus-ring`, `--lux-sidebar-active-text`, `--lux-table-header-text`)
- Sets Bootstrap variables (`--bs-primary`, `--bs-secondary`, `--bs-link-color`, `--bs-link-hover-color`)
- **No** selector-specific `!important` blocks
