import React, { useCallback, useEffect, useState } from 'react';
import { Alert, ScrollView, Text, View } from 'react-native';
import { formatPhone } from '../lib/phone';
import { useController } from '../state/hooks';
import { Row } from '../ui/components';
import { usePalette } from '../ui/theme';

interface AdminNumber {
  id: number; phone_number: string; friendly_name: string; is_active: boolean;
  messaging: { status: string };
  assignments: { user_id: number; name: string | null; can_call: boolean; can_send_sms: boolean }[];
}

/**
 * Who can use each of the company's existing numbers. Buying, releasing and
 * porting numbers are deliberately not available in the app.
 */
export function NumbersAdminScreen() {
  const p = usePalette();
  const c = useController();
  const [numbers, setNumbers] = useState<AdminNumber[]>([]);
  const load = useCallback(async () => {
    try {
      setNumbers((await c.http.get<{ numbers: AdminNumber[] }>('/admin/numbers')).numbers);
    } catch (e) {
      Alert.alert('Can’t load numbers', (e as Error).message);
    }
  }, [c]);
  useEffect(() => {
    void load();
  }, [load]);

  const remove = (n: AdminNumber, userId: number, name: string | null) =>
    Alert.alert(`Remove ${name ?? 'this person'}?`, `They will stop receiving calls and texts for ${formatPhone(n.phone_number)}. The number itself is kept.`, [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Remove', style: 'destructive', onPress: async () => {
        await c.http.delete(`/admin/numbers/${n.id}/assignments/${userId}`).catch((e) => Alert.alert('Failed', (e as Error).message));
        void load();
      } },
    ]);

  return (
    <ScrollView style={{ backgroundColor: p.bg }}>
      {numbers.map((n) => (
        <View key={n.id} style={{ marginTop: 20 }}>
          <Text style={{ color: p.text, fontSize: 18, fontWeight: '600', marginHorizontal: 16 }}>{formatPhone(n.phone_number)}</Text>
          <Text style={{ color: p.muted, marginHorizontal: 16, marginBottom: 6 }}>{n.friendly_name} · texting: {n.messaging.status.replace('_', ' ')}</Text>
          {n.assignments.map((a) => (
            <Row key={a.user_id} onPress={() => remove(n, a.user_id, a.name)}>
              <Text style={{ color: p.text, flex: 1 }}>{a.name ?? `User ${a.user_id}`}</Text>
              <Text style={{ color: p.muted }}>{[a.can_call ? 'Calls' : null, a.can_send_sms ? 'Texts' : null].filter(Boolean).join(' + ') || 'View only'}</Text>
            </Row>
          ))}
          {!n.assignments.length ? <Row><Text style={{ color: p.muted }}>Nobody assigned</Text></Row> : null}
        </View>
      ))}
      <Text style={{ color: p.muted, margin: 16, fontSize: 13 }}>
        Assign people to a number from LUXit on the web (Communications → Numbers). To add, release or port a number, contact LUXit support.
      </Text>
    </ScrollView>
  );
}
