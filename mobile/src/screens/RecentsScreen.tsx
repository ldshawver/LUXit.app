import React, { useCallback, useState } from 'react';
import { Alert, FlatList, RefreshControl, Text, View } from 'react-native';
import type { CallRecord } from '../api/types';
import { formatPhone } from '../lib/phone';
import { useController, useSession, useSync } from '../state/hooks';
import { Avatar, EmptyState, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import { relativeTime } from './time';

export function RecentsScreen() {
  const p = usePalette();
  const c = useController();
  const sync = useSync();
  const { me } = useSession();
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    await c.sync.syncNow();
    setRefreshing(false);
  }, [c]);

  const callBack = async (item: CallRecord) => {
    if (!item.is_read) c.api.markCallRead(item.id).catch(() => undefined);
    try {
      await c.voice.placeCall(item.callback_target, { name: item.caller_name ?? undefined, phoneNumberId: me?.numbers[0]?.id });
    } catch (e) {
      Alert.alert('Can\u2019t call', (e as Error).message);
    }
  };

  return (
    <FlatList
      style={{ backgroundColor: p.bg }}
      data={sync.calls}
      keyExtractor={(i) => String(i.id)}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} />}
      ListEmptyComponent={<EmptyState title="No recent calls" detail="Calls you make and receive appear here." />}
      renderItem={({ item }) => {
        const missed = item.label === 'Missed';
        const who = item.caller_name && item.caller_name !== item.callback_target ? item.caller_name : formatPhone(item.callback_target);
        return (
          <Row onPress={() => callBack(item)} accessibilityLabel={`${item.label} call, ${who}. Tap to call back.`}>
            <Avatar name={item.caller_name} size={40} />
            <View style={{ flex: 1 }}>
              <Text style={{ color: missed ? p.red : p.text, fontSize: 16, fontWeight: missed && !item.is_read ? '700' : '500' }} numberOfLines={1}>{who}</Text>
              <Text style={{ color: p.muted, fontSize: 13 }}>
                {item.direction === 'outbound' ? '\u2197 Outgoing' : missed ? '\u2199 Missed' : '\u2199 Incoming'}
                {item.voicemail_exists ? ' \u00b7 Voicemail' : ''}
              </Text>
            </View>
            <Text style={{ color: p.muted, fontSize: 13 }}>{relativeTime(item.created_at)}</Text>
          </Row>
        );
      }}
    />
  );
}
