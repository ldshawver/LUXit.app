# Company logo asset deployment note

The production path `/static/company_logos/LC-Logo-Wht_Icon-Blk-Logo-GLOW.png`
points to an uploaded/company-specific asset and should not be restored by
committing a binary image in application bug-fix PRs.

For this asset, use one of the source-controlled options below:

1. Update the affected company `logo_path`/`icon_path` value to an existing
   tracked logo under `static/company_logos/`, such as `LC-Logo-WHT.png`, when
   that is the intended fallback.
2. Deploy the original uploaded PNG to the server-side persistent static/upload
   storage used by production.
3. If the file was renamed, correct only the configured database path so its
   casing and filename match the deployed Linux filesystem exactly.

Do not encode, inline, or substitute another binary asset in a user-deletion
backend fix.
