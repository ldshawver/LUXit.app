import type { PaletteId } from '../state/prefs';

const light = {
  bg: '#FFFFFF', surface: '#F2F3F7', text: '#0B0C10', muted: '#6B7080', border: '#E3E5EC',
  accent: '#0B5FFF', green: '#1DB954', red: '#E5383B', amber: '#C77700', bubbleOut: '#0B5FFF', bubbleIn: '#ECEEF3',
};
const dark: typeof light = {
  bg: '#000000', surface: '#16171C', text: '#F4F5F8', muted: '#9AA0AE', border: '#26282F',
  accent: '#4D8BFF', green: '#30D158', red: '#FF453A', amber: '#FFB340', bubbleOut: '#2F6BFF', bubbleIn: '#23252C',
};
export type Palette = typeof light;

/**
 * The six LUXit palettes, values from the PWA (templates/inbox_pwa/calls.html
 * and index.html). In dark mode the whole palette applies; in light mode only
 * its brand color, so text stays readable on white.
 */
export const LUX_PALETTES: Record<PaletteId, { label: string; primary: string; background: string; surface: string; text: string; muted: string; border: string }> = {
  lux: { label: 'LUX', primary: '#2563eb', background: '#000000', surface: '#0b0d14', text: '#f1f5f9', muted: '#94a3b8', border: '#1e2535' },
  ocean: { label: 'Ocean', primary: '#0891b2', background: '#042f3a', surface: '#083b4a', text: '#ecfeff', muted: '#a5f3fc', border: '#155e75' },
  forest: { label: 'Forest', primary: '#16a34a', background: '#041f12', surface: '#0b2f1a', text: '#f0fdf4', muted: '#bbf7d0', border: '#166534' },
  sunset: { label: 'Sunset', primary: '#ea580c', background: '#2a0d05', surface: '#431407', text: '#fff7ed', muted: '#fed7aa', border: '#9a3412' },
  slate: { label: 'Slate', primary: '#2563eb', background: '#020617', surface: '#0f172a', text: '#f8fafc', muted: '#cbd5e1', border: '#334155' },
  rose: { label: 'Rose', primary: '#e11d48', background: '#2a0612', surface: '#4a0f1f', text: '#fff1f2', muted: '#fecdd3', border: '#9f1239' },
};

const cache = new Map<string, Palette>();

export function buildPalette(id: PaletteId, scheme: 'light' | 'dark'): Palette {
  const key = `${id}:${scheme}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const built = makePalette(id, scheme);
  cache.set(key, built);
  return built;
}

function makePalette(id: PaletteId, scheme: 'light' | 'dark'): Palette {
  const lp = LUX_PALETTES[id] ?? LUX_PALETTES.lux;
  if (scheme === 'light') return { ...light, accent: lp.primary, bubbleOut: lp.primary };
  return {
    ...dark, bg: lp.background, surface: lp.surface, text: lp.text, muted: lp.muted, border: lp.border,
    accent: lp.primary, bubbleOut: lp.primary,
  };
}
