import type { VoiceSnapshot } from './VoiceEngine';

export interface StatusText {
  title: string;
  detail: string | null;
  tone: 'ok' | 'working' | 'warn' | 'off';
}

const BLOCKED: Record<string, StatusText> = {
  CALLING_DISABLED: { title: 'Receive Calls is off', detail: 'Turn it on in Settings to ring this phone.', tone: 'off' },
  PHONE_AWAY: { title: 'You’re Away', detail: 'Calls go to voicemail until you’re Available.', tone: 'off' },
  NO_ASSIGNED_NUMBER: { title: 'No LUX Connect number', detail: 'Ask your administrator to assign you a number.', tone: 'warn' },
  DEVICE_PENDING_APPROVAL: { title: 'Waiting for approval', detail: 'An administrator must approve this phone before it can take calls.', tone: 'warn' },
  PUSH_CREDENTIAL_NOT_CONFIGURED: { title: 'Outgoing calls only', detail: 'Incoming calls to this phone aren’t set up on the server yet.', tone: 'warn' },
  TOKEN_ENDPOINT_FAILED: { title: 'Calling unavailable', detail: 'The calling service isn’t configured. Retrying automatically.', tone: 'warn' },
};

/** What the status line says. Never claims "Ready" unless registration is proven. */
export function voiceStatusText(s: VoiceSnapshot, platform: 'ios' | 'android'): StatusText {
  switch (s.phase) {
    case 'ready':
      return {
        title: 'Ready to receive calls',
        detail: platform === 'ios'
          ? 'Calls ring even when the app is closed or the phone is locked.'
          : 'Calls ring even when the app is closed. Keep battery optimisation off for LUX Connect.',
        tone: 'ok',
      };
    case 'starting':
    case 'registering':
      return { title: 'Connecting phone…', detail: null, tone: 'working' };
    case 'reconnecting':
      return { title: 'Reconnecting…', detail: s.lastError ? 'Retrying automatically.' : null, tone: 'working' };
    case 'offline':
      return { title: 'No internet connection', detail: 'Calls go to voicemail until you’re back online.', tone: 'warn' };
    case 'unavailable':
      return BLOCKED[s.blockReason ?? ''] ?? { title: 'Calling unavailable', detail: s.blockMessage, tone: 'warn' };
    case 'signed_out':
      return { title: 'Signed out', detail: null, tone: 'off' };
    case 'incoming':
      return { title: 'Incoming call', detail: null, tone: 'ok' };
    case 'connecting':
      return { title: 'Connecting…', detail: null, tone: 'working' };
    case 'ringing':
      return { title: 'Ringing…', detail: null, tone: 'working' };
    case 'connected':
      return { title: 'On a call', detail: null, tone: 'ok' };
    case 'call_reconnecting':
      return { title: 'Reconnecting call…', detail: null, tone: 'warn' };
    case 'ended':
      return { title: 'Call ended', detail: null, tone: 'off' };
  }
}
