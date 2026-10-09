import React, { useEffect, useState } from 'react';
import { ActionSheetIOS, Alert, Linking, Modal, Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import * as Haptics from 'expo-haptics';
import { SafeAreaView } from 'react-native-safe-area-context';
import { formatDuration, formatPhone } from '../lib/phone';
import { useController, useNow, useVoice } from '../state/hooks';
import { Avatar } from '../ui/components';
import { usePalette } from '../ui/theme';

const CALL_PHASES = new Set(['incoming', 'connecting', 'ringing', 'connected', 'call_reconnecting', 'ended']);
const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#'];

/**
 * Full-screen call UI above every tab. The End button is pinned to the
 * bottom safe area in every in-call layout (including with the keypad open)
 * so it can never be scrolled or pushed off screen.
 */
export function CallScreen() {
  const p = usePalette();
  const c = useController();
  const s = useVoice();
  const [showKeys, setShowKeys] = useState(false);
  const [dtmf, setDtmf] = useState('');
  const visible = CALL_PHASES.has(s.phase);
  const now = useNow(s.phase === 'connected' || s.phase === 'call_reconnecting');

  useEffect(() => {
    if (!s.call) {
      setShowKeys(false);
      setDtmf('');
    }
  }, [s.call]);

  if (!visible) return null;

  const name = (s.invite?.name || s.call?.name || s.ended?.name) ?? '';
  const number = (s.invite?.from ?? s.call?.remote ?? s.ended?.remote) ?? '';
  const title = name || formatPhone(number) || 'Unknown caller';
  const subtitle = name ? formatPhone(number) : null;

  let status = '';
  if (s.phase === 'incoming') status = 'LUX Connect call';
  else if (s.phase === 'connecting') status = s.call?.direction === 'outbound' ? 'Calling…' : 'Connecting…';
  else if (s.phase === 'ringing') status = 'Ringing…';
  else if (s.phase === 'connected' && s.call?.startedAt) status = formatDuration((now - s.call.startedAt) / 1000);
  else if (s.phase === 'call_reconnecting') status = 'Reconnecting… poor connection';
  else if (s.phase === 'ended' && s.ended) {
    status = s.ended.reason === 'declined' ? 'Declined'
      : s.ended.reason === 'failed' ? (s.ended.message ? `Call failed · ${s.ended.message}` : 'Call failed')
      : s.ended.answered ? `Call ended · ${formatDuration(s.ended.durationSec)}` : 'Call ended';
  }
  const poor = s.call?.qualityWarnings.length ? 'Poor connection' : null;

  const press = (fn: () => unknown) => () => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => undefined);
    void fn();
  };

  const chooseAudio = async () => {
    if (await c.voice.showRoutePicker()) return;
    const { devices, selected } = await c.voice.audioDevices();
    const label = (t: string, n: string) => (t === 'earpiece' ? 'Phone' : t === 'speaker' ? 'Speaker' : n || 'Bluetooth');
    const options = devices.map((d) => `${label(d.type, d.name)}${selected?.uuid === d.uuid ? ' ✓' : ''}`);
    const pick = (i: number) => devices[i]?.select();
    if (Platform.OS === 'ios') {
      ActionSheetIOS.showActionSheetWithOptions({ options: [...options, 'Cancel'], cancelButtonIndex: options.length }, (i) => {
        if (i < options.length) void pick(i);
      });
    } else {
      Alert.alert('Audio', undefined, [...options.map((o, i) => ({ text: o, onPress: () => void pick(i) })), { text: 'Cancel', style: 'cancel' as const }]);
    }
  };

  const inCall = Boolean(s.call);
  return (
    <Modal visible animationType="slide" presentationStyle="fullScreen" onRequestClose={() => undefined} supportedOrientations={['portrait']}>
      <SafeAreaView style={[styles.fill, { backgroundColor: '#0E1016' }]} edges={['top', 'bottom']}>
        <View style={styles.main}>
          {!showKeys ? (
            <View style={styles.identity}>
              <Avatar name={name || null} size={96} />
              <Text style={styles.title} numberOfLines={2} accessibilityRole="header">{title}</Text>
              {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
              <Text style={styles.status} accessibilityLiveRegion="polite">{status}</Text>
              {poor ? <Text style={[styles.status, { color: p.amber }]}>{poor}</Text> : null}
              {inCall && s.notice?.kind === 'mic_denied' ? (
                <Text style={[styles.status, { color: p.amber, textAlign: 'center' }]} onPress={() => void Linking.openSettings()}>{s.notice.text}</Text>
              ) : null}
            </View>
          ) : (
            <View style={styles.keysHeader}>
              <Text style={styles.subtitle} numberOfLines={1}>{title} · {status}</Text>
              <Text style={styles.dtmf} numberOfLines={1}>{dtmf || ' '}</Text>
            </View>
          )}

          {inCall && showKeys ? (
            <View style={styles.keypad}>
              {KEYS.map((k) => (
                <Pressable key={k} accessibilityLabel={`Key ${k}`} onPress={press(async () => { setDtmf((d) => d + k); await c.voice.sendDigits(k); })}
                  style={({ pressed }) => [styles.key, { opacity: pressed ? 0.6 : 1 }]}>
                  <Text style={styles.keyText}>{k}</Text>
                </Pressable>
              ))}
            </View>
          ) : null}

          {inCall ? (
            <View style={styles.controls}>
              <Control label={s.call?.muted ? 'Unmute' : 'Mute'} active={Boolean(s.call?.muted)} glyph={s.call?.muted ? '🔇' : '🎙️'}
                onPress={press(() => c.voice.setMuted(!s.call?.muted))} compact={showKeys} />
              <Control label={showKeys ? 'Hide keypad' : 'Keypad'} active={showKeys} glyph="⌨️" onPress={() => setShowKeys((v) => !v)} compact={showKeys} />
              <Control label="Audio" glyph="🔊" onPress={press(chooseAudio)} compact={showKeys} />
            </View>
          ) : null}
        </View>

        <View style={styles.foot}>
          {s.phase === 'incoming' ? (
            <View style={styles.answerRow}>
              <Round color={p.red} label="Decline" glyph="✕" onPress={press(() => c.voice.decline())} />
              <Round color={p.green} label="Answer" glyph="☎︎" onPress={press(() => c.voice.accept())} autoFocus />
            </View>
          ) : inCall ? (
            <Round color={p.red} label="End call" glyph="☎︎" rotate onPress={press(() => c.voice.hangup())} />
          ) : (
            <View style={{ height: 76 }} />
          )}
        </View>
      </SafeAreaView>
    </Modal>
  );
}

