# LUX Connect / LUXit Phone: iOS encryption and export compliance

Audited 2026-10-09 on `feat/lux-connect-native`. This is an engineering finding, not legal advice. Export classification is the company's legal responsibility.

## 1. Where encryption happens in the iOS app

| Component | Encryption | Implemented by | Evidence |
|---|---|---|---|
| API calls to luxit.app (sign-in, messages, sync, SSE) | TLS (HTTPS) | **Apple OS** (React Native networking → `NSURLSession`; ATS on, `NSAllowsArbitraryLoads=false`) | generated `Info.plist`; all URLs `https://` |
| Token and device-id storage | Keychain | **Apple OS** (`expo-secure-store` → `SecItem*`) | `node_modules/expo-secure-store/ios` |
| Random ids (`Crypto.randomUUID`) | CSPRNG | **Apple OS** (`expo-crypto` → `SecRandomCopyBytes` / CommonCrypto / CryptoKit) | `node_modules/expo-crypto/ios`; the only app use is `randomUUID` |
| Push (APNs, PushKit) | TLS | **Apple OS** | system frameworks |
| **Calls (Twilio Voice iOS SDK 6.13.6, via `@twilio/voice-react-native-sdk` 1.8.0)** | **DTLS-SRTP media encryption** (`AEAD_AES_256_GCM`, `AES_CM_128_HMAC_SHA1_80/32`, ChaCha20-Poly1305) and TLS signaling | **Bundled BoringSSL + WebRTC inside `TwilioVoice.framework`**, not Apple OS | `TwilioVoice.xcframework` (from the CocoaPods spec source): `ACKNOWLEDGEMENTS.md` lists BoringSSL, WebRTC and reSIProcate; the arm64 binary contains the DTLS-SRTP / BoringSSL strings and 45 `SSL_`/`EVP_`/`AES_`/`RSA_`/`X509_` symbols |
| Hermes / JS bundle | none | — | no crypto libraries in the bundle (`node-forge` is only a dependency of the Expo CLI and is not shipped) |

No proprietary or non-standard algorithm is used. Everything outside Apple's OS is IETF-standard (DTLS 1.2, SRTP, AES-GCM, ChaCha20-Poly1305, TLS).

## 2. Apple's rules applied

