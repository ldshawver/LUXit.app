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

**Conclusion:** the app is *not* "OS-only" encryption. Strictly, it is not in Apple's exempt row. It is in the standard-algorithm row, which needs **no documents unless the app is distributed in France**.
- **Not in France:** `ITSAppUsesNonExemptEncryption = NO` is what App Store Connect itself instructs, so the build no longer asks per upload.
- **In France:** upload the French declaration (ANSSI), wait for Apple's code, and ship `YES` + `ITSEncryptionExportComplianceCode`.

## 3. Configuration (mobile/app.config.ts)

- `ios.config.usesNonExemptEncryption` replaces the raw `infoPlist.ITSAppUsesNonExemptEncryption`.
- The default is `false`. If `ITS_ENCRYPTION_EXPORT_COMPLIANCE_CODE` is set (EAS env), it becomes `true` and the code is added.
- Verified with `expo prebuild` (iOS):
  - staging, default: `ITSAppUsesNonExemptEncryption = NO`, no code
  - production variant, default: `NO`, no code
  - with `ITS_ENCRYPTION_EXPORT_COMPLIANCE_CODE=TEST-CODE-123`: `YES` + the code

## 4. App Store Connect answers to give (first upload)

1. Does your app use encryption? **Yes.**
2. Algorithm type: **"Standard encryption algorithms instead of, or in addition to, using or accessing the encryption within Apple's operating system."** Not proprietary, not "none".
3. Distributed in France? Answer according to the availability you choose. If **No**, no documents are needed, and that matches the shipped `NO`. If **Yes**, upload the French declaration and use the issued code as above.

Do not answer "only Apple's OS". It is untrue because of the bundled BoringSSL/WebRTC.

## 5. US export law (for counsel to confirm)

- **Likely classification:** a mass-market end-user communications app using standard cryptography, i.e. ECCN 5D992.c under License Exception ENC § 740.17(b)(1). The functions are VoIP and messaging, publicly sold, with crypto that users can't change.
- **Reporting:** the March 29, 2021 BIS rule removed the annual self-classification report for many mass-market items. It **still applies to mass-market components and their executable software** ([Baker McKenzie](https://sanctionsnews.bakermckenzie.com/bis-updates-reporting-requirements-relating-to-mass-market-encryption-items-and-publicly-available-software-and-also-updates-certain-classifications/), [Venable](https://www.venable.com/insights/publications/2021/03/export-administration-rules-are-revised-to-elim)). An end-user app is generally not a "component", but confirm against the current 15 CFR 740.17 text.
- Apple's page also notes that apps with exempt encryption *might* need a year-end self-classification report.
- Embargoed / restricted destinations are out of scope for the App Store anyway. App Store availability controls apply.

## 6. Android

Google Play has no equivalent Info.plist declaration. The same US export classification applies to the Android build (Twilio Voice Android 6.10.3 also bundles WebRTC/BoringSSL).
