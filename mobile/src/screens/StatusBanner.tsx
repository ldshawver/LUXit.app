import React from 'react';
import { Linking, Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import { useController, useVoice } from '../state/hooks';
import { usePalette } from '../ui/theme';
import { voiceStatusText } from '../voice/statusText';

export function StatusBanner() {
  const p = usePalette();
  const c = useController();
  const s = useVoice();
  const t = voiceStatusText(s, Platform.OS === 'ios' ? 'ios' : 'android');
  const dot = t.tone === 'ok' ? p.green : t.tone === 'working' ? p.accent : t.tone === 'warn' ? p.amber : p.muted;
  return (
    <View style={[styles.wrap, { backgroundColor: p.surface }]} accessibilityRole="summary" accessibilityLiveRegion="polite">
      <View style={styles.line}>
        <View style={[styles.dot, { backgroundColor: dot }]} />
        <Text style={[styles.title, { color: p.text }]}>{t.title}</Text>
      </View>
      {t.detail ? <Text style={{ color: p.muted, marginTop: 4 }}>{t.detail}</Text> : null}
      {s.notice && !s.call && !s.invite ? (
        <View style={styles.notice}>
          <Text style={{ color: p.amber, flex: 1 }}>{s.notice.text}</Text>
          {s.notice.kind === 'mic_denied' ? (
            <Pressable accessibilityRole="button" hitSlop={8} onPress={() => void Linking.openSettings()}>
              <Text style={{ color: p.accent, fontWeight: '600' }}>Settings</Text>
            </Pressable>
          ) : null}
          <Pressable accessibilityRole="button" accessibilityLabel="Dismiss" hitSlop={8} onPress={() => c.voice.dismissNotice()}>
            <Text style={{ color: p.muted, fontWeight: '600' }}>✕</Text>
          </Pressable>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { marginHorizontal: 16, marginTop: 8, padding: 12, borderRadius: 12 },
  line: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  dot: { width: 10, height: 10, borderRadius: 5 },
  title: { fontSize: 15, fontWeight: '600' },
  notice: { flexDirection: 'row', alignItems: 'center', gap: 12, marginTop: 8 },
});
