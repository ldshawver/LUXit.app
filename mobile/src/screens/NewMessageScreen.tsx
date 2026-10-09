import React, { useState } from 'react';
import { Alert, StyleSheet, Text, TextInput, View } from 'react-native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import { formatPhone, toE164 } from '../lib/phone';
import { Button } from '../ui/components';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';

export function NewMessageScreen({ navigation, route }: NativeStackScreenProps<RootStackParams, 'NewMessage'>) {
  const p = usePalette();
  const [to, setTo] = useState(route.params?.to ?? '');
  const go = () => {
    const e164 = toE164(to);
    if (!e164) {
      Alert.alert('Check the number', 'Enter a 10-digit number, or + and the country code.');
      return;
    }
    navigation.replace('Conversation', { title: formatPhone(e164), number: e164 });
  };
  return (
    <View style={[styles.fill, { backgroundColor: p.bg }]}>
      <Text style={{ color: p.muted, marginBottom: 8 }}>To</Text>
      <TextInput value={to} onChangeText={setTo} placeholder="Phone number" keyboardType="phone-pad" autoFocus
        placeholderTextColor={p.muted} style={[styles.input, { color: p.text, backgroundColor: p.surface }]} onSubmitEditing={go} />
      <Button title="Next" onPress={go} disabled={!to} />
    </View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1, padding: 16 },
  input: { height: 50, borderRadius: 12, paddingHorizontal: 14, fontSize: 18, marginBottom: 16 },
});
