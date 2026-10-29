import { ApiClient, ApiError, NetworkError } from './http';
import type { AutomationRule, CallRecord, CallRouting, ContactHit, Conversation, DeviceInfo, Me, Message, NumberSettings, VoiceToken } from './types';
import type { PaletteId } from '../state/prefs';
import type { VoiceApi } from '../voice/VoiceEngine';

/** Typed calls into /api/mobile/v1. Paths are relative to that prefix. */
export class LuxApi implements VoiceApi {
  constructor(public http: ApiClient) {}

  me() {
    return this.http.get<Me & { success: boolean }>('/me');
  }

  // voice
  voiceToken() {
    return this.http.get<VoiceToken>('/voice/token', { retries: 1 });
  }

  async reportRegistration(registered: boolean, detail: { voiceDeviceToken?: string; error?: string }) {
    await this.http.post('/devices/current/voice-registration', {
      registered, voice_device_token: detail.voiceDeviceToken, error: detail.error,
    });
  }

  async markCallAccepted(callLogId: string) {
    await this.http.post(`/calls/${encodeURIComponent(callLogId)}/accept`);
  }

  // devices & push
  updateCurrentDevice(fields: Partial<{ device_name: string; app_version: string; os_version: string; model: string; ring_enabled: boolean; apns_environment: string }>) {
    return this.http.patch<{ device: DeviceInfo }>('/devices/current', fields);
  }

  setPushToken(token: string | null, type: 'apns' | 'fcm', apnsEnvironment?: 'sandbox' | 'production') {
    return this.http.put('/devices/current/push-token', { token, type, apns_environment: apnsEnvironment });
  }

  devices(scope?: 'company') {
    return this.http.get<{ devices: DeviceInfo[] }>(scope ? '/devices?scope=company' : '/devices');
  }

  updateDevice(id: number, fields: { ring_enabled?: boolean; device_name?: string }) {
    return this.http.patch<{ device: DeviceInfo }>(`/devices/${id}`, fields);
  }

  revokeDevice(id: number) {
    return this.http.delete(`/devices/${id}`);
  }

  approveDevice(id: number) {
    return this.http.post(`/devices/${id}/approve`);
  }

  logout() {
    return this.http.post('/auth/logout', undefined, { timeoutMs: 8000 });
  }

  // availability
  setReceiveCalls(on: boolean) {
    return this.http.put('/phone/receive-calls', { receive_calls: on });
  }

  setAvailability(state: 'available' | 'away') {
    return this.http.put('/phone/availability', { state });
  }

  // messaging
  conversations(page = 1, q = '') {
    const qs = new URLSearchParams({ page: String(page), filter: 'all' });
    if (q) qs.set('q', q);
    return this.http.get<{ conversations: Conversation[]; unread_count: number; total: number }>(`/inbox/conversations?${qs}`);
  }

  conversation(id: number) {
    return this.http.get<{ conversation: Conversation; messages: Message[] }>(`/inbox/conversations/${id}`);
  }

  markRead(id: number) {
    return this.http.patch(`/inbox/conversations/${id}/read`, { is_read: true });
  }

  uploadMedia(file: { uri: string; name: string; type: string }) {
    const form = new FormData();
    form.append('file', file as unknown as Blob);
    return this.http.post<{ media: { id: number } }>('/media', undefined, { form, timeoutMs: 60000 });
  }

  sendMessage(payload: { conversation_id?: number; to?: string; from_phone_number_id?: number; body: string; media_ids: number[]; idempotency_key: string }) {
    return this.http.post<{ conversation_id: number; idempotent: boolean; message: Message | null }>('/messages', payload, { timeoutMs: 30000 });
  }

  sync(since: string | null) {
    return this.http.get<{ server_time: string; conversations: Conversation[]; calls: CallRecord[]; badges: Record<string, number> | null }>(
      since ? `/sync?since=${encodeURIComponent(since)}` : '/sync');
  }

  // calls & contacts
  recentCalls() {
    return this.http.get<{ calls: CallRecord[] }>('/calls/recent');
  }

  voicemails() {
    return this.http.get<{ voicemails: CallRecord[] }>('/calls/voicemails');
  }

  markCallRead(id: number) {
    return this.http.post(`/calls/${id}/mark-read`);
  }

  // number settings, routing, automation (admins edit; everyone may read)
  numberSettings(id: number) {
    return this.http.get<{ editable: boolean; settings: NumberSettings }>(`/phone/numbers/${id}/settings`);
  }

  saveNumberSettings(id: number, fields: Partial<NumberSettings>) {
    return this.http.put<{ editable: boolean; settings: NumberSettings }>(`/phone/numbers/${id}/settings`, fields);
  }

  numberRouting(id: number) {
    return this.http.get<{ routing: CallRouting | null }>(`/phone/numbers/${id}/routing`);
  }

  rules(phoneNumberId?: number) {
    return this.http.get<{ editable: boolean; rules: AutomationRule[] }>(
      phoneNumberId ? `/automation/rules?phone_number_id=${phoneNumberId}` : '/automation/rules');
  }

  createRule(rule: Partial<AutomationRule>) {
    return this.http.post<{ rule: AutomationRule }>('/automation/rules', rule);
  }

  updateRule(id: number, fields: Partial<AutomationRule>) {
    return this.http.patch<{ rule: AutomationRule }>(`/automation/rules/${id}`, fields);
  }

  deleteRule(id: number) {
    return this.http.delete(`/automation/rules/${id}`);
  }

  // preferences (palette is account-wide, shared with the web app)
  preferences() {
    return this.http.get<{ preferences?: { palette_id?: string } }>('/pwa/preferences');
  }

  savePalette(paletteId: PaletteId) {
    // The server resets the palette when palette_id is missing, so it is always sent.
    return this.http.patch('/pwa/preferences', { palette_id: paletteId });
  }

  searchContacts(q: string) {
    return this.http.get<{ contacts: ContactHit[] }>(`/inbox/contacts/search?q=${encodeURIComponent(q)}`);
  }
}

/** How the voice engine should react to a failed /voice/token call. */
export function classifyVoiceError(e: unknown): 'policy' | 'auth' | 'transient' {
  if (e instanceof ApiError) {
    if (e.status === 401) return 'auth';
    if (e.status === 403 || e.status === 409) return 'policy';
    return 'transient';
  }
  if (e instanceof NetworkError) return 'transient';
  return 'transient';
}
