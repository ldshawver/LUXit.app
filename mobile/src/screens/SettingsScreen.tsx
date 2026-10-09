import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Linking, Platform, ScrollView, StyleSheet, Switch, Text, View } from 'react-native';
import { getRecordingPermissionsAsync, requestRecordingPermissionsAsync } from 'expo-audio';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import { formatPhone } from '../lib/phone';
import { notificationPermission } from '../push/pushRegistration';
import { APP_VARIANT } from '../state/controller';
import { useController, useSession, useVoice } from '../state/hooks';
import { Button, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import { voiceStatusText } from '../voice/statusText';
import type { RootStackParams } from './navTypes';

const TEXTING_LABEL: Record<string, string> = {
  registered: 'Texting approved', toll_free_verified: 'Texting approved', pending: 'Texting approval pending',
  toll_free_pending: 'Texting approval pending', not_registered: 'Not approved for texting yet', unknown: '',
};

export function SettingsScreen({ navigation }: NativeStackScreenProps<RootStackParams, 'Settings'>) {
  const p = usePalette();
  const c = useController();
  const { me } = useSession();
  const voice = useVoice();
  const [mic, setMic] = useState<string>('undetermined');
  const [notif, setNotif] = useState<string>('undetermined');
  const [busy, setBusy] = useState(false);

  const loadPerms = useCallback(async () => {
    setMic((await getRecordingPermissionsAsync()).status);
    setNotif(await notificationPermission(false));
  }, []);
  useEffect(() => {
    void loadPerms();
    void c.refreshMe();
  }, [loadPerms, c]);

  const toggle = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await c.refreshMe();
      await c.voice.refresh();
    } catch (e) {
      Alert.alert('Couldn’t save', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const askMic = async () => {
    const r = await requestRecordingPermissionsAsync();
    if (!r.granted && !r.canAskAgain) void Linking.openSettings();
    void loadPerms();
  };
  const askNotif = async () => {
    const r = await notificationPermission(true);
    if (r !== 'granted') void Linking.openSettings();
    else void c.registerPush();
    void loadPerms();
  };

  const status = voiceStatusText(voice, Platform.OS === 'ios' ? 'ios' : 'android');
  const receive = Boolean(me?.receive_calls?.receive_calls);
  const available = me?.availability?.state !== 'away';

  return (
    <ScrollView style={{ backgroundColor: p.bg }} contentContainerStyle={{ paddingBottom: 40 }}>
      <Section title="Account">
        <Row><Text style={[styles.label, { color: p.text }]}>{me?.user.name}</Text><Text style={{ color: p.muted }}>{me?.company.name}</Text></Row>
        {(me?.numbers ?? []).map((n) => (
          <Row key={n.id}>
            <View style={{ flex: 1 }}>
              <Text style={[styles.label, { color: p.text }]}>{formatPhone(n.phone_number)}</Text>
              <Text style={{ color: p.muted, fontSize: 13 }}>{[n.friendly_name !== n.phone_number ? n.friendly_name : null, TEXTING_LABEL[n.messaging.status]].filter(Boolean).join(' · ')}</Text>
            </View>
          </Row>
        ))}
        {me && !me.numbers.length ? <Row><Text style={{ color: p.amber }}>No LUX Connect number assigned. Ask your administrator.</Text></Row> : null}
      </Section>

      <Section title="Calls">
        <Row>
          <View style={{ flex: 1 }}><Text style={[styles.label, { color: p.text }]}>{status.title}</Text>{status.detail ? <Text style={{ color: p.muted, fontSize: 13 }}>{status.detail}</Text> : null}</View>
        </Row>
        <Toggle label="Receive calls" detail="Off: calls to your number skip you." value={receive} disabled={busy} onChange={(v) => toggle(() => c.api.setReceiveCalls(v))} />
        <Toggle label="Available" detail="Away pauses calls and alerts for everyone's shared line." value={available} disabled={busy} onChange={(v) => toggle(() => c.api.setAvailability(v ? 'available' : 'away'))} />
        <Toggle label="Ring this phone" detail="Turn off to keep calls on your other devices only." value={Boolean(me?.device.ring_enabled)} disabled={busy} onChange={(v) => toggle(() => c.api.updateCurrentDevice({ ring_enabled: v }))} />
      </Section>

      <Section title="Permissions">
        <Row onPress={mic === 'granted' ? undefined : askMic}>
          <Text style={[styles.label, { color: p.text, flex: 1 }]}>Microphone</Text>
          <Text style={{ color: mic === 'granted' ? p.green : p.red }}>{mic === 'granted' ? 'Allowed' : 'Tap to allow'}</Text>
        </Row>
        <Row onPress={notif === 'granted' ? undefined : askNotif}>
          <Text style={[styles.label, { color: p.text, flex: 1 }]}>Notifications</Text>
          <Text style={{ color: notif === 'granted' ? p.green : p.amber }}>{notif === 'granted' ? 'Allowed' : 'Tap to allow'}</Text>
        </Row>
        {Platform.OS === 'android' ? (
          <Row onPress={() => void Linking.openSettings()}>
            <View style={{ flex: 1 }}>
              <Text style={[styles.label, { color: p.text }]}>Battery</Text>
              <Text style={{ color: p.muted, fontSize: 13 }}>Set LUX Connect to &quot;Unrestricted&quot; so calls ring reliably when the app is closed.</Text>
            </View>
          </Row>
        ) : null}
      </Section>

      <Section title="Devices">
        <Row onPress={() => navigation.navigate('Devices')}><Text style={[styles.label, { color: p.accent }]}>Signed-in devices</Text></Row>
        {me?.is_admin ? <Row onPress={() => navigation.navigate('NumbersAdmin')}><Text style={[styles.label, { color: p.accent }]}>Manage numbers</Text></Row> : null}
      </Section>

      <Section title="Diagnostics">
        <Diag k="Calling" v={`${voice.registration}${voice.blockReason ? ` (${voice.blockReason})` : ''}`} />
        <Diag k="Last registered" v={voice.lastRegisteredAt ? new Date(voice.lastRegisteredAt).toLocaleString() : 'never'} />
        <Diag k="Retry" v={voice.nextRetryAt ? `attempt ${voice.attempts}, next ${new Date(voice.nextRetryAt).toLocaleTimeString()}` : '—'} />
        <Diag k="Last error" v={voice.lastError ?? '—'} />
        <Diag k="Server push" v={me ? `APNs ${me.push.apns ? 'on' : 'off'} · FCM ${me.push.fcm ? 'on' : 'off'}` : '—'} />
        <Diag k="Build" v={APP_VARIANT} />
        <View style={{ padding: 16 }}>
          <Button title="Reconnect now" kind="secondary" onPress={() => void c.voice.refresh()} />
        </View>
      </Section>

      <View style={{ padding: 16 }}>
        <Button title="Sign out" kind="danger" onPress={() => Alert.alert('Sign out?', 'This phone will stop ringing for your LUX Connect number.', [
          { text: 'Cancel', style: 'cancel' }, { text: 'Sign out', style: 'destructive', onPress: () => void c.signOut() },
        ])} />
      </View>
    </ScrollView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const p = usePalette();
  return (
    <View style={{ marginTop: 24 }}>
      <Text style={{ color: p.muted, fontSize: 13, marginLeft: 16, marginBottom: 6, textTransform: 'uppercase' }}>{title}</Text>
      {children}
    </View>
  );
}

function Toggle({ label, detail, value, onChange, disabled }: { label: string; detail?: string; value: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  const p = usePalette();
  return (
    <Row>
      <View style={{ flex: 1 }}>
        <Text style={[styles.label, { color: p.text }]}>{label}</Text>
        {detail ? <Text style={{ color: p.muted, fontSize: 13 }}>{detail}</Text> : null}
      </View>
      <Switch value={value} onValueChange={onChange} disabled={disabled} accessibilityLabel={label} />
    </Row>
  );
}

function Diag({ k, v }: { k: string; v: string }) {
  const p = usePalette();
  return <Row><Text style={{ color: p.muted, width: 120 }}>{k}</Text><Text style={{ color: p.text, flex: 1 }} selectable>{v}</Text></Row>;
}

const styles = StyleSheet.create({ label: { fontSize: 16 } });
