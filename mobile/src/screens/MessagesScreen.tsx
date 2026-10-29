import React, { useCallback, useState } from 'react';
import { FlatList, RefreshControl, Text, View } from 'react-native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import { formatPhone } from '../lib/phone';
import { useController, useSync } from '../state/hooks';
import { Avatar, EmptyState, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';
import { relativeTime } from './time';

export function MessagesScreen({ navigation }: { navigation: NativeStackScreenProps<RootStackParams>['navigation'] }) {
  const p = usePalette();
  const c = useController();
  const sync = useSync();
  const [refreshing, setRefreshing] = useState(false);
  const refresh = useCallback(async () => {
    setRefreshing(true);
    await c.sync.syncNow();
    setRefreshing(false);
  }, [c]);

  return (
    <FlatList
      style={{ backgroundColor: p.bg }}
      data={sync.conversations}
      keyExtractor={(i) => String(i.id)}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} />}
      ListEmptyComponent={<EmptyState title={sync.status === 'loading' ? 'Loading…' : 'No messages yet'} detail="Texts to your LUX Connect number appear here." />}
      renderItem={({ item }) => {
        const name = item.display_name && item.display_name !== item.from_number ? item.display_name : formatPhone(item.from_number);
        return (
          <Row onPress={() => navigation.navigate('Conversation', { id: item.id, title: name, number: item.from_number })}
            accessibilityLabel={`${name}${item.is_read ? '' : ', unread'}. ${item.last_message_preview}`}>
            <Avatar name={item.display_name} size={44} />
            <View style={{ flex: 1 }}>
              <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
                <Text style={{ color: p.text, fontSize: 16, fontWeight: item.is_read ? '500' : '700', flex: 1 }} numberOfLines={1}>{name}</Text>
                <Text style={{ color: p.muted, fontSize: 13 }}>{relativeTime(item.last_message_at)}</Text>
              </View>
              <Text style={{ color: item.is_read ? p.muted : p.text, marginTop: 2 }} numberOfLines={2}>
                {item.is_opted_out ? 'Opted out (STOP) · ' : ''}{item.last_message_preview}
              </Text>
            </View>
            {!item.is_read ? <View style={{ width: 10, height: 10, borderRadius: 5, backgroundColor: p.accent }} /> : null}
          </Row>
        );
      }}
    />
  );
}