function Control({ label, glyph, onPress, active, compact }: { label: string; glyph: string; onPress: () => void; active?: boolean; compact?: boolean }) {
  const size = compact ? 52 : 68;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ selected: Boolean(active) }} onPress={onPress} style={styles.control}>
      <View style={[styles.controlCircle, { width: size, height: size, borderRadius: size / 2, backgroundColor: active ? '#FFFFFF' : 'rgba(255,255,255,0.14)' }]}>
        <Text style={{ fontSize: compact ? 20 : 26 }}>{glyph}</Text>
      </View>
      <Text style={styles.controlLabel}>{label}</Text>
    </Pressable>
  );
}

function Round({ color, label, glyph, onPress, rotate, autoFocus }: { color: string; label: string; glyph: string; onPress: () => void; rotate?: boolean; autoFocus?: boolean }) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={onPress} hitSlop={12}
      ref={(r) => { if (autoFocus && r) r.focus?.(); }} style={styles.roundWrap}>
      {({ pressed }) => (
        <>
          <View style={[styles.round, { backgroundColor: color, opacity: pressed ? 0.8 : 1 }]}>
            <Text style={[styles.roundGlyph, rotate ? { transform: [{ rotate: '135deg' }] } : null]}>{glyph}</Text>
          </View>
          <Text style={styles.controlLabel}>{label}</Text>
        </>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  main: { flex: 1, paddingHorizontal: 24 },
  identity: { alignItems: 'center', marginTop: 48 },
  title: { color: '#fff', fontSize: 30, fontWeight: '600', marginTop: 18, textAlign: 'center' },
  subtitle: { color: 'rgba(255,255,255,0.75)', fontSize: 17, marginTop: 6, textAlign: 'center' },
  status: { color: 'rgba(255,255,255,0.75)', fontSize: 17, marginTop: 10, fontVariant: ['tabular-nums'] },
  keysHeader: { alignItems: 'center', marginTop: 16 },
  dtmf: { color: '#fff', fontSize: 30, marginTop: 8, letterSpacing: 2 },
  keypad: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', marginTop: 12, gap: 14 },
  key: { width: 72, height: 72, borderRadius: 36, backgroundColor: 'rgba(255,255,255,0.14)', alignItems: 'center', justifyContent: 'center' },
  keyText: { color: '#fff', fontSize: 30 },
  controls: { flexDirection: 'row', justifyContent: 'space-around', marginTop: 'auto', marginBottom: 24 },
  control: { alignItems: 'center', minWidth: 84 },
  controlCircle: { alignItems: 'center', justifyContent: 'center' },
  controlLabel: { color: '#fff', marginTop: 8, fontSize: 13 },
  foot: { alignItems: 'center', paddingBottom: 20, paddingTop: 8 },
  answerRow: { flexDirection: 'row', justifyContent: 'space-between', width: '100%', paddingHorizontal: 40 },
  roundWrap: { alignItems: 'center' },
  round: { width: 76, height: 76, borderRadius: 38, alignItems: 'center', justifyContent: 'center' },
  roundGlyph: { color: '#fff', fontSize: 32 },
});
