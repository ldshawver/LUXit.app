/**
 * iOS incoming-call bootstrap: CallKit configuration + PushKit registry.
 *
 * When a VoIP push wakes a closed / locked / backgrounded app, iOS requires it
 * to be reported to CallKit before the PushKit handler returns; an app that
 * keeps failing to do so is terminated and eventually stops receiving VoIP
 * pushes. In the Twilio RN SDK the push -> TVOCallInvite -> CallKit report path
 * is entirely native and needs no access token, network or JS session; it only
 * needs the PKPushRegistry to exist. So this runs from index.ts before the app
 * renders, restores a session or makes any request.
 *
 * Ordering matters: setCallKitConfiguration replaces the native CXProvider, so
 * it must run before the registry exists (no push can have been reported yet)
 * and never again, or a ringing call would be orphaned on a discarded provider.
 *
 * Once per JS runtime: a second PKPushRegistry would deliver every push twice
 * (duplicate TVOCallInvite / duplicate CallKit report).
 */

export interface PushKitNative {
  setCallKitConfiguration(configuration: Record<string, unknown>): Promise<void>;
  setIncomingCallContactHandleTemplate(template: string): Promise<void>;
  initializePushRegistry(): Promise<void>;
}

export const CALLKIT_CONFIGURATION = {
  callKitIncludesCallsInRecents: true,
  callKitMaximumCallGroups: 1,
  callKitMaximumCallsPerCallGroup: 1,
  callKitSupportedHandleTypes: [2], // CXHandleType.phoneNumber
} as const;

/** CallKit shows the caller name the server passes as a <Parameter>, else the number. */
export const INCOMING_CALL_HANDLE_TEMPLATE = '${caller_name}';

export type PushKitBootstrap = () => Promise<void>;

export function createPushKitBootstrap(
  native: PushKitNative,
  log: (msg: string, extra?: Record<string, unknown>) => void = () => undefined,
): PushKitBootstrap {
  let callKitConfigured = false;
  let ready: Promise<void> | null = null;

  return () => {
    if (ready) return ready;
    ready = (async () => {
      if (!callKitConfigured) {
        callKitConfigured = true;
        // Cosmetic only: the native module already holds a default provider,
        // so a failure here must not stop the registry from being created.
        try {
          await native.setCallKitConfiguration({ ...CALLKIT_CONFIGURATION, callKitSupportedHandleTypes: [...CALLKIT_CONFIGURATION.callKitSupportedHandleTypes] });
        } catch (e) {
          log('setCallKitConfiguration failed', { error: String(e) });
        }
        try {
          await native.setIncomingCallContactHandleTemplate(INCOMING_CALL_HANDLE_TEMPLATE);
        } catch (e) {
          log('setIncomingCallContactHandleTemplate failed', { error: String(e) });
        }
      }
      await native.initializePushRegistry();
    })().catch((e) => {
      // Let a later caller (VoiceEngine.start) retry the registry; CallKit is
      // not reconfigured on retry.
      ready = null;
      throw e;
    });
    return ready;
  };
}
