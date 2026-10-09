import React, { useCallback, useEffect, useState } from 'react';
import { Alert, FlatList, Pressable, RefreshControl, Text, View } from 'react-native';
import { useAudioPlayer, useAudioPlayerStatus } from 'expo-audio';
import type { CallRecord } from '../api/types';
import { formatDuration, formatPhone } from '../lib/phone';
import { useController, useCallingNumber, useSync } from '../state/hooks';
import { EmptyState, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import { relativeTime } from './time';

export function VoicemailScreen() {
  const p = usePalette();
  const c = useController();
  const sync = useSync();
  const callingNumber = useCallingNumber();
  const [items, setItems] = useState<CallRecord[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [playing, setPlaying] = useState<number | null>(null);
  const player = useAudioPlayer(null);
  const status = useAudioPlayerStatus(player);

  const load = useCallback(async () => {
    try {
      setItems((await c.api.voicemails()).voicemails);
    } catch {
      // keep the list we have
    }
  }, [c]);

  useEffect(() => {
    void load();
  }, [load, sync.version]);

  const play = async (vm: CallRecord) => {
    if (playing === vm.id && status.playing) {
      player.pause();
      return;
    }
    const token = await c.http.accessToken();
    player.replace({ uri: c.http.mobileUrl(`/api/calls/${vm.id}/voicemail/audio`), headers: token ? { Authorization: `Bearer ${token}` } : undefined });
    player.play();
    setPlaying(vm.id);
    if (!vm.is_read) {
      c.api.markCallRead(vm.id).catch(() => undefined);
      setItems((prev) => prev.map((x) => (x.id === vm.id ? { ...x, is_read: true } : x)));
    }
  };

  const callBack = (vm: CallRecord) =>
    c.voice.placeCall(vm.callback_target, { name: vm.caller_name ?? undefined, phoneNumberId: callingNumber?.id })
      .catch((e) => Alert.alert('Can’t call', (e as Error).message));

  return (
    <FlatList
      style={{ backgroundColor: p.bg }}
      data={items}
      keyExtractor={(i) => String(i.id)}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} />}
      ListEmptyComponent={<EmptyState title="No voicemail" />}
      renderItem={({ item }) => {
        const who = item.caller_name && item.caller_name !== item.callback_target ? item.caller_name : formatPhone(item.callback_target);
        const active = playing === item.id;
        return (
          <Row>
            <Pressable accessibilityLabel={active && status.playing ? 'Pause voicemail' : 'Play voicemail'} onPress={() => play(item)} hitSlop={8}
              style={{ width: 40, height: 40, borderRadius: 20, backgroundColor: p.surface, alignItems: 'center', justifyContent: 'center' }}>
              <Text style={{ color: p.accent, fontSize: 18 }}>{active && status.playing ? '❚❚' : '▶︎'}</Text>
            </Pressable>
            <View style={{ flex: 1 }}>
              <Text style={{ color: p.text, fontSize: 16, fontWeight: item.is_read ? '500' : '700' }} numberOfLines={1}>{who}</Text>
              <Text style={{ color: p.muted, fontSize: 13 }} numberOfLines={3}>
                {active && status.duration ? `${formatDuration(status.currentTime)} / ${formatDuration(status.duration)} · ` : ''}
                {item.transcription_text || (item.duration_seconds ? formatDuration(item.duration_seconds) : 'Voicemail')}
              </Text>
            </View>
            <View style={{ alignItems: 'flex-end', gap: 6 }}>
              <Text style={{ color: p.muted, fontSize: 13 }}>{relativeTime(item.created_at)}</Text>
              <Pressable onPress={() => callBack(item)} accessibilityLabel={`Call back ${who}`} hitSlop={8}>
                <Text style={{ color: p.accent }}>Call back</Text>
              </Pressable>
            </View>
          </Row>
        );
      }}
    />
  );
}
