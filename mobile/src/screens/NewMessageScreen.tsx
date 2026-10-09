import React, { useState } from 'react';
import { Alert, StyleSheet, Text, TextInput, View } from 'react-native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import { formatPhone, toE164 } from '../lib/phone';
import { usePrefs, useSession } from '../state/hooks';
import { outgoingNumber } from '../state/prefs';
import { Button } from '../ui/components';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';

export function NewMessageScreen({ navigation, route }: NativeStackScreenProps<RootStackParams, 'NewMessage'>) {
  const p = usePalette();
  const [to, setTo] = useState(route.params?.to ?? '');
  const { me } = useSession();
  const prefs = usePrefs();
  const senders = (me?.numbers ?? []).filter((n) => n.can_send_sms);
  const [fromId, setFromId] = useState<number | undefined>(outgoingNumber(me?.numbers, prefs.callerNumberId, 'text')?.id);
  const go = () => {
    const e164 = toE164(to);
    if (!e164) {
      Alert.alert('Check the number', 'Enter a 10-digit number, or + and the country code.');
      return;
    }
    navigation.replace('Conversation', { title: formatPhone(e164), number: e164, fromNumberId: fromId });
  };
  return (
    <View style={[styles.fill, { backgroundColor: p.bg }]}>
      <Text style={{ color: p.muted, marginBottom: 8 }}>To</Text>
      <TextInput value={to} onChangeText={setTo} placeholder="Phone number" keyboardType="phone-pad" autoFocus
        placeholderTextColor={p.muted} style={[styles.input, { color: p.text, backgroundColor: p.surface }]} onSubmitEditing={go} />
      {senders.length > 1 ? (
        <>
          <Text style={{ color: p.muted, marginBottom: 8 }}>From</Text>
          <View style={styles.chips}>
            {senders.map((n) => (
              <Text key={n.id} accessibilityRole="button" accessibilityState={{ selected: n.id === fromId }} onPress={() => setFromId(n.id)}
                style={[styles.chip, { color: n.id === fromId ? '#fff' : p.text, backgroundColor: n.id === fromId ? p.accent : p.surface }]}>
                {n.friendly_name !== n.phone_number ? n.friendly_name : formatPhone(n.phone_number)}
              </Text>
            ))}
          </View>
          <Text style={{ color: p.muted, fontSize: 13, marginBottom: 16 }}>If you already have a thread with this person, it moves to the number you pick.</Text>
        </>
      ) : null}
      <Button title="Next" onPress={go} disabled={!to} />
    </View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1, padding: 16 },
  input: { height: 50, borderRadius: 12, paddingHorizontal: 14, fontSize: 18, marginBottom: 16 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 8 },
  chip: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 16, overflow: 'hidden' },
});
