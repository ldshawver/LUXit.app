# Responsive QA Report

## Pages Tested
- Analytics Report (`/analytics/report`)
- Analytics Print View (`/analytics/report/print`)
- Login (`/auth/login`)

## Breakpoints Tested
- Mobile: 360px–430px (layout relies on stacked grid + flexible buttons)
- Tablet: 768px–1024px
- Desktop: 1280px+

## Notes
- Analytics report filters and action buttons reflow into stacked layout on small screens.
- KPI cards and report grids use auto-fit grid to avoid horizontal scrolling.
- Print view uses minimal styling for readable mobile print previews.

## Limitations
- Automated visual regression tests not configured in this repo.
