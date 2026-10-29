import React, { useCallback, useEffect, useState } from 'react';
import { Alert, FlatList, Pressable, Text, View } from 'react-native';
import type { DeviceInfo } from '../api/types';
import { useController, useSession } from '../state/hooks';
import { EmptyState, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import { relativeTime } from './time';

export function DevicesScreen() {
  const p = usePalette();
  const c = useController();
  const { me } = useSession();
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const load = useCallback(async () => {
    try {
      setDevices((await c.api.devices(me?.is_admin ? 'company' : undefined)).devices);
    } catch {
      // keep list
    }
  }, [c, me?.is_admin]);
  useEffect(() => {
    void load();
  }, [load]);

  const revoke = (d: DeviceInfo) =>
    Alert.alert('Sign out this device?', 'It stops ringing immediately and must sign in again.', [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Sign out', style: 'destructive', onPress: async () => {
        if (d.is_current) return void c.signOut();
        await c.api.revokeDevice(d.id).catch((e) => Alert.alert('Failed', (e as Error).message));
        void load();
      } },
    ]);

  return (
    <FlatList
      style={{ backgroundColor: p.bg }}
      data={devices}
      keyExtractor={(d) => String(d.id)}
      ListEmptyComponent={<EmptyState title="No devices" />}
      renderItem={({ item: d }) => (
        <Row>
          <View style={{ flex: 1 }}>
            <Text style={{ color: p.text, fontSize: 16 }}>{d.device_name ?? d.model ?? d.platform}{d.is_current ? ' (this phone)' : ''}</Text>
            <Text style={{ color: p.muted, fontSize: 13 }}>
              {d.platform === 'ios' ? 'iPhone' : 'Android'} · {d.status === 'pending_approval' ? 'Waiting for approval' : d.voice_registered ? (d.ring_enabled ? 'Rings for calls' : 'Ringing off') : 'Not registered for calls'} · seen {relativeTime(d.last_seen_at)}
            </Text>
          </View>
          {d.status === 'pending_approval' && me?.is_admin ? (
            <Pressable onPress={async () => { await c.api.approveDevice(d.id).catch(() => undefined); void load(); }} hitSlop={8}>
              <Text style={{ color: p.green, marginRight: 12 }}>Approve</Text>
            </Pressable>
          ) : null}
          <Pressable onPress={() => revoke(d)} hitSlop={8} accessibilityLabel="Sign out device"><Text style={{ color: p.red }}>Sign out</Text></Pressable>
        </Row>
      )}
    />
  );
}
