import { useColorScheme } from 'react-native';

const light = {
  bg: '#FFFFFF', surface: '#F2F3F7', text: '#0B0C10', muted: '#6B7080', border: '#E3E5EC',
  accent: '#0B5FFF', green: '#1DB954', red: '#E5383B', amber: '#C77700', bubbleOut: '#0B5FFF', bubbleIn: '#ECEEF3',
};
const dark: typeof light = {
  bg: '#000000', surface: '#16171C', text: '#F4F5F8', muted: '#9AA0AE', border: '#26282F',
  accent: '#4D8BFF', green: '#30D158', red: '#FF453A', amber: '#FFB340', bubbleOut: '#2F6BFF', bubbleIn: '#23252C',
};
export type Palette = typeof light;

export function usePalette(): Palette {
  return useColorScheme() === 'dark' ? dark : light;
}
