export interface Tokens {
  access_token: string;
  refresh_token: string;
  access_expires_at: string;
  refresh_expires_at: string;
}

export interface DeviceInfo {
  id: number;
  platform: 'ios' | 'android';
  device_name: string | null;
  model: string | null;
  app_version: string | null;
  status: 'active' | 'pending_approval' | 'signed_out' | 'revoked';
  ring_enabled: boolean;
  push_registered: boolean;
  voice_registered: boolean;
  voice_registered_at: string | null;
  last_seen_at: string | null;
  is_current: boolean;
  user_id?: number;
}

export type MessagingStatus = 'registered' | 'pending' | 'not_registered' | 'toll_free_verified' | 'toll_free_pending' | 'unknown';

export interface LuxNumber {
  id: number;
  phone_number: string;
  friendly_name: string;
  voice_enabled: boolean;
  can_call: boolean;
  can_send_sms: boolean;
  messaging: { status: MessagingStatus; checked_at: string | null; detail: string | null };
}

export interface CallingStatus {
  available: boolean;
  reason: string | null;
  message: string | null;
  incoming: boolean;
  outgoing: boolean;
  calling_number?: string | null;
}

export interface Me {
  user: { id: number; name: string; email: string };
  company: { id: number; name: string };
  is_admin: boolean;
  device: DeviceInfo;
  numbers: LuxNumber[];
  availability: { state?: string; available?: boolean } & Record<string, unknown>;
  receive_calls: { receive_calls: boolean } & Record<string, unknown>;
  calling: CallingStatus;
  push: { apns: boolean; fcm: boolean };
  server_time: string;
}

export interface VoiceToken {
  token: string;
  identity: string;
  expires_in: number;
  calling_number: string;
  permitted_numbers: string[];
  incoming_enabled: boolean;
  outbound_enabled: boolean;
}

export interface Conversation {
  id: number;
  from_number: string;
  display_name: string | null;
  contact_name: string | null;
  is_read: boolean;
  is_opted_out: boolean;
  is_archived: boolean;
  last_message_at: string | null;
  last_message_preview: string;
  message_count: number;
}

export interface Message {
  id: number;
  direction: 'inbound' | 'outbound';
  body: string;
  status: string;
  media_urls: string[];
  created_at: string | null;
  twilio_sid: string | null;
}

export interface CallRecord {
  id: number;
  twilio_call_sid: string | null;
  direction: 'inbound' | 'outbound';
  status: string;
  from_number: string;
  to_number: string;
  caller_name: string | null;
  label: 'Missed' | 'Outgoing' | 'Incoming';
  duration_seconds: number;
  voicemail_exists: boolean;
  transcription_text: string | null;
  callback_target: string;
  is_read: boolean;
  created_at: string | null;
}

export interface ContactHit {
  name: string;
  phone: string;
  company?: string | null;
  email?: string | null;
  source: string;
}
