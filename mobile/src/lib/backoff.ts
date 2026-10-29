/** Exponential backoff with full jitter, capped. attempt is 0-based. */
export function backoffDelay(attempt: number, opts: { baseMs?: number; capMs?: number; random?: () => number } = {}): number {
  const base = opts.baseMs ?? 1000;
  const cap = opts.capMs ?? 5 * 60 * 1000;
  const random = opts.random ?? Math.random;
  const ceiling = Math.min(cap, base * 2 ** Math.min(attempt, 20));
  // Never below half the ceiling so a retry storm can't collapse to ~0 ms.
  return Math.round(ceiling / 2 + random() * (ceiling / 2));
}