- **Info.plist key** ([Complying with Encryption Export Regulations](https://developer.apple.com/tutorials/data/documentation/security/complying-with-encryption-export-regulations.md)): `ITSAppUsesNonExemptEncryption = NO` if the app, *including third-party libraries*, uses no encryption or only exempt encryption. Otherwise `YES`. If documentation is approved, Apple issues an `ITSEncryptionExportComplianceCode`.
- **Documentation table** ([Export compliance documentation for encryption](https://developer.apple.com/help/app-store-connect/reference/export-compliance-documentation-for-encryption)):

  | Case | Required |
  |---|---|
  | Encryption limited to Apple's OS | nothing |
  | **Industry-standard algorithm not provided by Apple's OS** ← this app | **French encryption declaration, only if distributed in France** |
  | Proprietary / non-standard algorithm | US CCATS + French declaration |

- **App Store Connect in practice** ([Apple forum thread 849352](https://developer.apple.com/forums/thread/849352)):
  - For "standard algorithms" + "not in France", App Store Connect answers: *"you don't need to upload any documents. You can specify that you don't use encryption in the information property list …"*.
  - Declaring `YES` without an approved document produced build-processing error **90592** for that developer.
  - Apple's docs and the App Store Connect wording are not perfectly consistent; no Apple staff answer exists in that thread.

**Earlier conclusion, withdrawn 2026-10-09:** this doc first treated "standard algorithms, not in France" as enough to ship `NO`. That reads App Store Connect's *documentation* answer as an answer to the *Info.plist* question, and they are different questions (section 2a).

## 2a. Three separate obligations

| | What it is | Decided by | Does leaving France out change it? |
|---|---|---|---|
| **Apple `ITSAppUsesNonExemptEncryption`** | A statement to Apple: the app uses no encryption, or only *exempt* encryption. Lets Apple skip the export questions for each build. | Apple's definition of "exempt". Apple's text says encryption *including third-party libraries*. It does not say that standard algorithms outside the OS are exempt. | **No.** France only decides whether a document must be uploaded. |
| **US export classification (EAR)** | Publishing the app worldwide from the App Store is an export of encryption software. Likely ECCN 5D992.c, mass market, License Exception ENC § 740.17(b)(1). Self-classified; the annual report probably doesn't apply (section 5). | The company and its counsel. Apple does not classify the app. | **No.** It applies to every country the app ships to. |
| **France** | A French encryption declaration (ANSSI), uploaded to App Store Connect, which then issues `ITSEncryptionExportComplianceCode`. | Needed only if the app is distributed in France. | **Yes.** This is the only part France affects. |

How the `NO` question looks:
- **Strict reading.** The app ships cryptography Apple doesn't provide (Twilio's BoringSSL/WebRTC). Mass-market treatment under ENC is a *licence exception*, not a decontrol. The communications-app decontrol (Cat. 5 Part 2 Note 4) doesn't fit, because communications is the app's main purpose. On that reading the answer is **YES**.
- **Practical reading.** App Store Connect, for "standard, not France", says no documents are needed. One reported case also suggested that `NO` is acceptable (forum thread 849352, no Apple staff answer). That is evidence, not a ruling.
- **EAS itself** asks "only uses standard/exempt encryption?", running the two together. Its follow-up says choosing "non-exempt" means annual self-classification reports. That is a tool's view, not Apple's or BIS's.

**Decision:** until Apple (App Store Connect / Developer Support) or counsel confirms which reading is right, the build **writes no value**. App Store Connect then asks the export questions on each build ("Missing Compliance" in TestFlight), and the account holder answers them there. Builds are not blocked.

## 3. Configuration (mobile/app.config.ts)

| EAS environment | Info.plist |
|---|---|
| nothing set (**default**) | no `ITSAppUsesNonExemptEncryption`; answer in App Store Connect for each build |
| `ITS_USES_NON_EXEMPT_ENCRYPTION=false` | `NO` (only after confirmation) |
| `ITS_USES_NON_EXEMPT_ENCRYPTION=true` | `YES` (App Store Connect then asks about documentation) |
| `ITS_ENCRYPTION_EXPORT_COMPLIANCE_CODE=<code>` | `YES` + the code (overrides the line above) |
| any other value | treated as not set |

Verified with `expo config --type introspect` for both variants (staging `com.luxit.connect.staging`, production `com.luxit.connect`).

`eas build` with no value: non-interactive builds log a warning and continue. Interactive builds ask "only uses standard/exempt encryption?". **Answer No to keep the default**: with this dynamic config EAS can't edit the file anyway; it only prints a suggestion.

## 4. App Store Connect answers (each build until a value is set)

1. Does your app use encryption? **Yes.**
2. Algorithm type: **"Standard encryption algorithms instead of, or in addition to, using or accessing the encryption within Apple's operating system."** Not proprietary, and not "only Apple's OS". That would be untrue because of the bundled BoringSSL/WebRTC.
3. Distributed in France? **No**, for the first release (section 4a).

## 4a. First release without France, and adding it later

- **App Store Connect → Pricing and Availability:** deselect France. Overseas departments and territories sold as their own storefronts are not covered by that and need their own checks. Do the same in **Google Play → Countries/regions**: ANSSI covers supplying the app in France on any store.
- **Adding France later:**
  1. File the ANSSI declaration.
  2. Upload it in App Store Connect and wait for the code.
  3. Set `ITS_ENCRYPTION_EXPORT_COMPLIANCE_CODE` in the EAS environment.
  4. Build and submit.
  5. Only then enable France.

  Nothing else in the app changes.

## 5. US export law (for counsel to confirm)

- **Likely classification:** a mass-market end-user communications app using standard cryptography, i.e. ECCN 5D992.c under License Exception ENC § 740.17(b)(1). The functions are VoIP and messaging, publicly sold, with crypto that users can't change.
- **Reporting:** the March 29, 2021 BIS rule removed the annual self-classification report for many mass-market items. It **still applies to mass-market components and their executable software** ([Baker McKenzie](https://sanctionsnews.bakermckenzie.com/bis-updates-reporting-requirements-relating-to-mass-market-encryption-items-and-publicly-available-software-and-also-updates-certain-classifications/), [Venable](https://www.venable.com/insights/publications/2021/03/export-administration-rules-are-revised-to-elim)). An end-user app is generally not a "component", but confirm against the current 15 CFR 740.17 text.
- Apple's page also notes that apps with exempt encryption *might* need a year-end self-classification report.
- Embargoed / restricted destinations are out of scope for the App Store anyway. App Store availability controls apply.

## 6. Android

Google Play has no equivalent Info.plist declaration. The same US export classification applies to the Android build (Twilio Voice Android 6.10.3 also bundles WebRTC/BoringSSL).
