import { useColorScheme } from 'react-native';
import { usePrefs } from '../state/hooks';
import { buildPalette, type Palette } from './palettes';

export { LUX_PALETTES, buildPalette, type Palette } from './palettes';

export function usePalette(): Palette {
  const prefs = usePrefs();
  return buildPalette(prefs.paletteId, useColorScheme() === 'dark' ? 'dark' : 'light');
}
