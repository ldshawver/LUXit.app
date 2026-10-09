import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type ViewStyle } from 'react-native';
import { initials } from '../lib/phone';
import { usePalette } from './theme';

export function Avatar({ name, size = 44 }: { name: string | null | undefined; size?: number }) {
  const p = usePalette();
  return (
    <View style={[styles.avatar, { width: size, height: size, borderRadius: size / 2, backgroundColor: p.surface }]}>
      <Text style={{ color: p.muted, fontWeight: '600', fontSize: size * 0.38 }}>{initials(name)}</Text>
    </View>
  );
}

export function Button({ title, onPress, kind = 'primary', disabled, busy, style }: {
  title: string; onPress: () => void; kind?: 'primary' | 'secondary' | 'danger'; disabled?: boolean; busy?: boolean; style?: ViewStyle;
}) {
  const p = usePalette();
  const bg = kind === 'primary' ? p.accent : kind === 'danger' ? p.red : p.surface;
  const fg = kind === 'secondary' ? p.text : '#fff';
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={title} disabled={disabled || busy} onPress={onPress}
      style={({ pressed }) => [styles.button, { backgroundColor: bg, opacity: disabled ? 0.5 : pressed ? 0.8 : 1 }, style]}>
      {busy ? <ActivityIndicator color={fg} /> : <Text style={[styles.buttonText, { color: fg }]}>{title}</Text>}
    </Pressable>
  );
}

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  const p = usePalette();
  return (
    <View style={styles.empty}>
      <Text style={{ color: p.text, fontSize: 17, fontWeight: '600' }}>{title}</Text>
      {detail ? <Text style={{ color: p.muted, marginTop: 6, textAlign: 'center' }}>{detail}</Text> : null}
    </View>
  );
}

export function Row({ children, onPress, accessibilityLabel }: { children: React.ReactNode; onPress?: () => void; accessibilityLabel?: string }) {
  const p = usePalette();
  return (
    <Pressable onPress={onPress} accessibilityLabel={accessibilityLabel} disabled={!onPress}
      style={({ pressed }) => [styles.row, { borderBottomColor: p.border, backgroundColor: pressed ? p.surface : p.bg }]}>
      {children}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  avatar: { alignItems: 'center', justifyContent: 'center' },
  button: { height: 50, borderRadius: 12, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 18 },
  buttonText: { fontSize: 17, fontWeight: '600' },
  empty: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 32 },
  row: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 16, paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth, gap: 12 },
});
