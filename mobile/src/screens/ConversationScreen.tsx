import React, { useCallback, useEffect, useLayoutEffect, useMemo, useState } from 'react';
import { Alert, FlatList, Image, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { Message } from '../api/types';
import type { OutboxAttachment, OutboxItem } from '../messaging/outbox';
import { formatPhone } from '../lib/phone';
import { useController, useOutbox, useSession, useSync } from '../state/hooks';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';
import { relativeTime } from './time';

type Props = NativeStackScreenProps<RootStackParams, 'Conversation'>;
type Bubble = { key: string; mine: boolean; body: string; media: string[]; at: string | null; status: string; pending?: OutboxItem };

const STATUS_LABEL: Record<string, string> = {
  queued: 'Sending…', uploading: 'Uploading…', sending: 'Sending…', failed: 'Not sent', unknown: 'Unknown',
  delivered: 'Delivered', sent: 'Sent', undelivered: 'Not delivered', received: '',
};

export function ConversationScreen({ route, navigation }: Props) {
  const p = usePalette();
  const c = useController();
  const sync = useSync();
  const outbox = useOutbox();
  const { me } = useSession();
  const [convId, setConvId] = useState<number | undefined>(route.params.id);
  const [messages, setMessages] = useState<Message[]>([]);
  const [optedOut, setOptedOut] = useState(false);
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState<OutboxAttachment[]>([]);
  const number = route.params.number;
  const smsNumber = me?.numbers[0];
  const smsBlocked = smsNumber && ['not_registered', 'pending', 'toll_free_pending'].includes(smsNumber.messaging.status);

  useLayoutEffect(() => {
    navigation.setOptions({
      title: route.params.title || formatPhone(number),
      headerRight: () => (
        <Pressable accessibilityLabel={`Call ${route.params.title || number}`} hitSlop={10}
          onPress={() => c.voice.placeCall(number, { name: route.params.title, phoneNumberId: smsNumber?.id }).catch((e) => Alert.alert('Can’t call', (e as Error).message))}>
          <Text style={{ color: p.accent, fontSize: 22 }}>{'☎︎'}</Text>
        </Pressable>
      ),
    });
  }, [navigation, route.params.title, number, p.accent, c, smsNumber?.id]);

  const load = useCallback(async () => {
    if (!convId) return;
    try {
      const res = await c.api.conversation(convId);
      setMessages(res.messages);
      setOptedOut(res.conversation.is_opted_out);
      if (!res.conversation.is_read) {
        c.api.markRead(convId).catch(() => undefined);
        c.sync.markConversationRead(convId);
      }
    } catch {
      // keep what we have; the next sync event reloads
    }
  }, [c, convId]);

  // Reload on open and whenever sync reports a change (live event, resume, reconnect).
  useEffect(() => {
    void load();
  }, [load, sync.version]);

  // A brand-new thread gets its id from the first send.
  useEffect(() => {
    if (convId) return;
    const sent = outbox.find((i) => i.to === number && i.serverConversationId);
    if (sent?.serverConversationId) setConvId(sent.serverConversationId);
  }, [outbox, convId, number]);

  const pending = useMemo(
    () => outbox.filter((i) => i.state !== 'sent' && ((convId && i.conversationId === convId) || (!i.conversationId && i.to === number))),
    [outbox, convId, number],
  );

  const bubbles: Bubble[] = useMemo(() => {
    const server: Bubble[] = messages.map((m) => ({
      key: `m${m.id}`, mine: m.direction === 'outbound', body: m.body, media: m.media_urls.map((u) => c.http.mobileUrl(u)),
      at: m.created_at, status: m.direction === 'outbound' ? m.status : '',
    }));
    const local: Bubble[] = pending.map((i) => ({
      key: `o${i.localId}`, mine: true, body: i.body, media: i.attachments.map((a) => a.uri), at: null, status: i.state, pending: i,
    }));
    return [...server, ...local].reverse();
  }, [messages, pending, c.http]);

  const pick = async () => {
    const res = await ImagePicker.launchImageLibraryAsync({ mediaTypes: ['images'], quality: 0.7, allowsMultipleSelection: true, selectionLimit: 5 });
    if (res.canceled) return;
    setAttachments((prev) => [...prev, ...res.assets.map((a) => ({
      uri: a.uri, name: a.fileName ?? 'photo.jpg', type: a.mimeType ?? 'image/jpeg',
    }))].slice(0, 10));
  };

  const send = async () => {
    if (!text.trim() && !attachments.length) return;
    try {
      await c.outbox.enqueue({ conversationId: convId, to: convId ? undefined : number, body: text, attachments });
      setText('');
      setAttachments([]);
    } catch (e) {
      Alert.alert('Can’t send', (e as Error).message);
    }
  };

  const onBubble = (b: Bubble) => {
    const item = b.pending;
    if (!item || (item.state !== 'failed' && item.state !== 'unknown')) return;
    Alert.alert(item.state === 'unknown' ? 'Not sure this was sent' : 'Not sent', item.error ?? undefined, [
      { text: 'Try again', onPress: () => void c.outbox.retry(item.localId) },
      { text: 'Delete', style: 'destructive', onPress: () => void c.outbox.discard(item.localId) },
      { text: 'Cancel', style: 'cancel' },
    ]);
  };

  const [token, setToken] = useState<string | null>(null);
  useEffect(() => {
    void c.http.accessToken().then(setToken);
  }, [c, sync.version]);

  return (
    <KeyboardAvoidingView style={{ flex: 1, backgroundColor: p.bg }} behavior={Platform.OS === 'ios' ? 'padding' : undefined} keyboardVerticalOffset={90}>
      <FlatList
        inverted
        data={bubbles}
        keyExtractor={(b) => b.key}
        contentContainerStyle={{ padding: 12 }}
        renderItem={({ item: b }) => (
          <Pressable onPress={() => onBubble(b)} style={[styles.bubbleWrap, { alignItems: b.mine ? 'flex-end' : 'flex-start' }]}>
            <View style={[styles.bubble, { backgroundColor: b.mine ? p.bubbleOut : p.bubbleIn }]}>
              {b.media.map((u) => (
                <Image key={u} source={{ uri: u, headers: u.startsWith('http') && token ? { Authorization: `Bearer ${token}` } : undefined }}
                  style={styles.media} resizeMode="cover" accessibilityLabel="Attachment" />
              ))}
              {b.body ? <Text style={{ color: b.mine ? '#fff' : p.text, fontSize: 16 }}>{b.body}</Text> : null}
            </View>
            <Text style={{ color: b.status === 'failed' || b.status === 'undelivered' ? p.red : p.muted, fontSize: 11, marginTop: 2 }}>
              {[relativeTime(b.at), STATUS_LABEL[b.status] ?? b.status].filter(Boolean).join(' · ')}
              {b.pending?.state === 'failed' || b.pending?.state === 'unknown' ? ' · Tap for options' : ''}
            </Text>
          </Pressable>
        )}
      />
      {optedOut ? <Text style={[styles.banner, { color: p.amber }]}>This contact replied STOP. Texts can&apos;t be sent until they reply START.</Text> : null}
      {smsBlocked ? <Text style={[styles.banner, { color: p.amber }]}>Your number isn&apos;t approved for business texting yet. Calls still work.</Text> : null}
      {attachments.length ? (
        <View style={styles.attachRow}>
          {attachments.map((a, i) => (
            <Pressable key={a.uri} onPress={() => setAttachments((prev) => prev.filter((_, j) => j !== i))} accessibilityLabel="Remove attachment">
              <Image source={{ uri: a.uri }} style={styles.thumb} />
            </Pressable>
          ))}
        </View>
      ) : null}
      <View style={[styles.composer, { borderTopColor: p.border }]}>
        <Pressable onPress={pick} accessibilityLabel="Attach photo" hitSlop={8} disabled={optedOut}>
          <Text style={{ color: p.accent, fontSize: 26 }}>+</Text>
        </Pressable>
        <TextInput value={text} onChangeText={setText} placeholder="Text message" placeholderTextColor={p.muted} multiline editable={!optedOut}
          style={[styles.input, { color: p.text, backgroundColor: p.surface }]} maxLength={1600} />
        <Pressable onPress={send} accessibilityLabel="Send" disabled={optedOut || (!text.trim() && !attachments.length)} hitSlop={8}>
          <Text style={{ color: optedOut || (!text.trim() && !attachments.length) ? p.muted : p.accent, fontWeight: '700', fontSize: 16 }}>Send</Text>
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  bubbleWrap: { marginVertical: 3 },
  bubble: { maxWidth: '80%', borderRadius: 18, paddingHorizontal: 12, paddingVertical: 8, gap: 6 },
  media: { width: 200, height: 200, borderRadius: 12 },
  composer: { flexDirection: 'row', alignItems: 'flex-end', gap: 10, padding: 10, borderTopWidth: StyleSheet.hairlineWidth },
  input: { flex: 1, minHeight: 38, maxHeight: 120, borderRadius: 19, paddingHorizontal: 14, paddingTop: 9, paddingBottom: 9, fontSize: 16 },
  banner: { paddingHorizontal: 16, paddingVertical: 6, fontSize: 13 },
  attachRow: { flexDirection: 'row', gap: 8, paddingHorizontal: 12, paddingTop: 8 },
  thumb: { width: 56, height: 56, borderRadius: 8 },
});
