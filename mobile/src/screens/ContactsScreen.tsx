import React, { useEffect, useState } from 'react';
import { ActionSheetIOS, Alert, FlatList, Platform, TextInput, Text, View } from 'react-native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { ContactHit } from '../api/types';
import { formatPhone } from '../lib/phone';
import { useController, useSession } from '../state/hooks';
import { Avatar, EmptyState, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';

/** LUXit contacts (the CRM is the source of truth), searched server-side. */
export function ContactsScreen({ navigation }: { navigation: NativeStackScreenProps<RootStackParams>['navigation'] }) {
  const p = usePalette();
  const c = useController();
  const { me } = useSession();
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<ContactHit[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let live = true;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await c.api.searchContacts(q.trim());
        if (live) setHits(res.contacts.filter((h) => h.phone));
      } catch {
        if (live) setHits([]);
      } finally {
        if (live) setLoading(false);
      }
    }, q ? 250 : 0);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q, c]);

  const open = (h: ContactHit) => {
    const call = () => c.voice.placeCall(h.phone, { name: h.name, phoneNumberId: me?.numbers[0]?.id }).catch((e) => Alert.alert('Can’t call', (e as Error).message));
    const text = () => navigation.navigate('Conversation', { title: h.name, number: h.phone });
    if (Platform.OS === 'ios') {
      ActionSheetIOS.showActionSheetWithOptions({ title: h.name, message: formatPhone(h.phone), options: ['Call', 'Message', 'Cancel'], cancelButtonIndex: 2 },
        (i) => (i === 0 ? void call() : i === 1 ? text() : undefined));
    } else {
      Alert.alert(h.name, formatPhone(h.phone), [{ text: 'Call', onPress: () => void call() }, { text: 'Message', onPress: text }, { text: 'Cancel', style: 'cancel' }]);
    }
  };

  return (
    <View style={{ flex: 1, backgroundColor: p.bg }}>
      <TextInput value={q} onChangeText={setQ} placeholder="Search name or number" placeholderTextColor={p.muted} clearButtonMode="while-editing"
        style={{ margin: 12, height: 40, borderRadius: 10, paddingHorizontal: 12, backgroundColor: p.surface, color: p.text }} />
      <FlatList
        data={hits}
        keyExtractor={(h, i) => `${h.phone}-${i}`}
        keyboardShouldPersistTaps="handled"
        ListEmptyComponent={<EmptyState title={loading ? 'Searching…' : 'No contacts found'} detail="Contacts come from LUXit." />}
        renderItem={({ item }) => (
          <Row onPress={() => open(item)} accessibilityLabel={`${item.name}, ${formatPhone(item.phone)}`}>
            <Avatar name={item.name} size={40} />
            <View style={{ flex: 1 }}>
              <Text style={{ color: p.text, fontSize: 16 }} numberOfLines={1}>{item.name}</Text>
              <Text style={{ color: p.muted, fontSize: 13 }}>{formatPhone(item.phone)}{item.company ? ` · ${item.company}` : ''}</Text>
            </View>
          </Row>
        )}
      />
    </View>
  );
}
