import type { CallDestination, CallRouting } from '../api/types';
import { formatPhone } from '../lib/phone';

function phrase(d: { destination: CallDestination; forward_to: string | null }): string {
  if (d.destination === 'app') return 'ring LUX Connect';
  if (d.destination === 'forward') return d.forward_to ? `forward to ${d.forward_to.startsWith('+') ? formatPhone(d.forward_to) : d.forward_to}` : 'forward to another phone';
  return 'go to voicemail';
}

/** Plain-language summary of the server's routing decision for one number. */
export function routingSummary(r: CallRouting | null | undefined): { now: string; detail: string | null; bypassesApp: boolean } {
  if (!r) return { now: 'Call routing unavailable', detail: null, bypassesApp: false };
  const now = r.now.destination === 'app' ? 'Calls ring this app'
    : r.now.destination === 'forward' ? `Calls are ${phrase(r.now).replace('forward', 'forwarded')} — this app won’t ring`
      : 'Calls go to voicemail right now — this app won’t ring';
  const same = r.business_hours.destination === r.after_hours.destination
    && r.business_hours.forward_to === r.after_hours.forward_to;
  const detail = same ? null : `Business hours: ${phrase(r.business_hours)}. After hours: ${phrase(r.after_hours)}.`;
  return { now, detail, bypassesApp: r.now.destination !== 'app' };
}
