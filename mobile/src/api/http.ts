import { backoffDelay } from '../lib/backoff';
import type { Tokens } from './types';

export interface TokenStore {
  get(): Promise<Tokens | null>;
  set(tokens: Tokens): Promise<void>;
  clear(): Promise<void>;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public body?: unknown) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The request never got an HTTP answer (offline, DNS, TLS, timeout). Safe to retry idempotent work. */
export class NetworkError extends Error {
  constructor(message: string, public timedOut = false) {
    super(message);
    this.name = 'NetworkError';
  }
}

export interface RequestOptions {
  body?: unknown;
  form?: FormData;
  auth?: boolean;
  timeoutMs?: number;
  /** GETs default to 2 retries on network errors / 5xx; writes default to 0. */
  retries?: number;
  signal?: AbortSignal;
}

type FetchLike = (url: string, init: RequestInit) => Promise<Response>;

interface ClientDeps {
  baseUrl: string;
  tokens: TokenStore;
  fetchImpl?: FetchLike;
  sleep?: (ms: number) => Promise<void>;
  /** Called once when the refresh token is rejected: the app must sign in again. */
  onAuthLost?: (code: string) => void;
}

const RETRYABLE_STATUS = new Set([502, 503, 504]);
const REFRESHABLE_CODES = new Set(['token_expired', 'invalid_token', 'auth_required']);

export class ApiClient {
  readonly baseUrl: string;
  private tokens: TokenStore;
  private fetchImpl: FetchLike;
  private sleep: (ms: number) => Promise<void>;
  private onAuthLost?: (code: string) => void;
  private refreshing: Promise<Tokens> | null = null;

  constructor(deps: ClientDeps) {
    this.baseUrl = deps.baseUrl.replace(/\/$/, '');
    this.tokens = deps.tokens;
    this.fetchImpl = deps.fetchImpl ?? ((url, init) => fetch(url, init));
    this.sleep = deps.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.onAuthLost = deps.onAuthLost;
  }

  /** Map a server-relative "/api/x" URL (as the shared PWA views return) to the bearer-only prefix. */
  mobileUrl(serverPath: string): string {
    if (/^https?:\/\//.test(serverPath)) return serverPath;
    const p = serverPath.startsWith('/api/mobile/v1/') ? serverPath : serverPath.replace(/^\/api\//, '/api/mobile/v1/');
    return this.baseUrl + p;
  }

  async accessToken(): Promise<string | null> {
    return (await this.tokens.get())?.access_token ?? null;
  }

  get<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('GET', path, { retries: 2, ...opts });
  }

  post<T>(path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('POST', path, { ...opts, body });
  }

  put<T>(path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('PUT', path, { ...opts, body });
  }

  patch<T>(path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('PATCH', path, { ...opts, body });
  }

  delete<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('DELETE', path, opts);
  }

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const retries = opts.retries ?? 0;
    let attempt = 0;
    for (;;) {
      try {
        return await this.requestOnce<T>(method, path, opts, false);
      } catch (e) {
        const retryable = e instanceof NetworkError || (e instanceof ApiError && RETRYABLE_STATUS.has(e.status));
        if (!retryable || attempt >= retries || opts.signal?.aborted) throw e;
        await this.sleep(backoffDelay(attempt, { baseMs: 500, capMs: 8000 }));
        attempt += 1;
      }
    }
  }

  private async requestOnce<T>(method: string, path: string, opts: RequestOptions, isRetryAfterRefresh: boolean): Promise<T> {
    const auth = opts.auth ?? true;
    const headers: Record<string, string> = { Accept: 'application/json' };
    let token: string | null = null;
    if (auth) {
      token = await this.accessToken();
      if (!token) throw new ApiError(401, 'signed_out', 'Sign in required.');
      headers.Authorization = `Bearer ${token}`;
    }
    let body: BodyInit | undefined;
    if (opts.form) {
      body = opts.form;
    } else if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
    const url = path.startsWith('http') ? path : this.mobileUrl(path.startsWith('/api/') ? path : `/api/mobile/v1${path}`);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), opts.timeoutMs ?? 20000);
    const onOuterAbort = () => controller.abort();
    opts.signal?.addEventListener('abort', onOuterAbort);
    let res: Response;
    try {
      res = await this.fetchImpl(url, { method, headers, body, signal: controller.signal });
    } catch (e) {
      const timedOut = controller.signal.aborted && !opts.signal?.aborted;
      throw new NetworkError(timedOut ? 'Request timed out' : String((e as Error)?.message ?? e), timedOut);
    } finally {
      clearTimeout(timeout);
      opts.signal?.removeEventListener('abort', onOuterAbort);
    }
    const data = await res.json().catch(() => null) as Record<string, unknown> | null;
    if (res.ok) return data as T;
    const code = String(data?.code ?? (res.status === 401 ? 'auth_required' : 'error'));
    const message = String(data?.error ?? `Request failed (${res.status})`);
    if (res.status === 401 && auth && !isRetryAfterRefresh && REFRESHABLE_CODES.has(code)) {
      await this.refresh(token);
      return this.requestOnce<T>(method, path, opts, true);
    }
    if (res.status === 401 && auth) {
      // Device revoked, account archived, company changed, etc.: no refresh helps.
      await this.signedOut(code);
    }
    throw new ApiError(res.status, code, message, data);
  }

  /**
   * Single-flight refresh. Concurrent 401s share one rotation, which matters:
   * the server treats a second use of the same refresh token as theft.
   */
  refresh(staleAccessToken: string | null = null): Promise<Tokens> {
    if (this.refreshing) return this.refreshing;
    this.refreshing = (async () => {
      const current = await this.tokens.get();
      if (!current) throw new ApiError(401, 'signed_out', 'Sign in required.');
      // Another caller already rotated while we waited for the lock.
      if (staleAccessToken && current.access_token !== staleAccessToken) return current;
      let res: Response;
      try {
        res = await this.fetchImpl(this.mobileUrl('/api/mobile/v1/auth/refresh'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ refresh_token: current.refresh_token }),
        });
      } catch (e) {
        // Offline: keep the tokens; the caller sees a network error and retries later.
        throw new NetworkError(String((e as Error)?.message ?? e));
      }
      const data = await res.json().catch(() => null) as (Tokens & { code?: string; error?: string }) | null;
      if (!res.ok || !data?.access_token) {
        const code = data?.code ?? 'refresh_failed';
        if (res.status === 401) await this.signedOut(code);
        throw new ApiError(res.status, code, data?.error ?? 'Please sign in again.');
      }
      const next: Tokens = {
        access_token: data.access_token,
        refresh_token: data.refresh_token,
        access_expires_at: data.access_expires_at,
        refresh_expires_at: data.refresh_expires_at,
      };
      await this.tokens.set(next);
      return next;
    })().finally(() => {
      this.refreshing = null;
    });
    return this.refreshing;
  }

  private async signedOut(code: string): Promise<void> {
    await this.tokens.clear();
    this.onAuthLost?.(code);
  }
}
