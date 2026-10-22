# Vendored: @twilio/voice-sdk 2.18.3

Self-hosted because Twilio's legacy CDN
(`https://sdk.twilio.com/js/voice/releases/*`) was discontinued as of
Voice SDK v2.0 -- "As of 2.0, the Twilio Voice SDK is no longer hosted via
CDN" (Twilio's own twilio-voice.js documentation). Every release path under
that host now returns HTTP 403/AccessDenied. Twilio's own recommendation for
non-bundler consumers is to vendor `dist/twilio.min.js` from the npm
package or GitHub release.

- Source: npm registry, `@twilio/voice-sdk@2.18.3`
  (https://registry.npmjs.org/@twilio/voice-sdk/-/voice-sdk-2.18.3.tgz)
- Tarball sha1 (matches npm registry `dist.shasum`): 6d78baf4f0ae7da675a2444457f24b82661bd31b
- twilio.min.js sha256: c688006b6fe2f1810a5eb8e64c1a2549a8bf60fb675e51ee46cedd8ec449dd81
- Vendored: 2026-08-28
- License: MIT-style, see LICENSE.md (Twilio, inc.)

## Update strategy
To pick up a newer SDK version: check
`https://registry.npmjs.org/@twilio/voice-sdk/latest` for the current
version, download that version's tarball, verify its sha1 against the
registry's `dist.shasum` field, extract `package/dist/twilio.min.js` and
`package/LICENSE.md`, place them under a new
`static/vendor/twilio-voice-sdk/<version>/` directory (keep old versions
until the template is updated and verified), and update the
`<script src="...">` path in `templates/inbox_pwa/calls.html` (and
`templates/twilio/comms_hub.html` / `templates/inbox_pwa/index.html` if
they also reference the SDK) to the new path. Do not overwrite an
already-deployed version in place.
