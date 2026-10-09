import React, { useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, View } from 'react-native';
import * as Haptics from 'expo-haptics';
import { formatAsTyped, formatPhone, toE164 } from '../lib/phone';
import { useCallingNumber, useController, useSession, useVoice } from '../state/hooks';
import { usePalette } from '../ui/theme';
import { StatusBanner } from './StatusBanner';

const KEYS: [string, string][] = [
  ['1', ''], ['2', 'ABC'], ['3', 'DEF'], ['4', 'GHI'], ['5', 'JKL'], ['6', 'MNO'],
  ['7', 'PQRS'], ['8', 'TUV'], ['9', 'WXYZ'], ['*', ''], ['0', '+'], ['#', ''],
];

export function KeypadScreen() {
  const p = usePalette();
  const c = useController();
  const voice = useVoice();
  const { me } = useSession();
  const [digits, setDigits] = useState('');
  const number = useCallingNumber();

  const tap = (k: string) => {
    Haptics.selectionAsync().catch(() => undefined);
    setDigits((d) => (d + k).slice(0, 20));
  };

  const call = async () => {
    if (!toE164(digits)) {
      Alert.alert('Check the number', 'Enter a 10-digit number, or + and the country code.');
      return;
    }
    try {
      await c.voice.placeCall(digits, { phoneNumberId: number?.id });
      setDigits('');
    } catch (e) {
      Alert.alert('Can\u2019t call', (e as Error).message);
    }
  };

  const canCall = voice.outgoingEnabled || voice.phase === 'ready';
  return (
    <View style={[styles.fill, { backgroundColor: p.bg }]}>
      <StatusBanner />
      {number ? <Text style={[styles.from, { color: p.muted }]}>Calling from {formatPhone(number.phone_number)}</Text> : null}
      <View style={styles.display}>
        <Text style={[styles.digits, { color: p.text }]} numberOfLines={1} adjustsFontSizeToFit accessibilityLabel="Number to call">
          {formatAsTyped(digits)}
        </Text>
      </View>
      <View style={styles.pad}>
        {KEYS.map(([k, sub]) => (
          <Pressable key={k} onPress={() => tap(k)} onLongPress={() => k === '0' && tap('+')} accessibilityLabel={k}
            style={({ pressed }) => [styles.key, { backgroundColor: pressed ? p.border : p.surface }]}>
            <Text style={[styles.keyText, { color: p.text }]}>{k}</Text>
            {sub ? <Text style={[styles.sub, { color: p.muted }]}>{sub}</Text> : null}
          </Pressable>
        ))}
      </View>
      <View style={styles.actions}>
        <View style={styles.side} />
        <Pressable accessibilityRole="button" accessibilityLabel="Call" disabled={!digits || !canCall} onPress={call}
          style={({ pressed }) => [styles.call, { backgroundColor: p.green, opacity: !digits || !canCall ? 0.4 : pressed ? 0.8 : 1 }]}>
          <Text style={styles.callGlyph}>{'\u260E\uFE0E'}</Text>
        </Pressable>
        <Pressable accessibilityLabel="Delete" style={styles.side} onPress={() => setDigits((d) => d.slice(0, -1))} onLongPress={() => setDigits('')}>
          {digits ? <Text style={{ color: p.muted, fontSize: 24 }}>{'\u232B'}</Text> : null}
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  from: { textAlign: 'center', marginTop: 10 },
  display: { height: 64, justifyContent: 'center', paddingHorizontal: 24 },
  digits: { fontSize: 36, textAlign: 'center', fontVariant: ['tabular-nums'] },
  pad: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', gap: 18, paddingHorizontal: 32 },
  key: { width: 78, height: 78, borderRadius: 39, alignItems: 'center', justifyContent: 'center' },
  keyText: { fontSize: 32 },
  sub: { fontSize: 10, letterSpacing: 2, marginTop: -2 },
  actions: { flexDirection: 'row', justifyContent: 'center', alignItems: 'center', marginTop: 20, gap: 36 },
  side: { width: 60, height: 60, alignItems: 'center', justifyContent: 'center' },
  call: { width: 78, height: 78, borderRadius: 39, alignItems: 'center', justifyContent: 'center' },
  callGlyph: { color: '#fff', fontSize: 34 },
});
