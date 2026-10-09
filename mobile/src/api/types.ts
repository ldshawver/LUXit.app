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
  call_routing?: CallRouting | null;
}

export type CallDestination = 'app' | 'forward' | 'voicemail';

/** Where calls to a number go; computed by the server with the webhook's own logic. */
export interface CallRouting {
  in_business_hours: boolean;
  business_hours: { destination: CallDestination; forward_to: string | null };
  after_hours: { destination: CallDestination; forward_to: string | null };
  now: { destination: CallDestination; forward_to: string | null };
}

/** Subset of /phone/numbers/<id>/settings the app edits. */
export interface NumberSettings {
  id: number;
  phone_number: string;
  friendly_name: string;
  sms_forward_to: string | null;
  sms_forwarding_enabled: boolean;
  call_forwarding_number: string | null;
  call_forwarding_enabled: boolean;
  during_hours_route: string | null;
  after_hours_route: string | null;
  auto_reply_enabled: boolean;
  number_auto_reply_text: string | null;
  business_hours_auto_reply_enabled: boolean;
  business_hours_auto_reply_text: string;
  after_hours_sms_enabled?: boolean;
  after_hours_text?: string | null;
}

export type RuleTrigger = 'keyword_contains' | 'keyword_exact' | 'regex' | 'first_contact' | 'after_hours' | 'always' | 'stop_keyword';
export type RuleAction = 'reply' | 'forward' | 'tag' | 'opt_out';

export interface AutomationRule {
  id: number;
  name: string;
  phone_number_id: number | null;
  trigger_type: RuleTrigger;
  keywords: string[];
  response: string;
  action: RuleAction;
  forward_to: string | null;
  tag_value: string | null;
  priority: number;
  active_days: number[];
  active_hours_start: string | null;
  active_hours_end: string | null;
  is_active: boolean;
  match_count: number;
  system: boolean;
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
  can_edit_phone_settings?: boolean;
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
