import { AppState, type AppStateStatus, Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import NetInfo from '@react-native-community/netinfo';
import * as Application from 'expo-application';
import Constants from 'expo-constants';
import * as Crypto from 'expo-crypto';
import * as Device from 'expo-device';
import { ApiClient } from '../api/http';
import { classifyVoiceError, LuxApi } from '../api/endpoints';
import type { Me } from '../api/types';
import { getDeviceUid } from '../auth/deviceId';
import { SecureTokenStore } from '../auth/secureTokenStore';
import { Observable } from '../lib/emitter';
import { Outbox } from '../messaging/outbox';
import { makeStreamOpener } from '../messaging/sseStream';
import { SyncManager } from '../messaging/sync';
import { ensureNotificationChannels, registerAlertPush, setBadge } from '../push/pushRegistration';
import { createTwilioSdk } from '../voice/twilioAdapter';
import { VoiceEngine } from '../voice/VoiceEngine';

export interface SessionSnapshot {
  status: 'booting' | 'signed_out' | 'signed_in';
  me: Me | null;
  signOutReason: string | null;
  online: boolean;
}

const extra = (Constants.expoConfig?.extra ?? {}) as { apiBaseUrl?: string; apsEnvironment?: 'sandbox' | 'production'; variant?: string };
export const API_BASE_URL = extra.apiBaseUrl ?? 'https://staging.luxit.app';
export const APNS_ENVIRONMENT = extra.apsEnvironment ?? 'sandbox';
export const APP_VARIANT = extra.variant ?? 'staging';

const SIGN_OUT_MESSAGES: Record<string, string> = {
  device_revoked: 'This phone was signed out from another device.',
  session_revoked: 'For your security, please sign in again.',
  account_archived: 'This account is no longer active.',
  mobile_access_disabled: 'LUX Connect access was turned off for your account.',
  company_changed: 'Your company changed. Please sign in again.',
  refresh_expired: 'Please sign in again.',
};

/**
 * Owns every long-lived object. Created once per process; OS listeners
 * (AppState, NetInfo) are attached once here and fan out to the engine, sync
 * and outbox, so no screen can register duplicates.
 */
export class AppController extends Observable<SessionSnapshot> {
  readonly tokens = new SecureTokenStore();
  readonly http: ApiClient;
  readonly api: LuxApi;
  readonly sdk = createTwilioSdk();
  readonly voice: VoiceEngine;
  readonly outbox: Outbox;
  readonly sync: SyncManager;

  private state: SessionSnapshot = { status: 'booting', me: null, signOutReason: null, online: true };
  private appState: AppStateStatus = AppState.currentState;
  private booted = false;

  constructor() {
    super();
    this.http = new ApiClient({
      baseUrl: API_BASE_URL,
      tokens: this.tokens,
      onAuthLost: (code) => void this.localSignOut(SIGN_OUT_MESSAGES[code] ?? 'Please sign in again.'),
    });
    this.api = new LuxApi(this.http);
    this.voice = new VoiceEngine({
      sdk: this.sdk,
      api: this.api,
      platform: Platform.OS === 'ios' ? 'ios' : 'android',
      classify: classifyVoiceError,
      log: (msg, extraInfo) => console.log(`[voice] ${msg}`, extraInfo ?? ''),
    });
    this.outbox = new Outbox({
      api: this.api,
      store: AsyncStorage,
      newKey: () => Crypto.randomUUID().replace(/-/g, ''),
      isOnline: () => this.state.online,
    });
    this.sync = new SyncManager({ api: this.api, openStream: makeStreamOpener(this.http) });
    this.sync.subscribe((s) => setBadge(s.unreadMessages + s.missedCalls));
  }

  snapshot(): SessionSnapshot {
    return this.state;
  }

  private set(patch: Partial<SessionSnapshot>): void {
    this.state = { ...this.state, ...patch };
    this.emit(this.state);
  }

  /** Called once from App on mount. */
  async boot(): Promise<void> {
    if (this.booted) return;
    this.booted = true;
    AppState.addEventListener('change', (next) => this.onAppState(next));
    NetInfo.addEventListener((s) => this.onNetwork(Boolean(s.isConnected) && s.isInternetReachable !== false));
    await ensureNotificationChannels().catch(() => undefined);
    const tokens = await this.tokens.get();
    if (!tokens) {
      this.set({ status: 'signed_out' });
      return;
    }
    this.set({ status: 'signed_in' });
    await this.afterSignIn();
  }

  async signIn(email: string, password: string): Promise<void> {
    const uid = await getDeviceUid();
    const res = await this.http.post<{ access_token: string; refresh_token: string; access_expires_at: string; refresh_expires_at: string }>(
      '/auth/login',
      {
        email, password,
        device: {
          device_uid: uid,
          platform: Platform.OS === 'ios' ? 'ios' : 'android',
          device_name: Device.deviceName ?? Device.modelName ?? 'Phone',
          model: Device.modelName,
          os_version: `${Platform.OS} ${Device.osVersion ?? ''}`.trim(),
          app_version: `${Application.nativeApplicationVersion ?? '0'} (${Application.nativeBuildVersion ?? '0'})`,
        },
      },
      { auth: false },
    );
    await this.tokens.set(res);
    this.set({ status: 'signed_in', signOutReason: null });
    await this.afterSignIn();
  }

  private async afterSignIn(): Promise<void> {
    if (Platform.OS === 'ios') {
      await this.api.updateCurrentDevice({ apns_environment: APNS_ENVIRONMENT }).catch(() => undefined);
    }
    await this.refreshMe();
    void this.outbox.load();
    void this.sync.start();
    await this.voice.start();
    void this.registerPush();
  }

  async registerPush(): Promise<void> {
    try {
      await registerAlertPush(this.api, this.sdk, APNS_ENVIRONMENT);
    } catch (e) {
      console.log('[push] registration failed', e);
    }
  }

  async refreshMe(): Promise<Me | null> {
    try {
      const me = await this.api.me();
      this.set({ me });
      return me;
    } catch {
      return this.state.me;
    }
  }

  /** User-initiated sign-out: unregister from Twilio, tell the server, wipe local state. */
  async signOut(): Promise<void> {
    await this.voice.signOut();
    try {
      await this.api.logout();
    } catch {
      // The server also drops this device from ringing when the next
      // refresh fails; local state is cleared regardless.
    }
    await this.localSignOut(null);
  }

  private async localSignOut(reason: string | null): Promise<void> {
    if (this.state.status === 'signed_out') return;
    await this.voice.signOut().catch(() => undefined);
    this.sync.stop();
    await this.tokens.clear();
    setBadge(0);
    this.set({ status: 'signed_out', me: null, signOutReason: reason });
  }

  private onAppState(next: AppStateStatus): void {
    const wasActive = this.appState === 'active';
    this.appState = next;
    const active = next === 'active';
    if (this.state.status !== 'signed_in') return;
    this.sync.setForeground(active);
    if (active && !wasActive) {
      void this.voice.onForeground();
      void this.refreshMe();
      this.outbox.wake();
    }
  }

  private onNetwork(online: boolean): void {
    if (online === this.state.online) return;
    this.set({ online });
    this.voice.onNetwork(online);
    this.sync.setOnline(online);
    if (online) this.outbox.wake();
  }
}

let singleton: AppController | null = null;
export function getController(): AppController {
  if (!singleton) singleton = new AppController();
  return singleton;
}
