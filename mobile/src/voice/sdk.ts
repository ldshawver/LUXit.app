/**
 * The slice of @twilio/voice-react-native-sdk the engine depends on. The real
 * adapter (twilioAdapter.ts) maps the SDK onto this; tests use fakes. Event
 * names are the SDK's literal string values (Voice.Event, Call.Event,
 * CallInvite.Event) so the adapter is a thin pass-through.
 */
export type SdkVoiceEvent = 'callInvite' | 'registered' | 'unregistered' | 'error' | 'audioDevicesUpdated';
export type SdkCallEvent = 'connected' | 'connectFailure' | 'reconnecting' | 'reconnected' | 'disconnected' | 'ringing' | 'qualityWarningsChanged';
export type SdkInviteEvent = 'accepted' | 'rejected' | 'cancelled' | 'notificationTapped';

export interface SdkError {
  message: string;
  code?: number;
}

export interface SdkCall {
  on(event: SdkCallEvent, listener: (...args: any[]) => void): unknown;
  getSid(): string | undefined;
  getState(): string;
  getFrom(): string | undefined;
  getTo(): string | undefined;
  getCustomParameters(): Record<string, string>;
  getInitialConnectedTimestamp(): Date | undefined;
  disconnect(): Promise<void>;
  mute(mute: boolean): Promise<boolean>;
  isMuted(): boolean | undefined;
  hold(hold: boolean): Promise<boolean>;
  sendDigits(digits: string): Promise<void>;
}

export interface SdkCallInvite {
  on(event: SdkInviteEvent, listener: (...args: any[]) => void): unknown;
  getCallSid(): string;
  getFrom(): string;
  getTo(): string;
  getState(): string;
  getCustomParameters(): Record<string, string>;
  accept(): Promise<SdkCall>;
  reject(): Promise<void>;
}

export interface SdkAudioDevice {
  uuid: string;
  type: 'earpiece' | 'speaker' | 'bluetooth' | string;
  name: string;
  select(): Promise<void>;
}

export interface VoiceSdk {
  on(event: SdkVoiceEvent, listener: (...args: any[]) => void): unknown;
  register(token: string): Promise<void>;
  unregister(token: string): Promise<void>;
  connect(token: string, opts: { params: Record<string, string>; contactHandle?: string; notificationDisplayName?: string }): Promise<SdkCall>;
  getCalls(): Promise<ReadonlyMap<string, SdkCall>>;
  getCallInvites(): Promise<ReadonlyMap<string, SdkCallInvite>>;
  getDeviceToken(): Promise<string>;
  getAudioDevices(): Promise<{ audioDevices: SdkAudioDevice[]; selectedDevice?: SdkAudioDevice }>;
  /** iOS only. */
  initializePushRegistry?(): Promise<void>;
  /** iOS only. */
  showAvRoutePickerView?(): Promise<void>;
}
