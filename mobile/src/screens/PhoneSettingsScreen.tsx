import React, { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, Alert, ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { CallRouting, NumberSettings } from '../api/types';
import { formatPhone, toE164 } from '../lib/phone';
import { useController } from '../state/hooks';
import { Button, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';
import { routingSummary } from './routingText';

type When = 'business' | 'after' | 'always';

function forwardWhen(s: NumberSettings): When | null {
  const day = s.during_hours_route === 'forward';
  const night = s.after_hours_route === 'forward';
  if (!s.call_forwarding_enabled || (!day && !night)) return null;
  return day && night ? 'always' : day ? 'business' : 'after';
}

/**
 * One number's forwarding and automatic replies. Everything is stored by the
 * same /phone/numbers/<id>/settings the web app uses; the routing summary is
 * the server's own call-routing decision, re-read after every save.
 */
export function PhoneSettingsScreen({ route, navigation }: NativeStackScreenProps<RootStackParams, 'PhoneSettings'>) {
  const p = usePalette();
  const c = useController();
  const id = route.params.numberId;
  const [s, setS] = useState<NumberSettings | null>(null);
  const [editable, setEditable] = useState(false);
  const [routing, setRouting] = useState<CallRouting | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [smsTo, setSmsTo] = useState('');
  const [callTo, setCallTo] = useState('');
  const [when, setWhen] = useState<When>('always');
  const [bhText, setBhText] = useState('');
  const [ahText, setAhText] = useState('');

  const load = useCallback(async () => {
    try {
      const [res, r] = await Promise.all([c.api.numberSettings(id), c.api.numberRouting(id)]);
      setS(res.settings);
      setEditable(res.editable);
      setRouting(r.routing);
      setSmsTo(res.settings.sms_forward_to ?? '');
      setCallTo(res.settings.call_forwarding_number ?? '');
      setWhen(forwardWhen(res.settings) ?? 'always');
      setBhText(res.settings.business_hours_auto_reply_text ?? '');
      setAhText(res.settings.after_hours_text ?? '');
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [c, id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (s) navigation.setOptions({ title: s.friendly_name || formatPhone(s.phone_number) });
  }, [navigation, s]);

  const save = async (fields: Partial<NumberSettings>) => {
    setBusy(true);
    try {
      const res = await c.api.saveNumberSettings(id, fields);
      setS(res.settings);
      setRouting((await c.api.numberRouting(id)).routing);
      void c.refreshMe();
    } catch (e) {
      Alert.alert('Couldn’t save', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (error) return <View style={[styles.center, { backgroundColor: p.bg }]}><Text style={{ color: p.red }}>{error}</Text><Button title="Try again" kind="secondary" onPress={() => void load()} style={{ marginTop: 12 }} /></View>;
  if (!s) return <View style={[styles.center, { backgroundColor: p.bg }]}><ActivityIndicator /></View>;

  const fwdOn = forwardWhen(s) !== null;
  const smsOn = Boolean(s.sms_forwarding_enabled && s.sms_forward_to);

  const applyCallForward = (on: boolean, nextWhen: When = when) => {
    if (!on) {
      void save({
        call_forwarding_enabled: false,
        during_hours_route: s.during_hours_route === 'forward' ? 'ring_pwa' : s.during_hours_route,
        after_hours_route: s.after_hours_route === 'forward' ? 'voicemail' : s.after_hours_route,
      });
      return;
    }
    const to = toE164(callTo);
    if (!to) {
      Alert.alert('Forward calls to', 'Enter the number to forward calls to, with area code.');
      return;
    }
    void save({
      call_forwarding_enabled: true,
      call_forwarding_number: to,
      during_hours_route: nextWhen === 'after' ? (s.during_hours_route === 'forward' ? 'ring_pwa' : s.during_hours_route) : 'forward',
      after_hours_route: nextWhen === 'business' ? (s.after_hours_route === 'forward' ? 'voicemail' : s.after_hours_route) : 'forward',
    });
  };

  const applySmsForward = (on: boolean) => {
    if (!on) {
      void save({ sms_forwarding_enabled: false });
      return;
    }
    const to = toE164(smsTo);
    if (!to) {
      Alert.alert('Forward texts to', 'Enter the number to forward texts to, with area code.');
      return;
    }
    void save({ sms_forwarding_enabled: true, sms_forward_to: to });
  };

  const summary = routingSummary(routing);
  return (
    <ScrollView style={{ backgroundColor: p.bg }} contentContainerStyle={{ paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
      <Section title="Where calls go">
        <Row><View style={{ flex: 1 }}>
          <Text style={[styles.label, { color: summary.bypassesApp ? p.amber : p.text }]}>{summary.now}</Text>
          {summary.detail ? <Text style={{ color: p.muted, fontSize: 13, marginTop: 2 }}>{summary.detail}</Text> : null}
        </View></Row>
      </Section>

      {!editable ? (
        <Text style={[styles.note, { color: p.muted }]}>Only an administrator can change forwarding and automatic replies for this number.</Text>
      ) : null}

      <Section title="Call forwarding">
        <ToggleRow label="Forward calls" detail="Forwarded calls go straight to the other phone. LUX Connect and the web app do not ring."
          value={fwdOn} disabled={!editable || busy} onChange={(v) => applyCallForward(v)} />
        <Field label="Forward to" value={callTo} onChange={setCallTo} editable={editable && !busy} />
        <View style={styles.segment}>
          {(['business', 'after', 'always'] as When[]).map((w) => (
            <Text key={w} accessibilityRole="button" accessibilityState={{ selected: when === w }}
              onPress={() => { if (!editable || busy) return; setWhen(w); if (fwdOn) applyCallForward(true, w); }}
              style={[styles.seg, { color: when === w ? '#fff' : p.text, backgroundColor: when === w ? p.accent : p.surface }]}>
              {w === 'business' ? 'Business hours' : w === 'after' ? 'After hours' : 'Always'}
            </Text>
          ))}
        </View>
        {fwdOn && callTo !== (s.call_forwarding_number ?? '') ? (
          <View style={styles.pad}><Button title="Save number" onPress={() => applyCallForward(true)} busy={busy} /></View>
        ) : null}
      </Section>

      <Section title="Text forwarding">
        <ToggleRow label="Forward texts" detail={'A copy of each incoming text is sent to this phone. Reply from it with "reply +1… message" to answer from your business number.'}
          value={smsOn} disabled={!editable || busy} onChange={applySmsForward} />
        <Field label="Forward to" value={smsTo} onChange={setSmsTo} editable={editable && !busy} />
        {smsOn && toE164(smsTo) !== s.sms_forward_to ? (
          <View style={styles.pad}><Button title="Save number" onPress={() => applySmsForward(true)} busy={busy} /></View>
        ) : null}
      </Section>

      <Section title="Automatic replies">
        <ToggleRow label="Automatic replies" detail="Sent at most once per conversation per cooldown period. Never sent to anyone who replied STOP."
          value={s.auto_reply_enabled !== false} disabled={!editable || busy} onChange={(v) => void save({ auto_reply_enabled: v })} />
        <ToggleRow label="During business hours" value={Boolean(s.business_hours_auto_reply_enabled)} disabled={!editable || busy}
          onChange={(v) => void save({ business_hours_auto_reply_enabled: v, business_hours_auto_reply_text: bhText })} />
        <Field label="Reply" value={bhText} onChange={setBhText} editable={editable && !busy} multiline />
        <ToggleRow label="After hours" value={Boolean(s.after_hours_sms_enabled)} disabled={!editable || busy}
          onChange={(v) => void save({ after_hours_sms_enabled: v, after_hours_text: ahText })} />
        <Field label="Reply" value={ahText} onChange={setAhText} editable={editable && !busy} multiline />
        {editable && (bhText !== (s.business_hours_auto_reply_text ?? '') || ahText !== (s.after_hours_text ?? '')) ? (
          <View style={styles.pad}><Button title="Save replies" busy={busy} onPress={() => void save({ business_hours_auto_reply_text: bhText, after_hours_text: ahText })} /></View>
        ) : null}
      </Section>

      <Section title="Rules">
        <Row onPress={() => navigation.navigate('Rules', { numberId: id })}>
          <Text style={[styles.label, { color: p.accent }]}>Keyword rules and automation</Text>
        </Row>
      </Section>
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

function ToggleRow({ label, detail, value, onChange, disabled }: { label: string; detail?: string; value: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
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

function Field({ label, value, onChange, editable, multiline }: { label: string; value: string; onChange: (v: string) => void; editable: boolean; multiline?: boolean }) {
  const p = usePalette();
  return (
    <View style={[styles.field, { borderBottomColor: p.border }]}>
      <Text style={{ color: p.muted, fontSize: 13 }}>{label}</Text>
      <TextInput value={value} onChangeText={onChange} editable={editable} multiline={multiline}
        keyboardType={multiline ? 'default' : 'phone-pad'} placeholder={multiline ? 'Message' : '+1 555 123 4567'} placeholderTextColor={p.muted}
        accessibilityLabel={label} style={[styles.input, { color: p.text, minHeight: multiline ? 60 : 36 }]} />
    </View>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 24 },
  label: { fontSize: 16 },
  note: { marginHorizontal: 16, marginTop: 12, fontSize: 13 },
  field: { paddingHorizontal: 16, paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth },
  input: { fontSize: 16, paddingVertical: 4 },
  segment: { flexDirection: 'row', gap: 8, padding: 16 },
  seg: { flex: 1, textAlign: 'center', paddingVertical: 8, borderRadius: 8, overflow: 'hidden', fontSize: 13 },
  pad: { paddingHorizontal: 16, paddingBottom: 8 },
});
