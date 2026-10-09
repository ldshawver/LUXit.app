/** Normalise user input to E.164, assuming US/Canada for 10-digit numbers. */
export function toE164(raw: string): string | null {
  const trimmed = (raw ?? '').trim();
  const digits = trimmed.replace(/\D/g, '');
  if (!digits) return null;
  if (trimmed.startsWith('+')) return digits.length >= 8 && digits.length <= 15 ? `+${digits}` : null;
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith('1')) return `+${digits}`;
  return null;
}

/** "+14155550100" -> "(415) 555-0100"; other countries keep +E.164. */
export function formatPhone(value: string | null | undefined): string {
  if (!value) return '';
  const digits = value.replace(/\D/g, '');
  const nanp = digits.length === 11 && digits.startsWith('1') ? digits.slice(1) : digits.length === 10 ? digits : null;
  if (nanp && (value.startsWith('+1') || !value.startsWith('+'))) {
    return `(${nanp.slice(0, 3)}) ${nanp.slice(3, 6)}-${nanp.slice(6)}`;
  }
  return value.startsWith('+') ? value : `+${digits}`;
}

/** Keypad display formatting as the user types (no country assumptions beyond NANP). */
export function formatAsTyped(input: string): string {
  if (input.startsWith('+') && !input.startsWith('+1')) return input;
  const digits = input.replace(/\D/g, '');
  const local = digits.startsWith('1') ? digits.slice(1) : digits;
  const prefix = digits.startsWith('1') ? '1 ' : '';
  if (local.length <= 3) return prefix + local;
  if (local.length <= 6) return `${prefix}(${local.slice(0, 3)}) ${local.slice(3)}`;
  if (local.length <= 10) return `${prefix}(${local.slice(0, 3)}) ${local.slice(3, 6)}-${local.slice(6)}`;
  return input;
}

export function initials(name: string | null | undefined, fallback = '#'): string {
  const parts = (name ?? '').replace(/[^\p{L}\s]/gu, ' ').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return fallback;
  return (parts[0]![0]! + (parts.length > 1 ? parts[parts.length - 1]![0]! : '')).toUpperCase();
}

export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}
