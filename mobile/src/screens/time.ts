/** "9:41 AM" today, "Yesterday", weekday within a week, else a short date. Server times are naive UTC. */
export function relativeTime(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return '';
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return '';
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 24 * 60 * 60 * 1000;
  if (d.getTime() >= startOfToday) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (d.getTime() >= startOfToday - day) return 'Yesterday';
  if (d.getTime() >= startOfToday - 6 * day) return d.toLocaleDateString([], { weekday: 'long' });
  return d.toLocaleDateString([], { month: 'numeric', day: 'numeric', year: '2-digit' });
}
