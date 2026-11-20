import React, { useEffect, useState } from 'react';
import { ActivityIndicator, Alert, ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { AutomationRule, RuleAction, RuleTrigger } from '../api/types';
import { toE164 } from '../lib/phone';
import { useController, useSession } from '../state/hooks';
import { Button } from '../ui/components';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';
import { ACTION_LABELS, TRIGGER_LABELS } from './ruleText';

type EditableTrigger = Exclude<RuleTrigger, 'stop_keyword'>;
const TRIGGERS = Object.keys(TRIGGER_LABELS) as EditableTrigger[];
const ACTIONS = Object.keys(ACTION_LABELS) as RuleAction[];
const KEYWORD_TRIGGERS: RuleTrigger[] = ['keyword_contains', 'keyword_exact', 'regex'];

/** Create or edit one rule. Validation is the server's (services/auto_reply_rules.py); errors are shown as returned. */
export function RuleEditScreen({ route, navigation }: NativeStackScreenProps<RootStackParams, 'RuleEdit'>) {
  const p = usePalette();
  const c = useController();
  const { me } = useSession();
  const ruleId = route.params?.ruleId;
  const [loaded, setLoaded] = useState(!ruleId);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState('');
  const [trigger, setTrigger] = useState<EditableTrigger>('keyword_contains');
  const [keywords, setKeywords] = useState('');
  const [action, setAction] = useState<RuleAction>('reply');
  const [response, setResponse] = useState('');
  const [forwardTo, setForwardTo] = useState('');
  const [tag, setTag] = useState('');
  const [numberId, setNumberId] = useState<number | null>(route.params?.numberId ?? null);
  const [active, setActive] = useState(true);

  useEffect(() => {
    navigation.setOptions({ title: ruleId ? 'Edit rule' : 'New rule' });
    if (!ruleId) return;
    void (async () => {
      try {
        const res = await c.api.rules();
        const r = res.rules.find((x) => x.id === ruleId);
        if (!r) throw new Error('Rule not found.');
        setName(r.name);
        setTrigger(r.trigger_type === 'stop_keyword' ? 'keyword_contains' : r.trigger_type);
        setKeywords(r.keywords.join(', '));
        setAction(r.action);
        setResponse(r.response);
        setForwardTo(r.forward_to ?? '');
        setTag(r.tag_value ?? '');
        setNumberId(r.phone_number_id);
        setActive(r.is_active);
        setLoaded(true);
      } catch (e) {
        Alert.alert('Couldn’t load rule', (e as Error).message);
        navigation.goBack();
      }
    })();
  }, [c, navigation, ruleId]);

  const save = async () => {
    if (action === 'forward' && !toE164(forwardTo)) {
      Alert.alert('Forward to', 'Enter the number to forward to, with area code.');
      return;
    }
    const body: Partial<AutomationRule> = {
      name, trigger_type: trigger, action, response, is_active: active, phone_number_id: numberId,
      keywords: KEYWORD_TRIGGERS.includes(trigger) ? keywords.split(',').map((k) => k.trim()).filter(Boolean) : [],
      forward_to: action === 'forward' ? toE164(forwardTo) : null,
      tag_value: action === 'tag' ? tag : null,
    };
    setBusy(true);
    try {
      if (ruleId) await c.api.updateRule(ruleId, body);
      else await c.api.createRule(body);
      navigation.goBack();
    } catch (e) {
      Alert.alert('Couldn’t save rule', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = () => Alert.alert('Delete rule?', name, [
    { text: 'Cancel', style: 'cancel' },
    { text: 'Delete', style: 'destructive', onPress: async () => {
      try {
        await c.api.deleteRule(ruleId!);
        navigation.goBack();
      } catch (e) {
        Alert.alert('Couldn’t delete', (e as Error).message);
      }
    } },
  ]);

  if (!loaded) return <View style={[styles.center, { backgroundColor: p.bg }]}><ActivityIndicator /></View>;
  const input = [styles.input, { color: p.text, backgroundColor: p.surface }];
  const chips = <T extends string>(values: T[], labels: Record<T, string>, value: T, set: (v: T) => void) => (
    <View style={styles.chips}>
      {values.map((v) => (
        <Text key={v} accessibilityRole="button" accessibilityState={{ selected: v === value }} onPress={() => set(v)}
          style={[styles.chip, { color: v === value ? '#fff' : p.text, backgroundColor: v === value ? p.accent : p.surface }]}>{labels[v]}</Text>
      ))}
    </View>
  );
  const numbers = me?.numbers ?? [];
  const numberLabels: Record<string, string> = { all: 'All my numbers', ...Object.fromEntries(numbers.map((n) => [String(n.id), n.friendly_name])) };

  return (
    <ScrollView style={{ backgroundColor: p.bg }} contentContainerStyle={{ padding: 16, paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
      <Label text="Name" />
      <TextInput value={name} onChangeText={setName} placeholder="e.g. Store hours" placeholderTextColor={p.muted} style={input} accessibilityLabel="Rule name" />

      <Label text="When" />
      {chips(TRIGGERS, TRIGGER_LABELS, trigger, setTrigger)}
      {KEYWORD_TRIGGERS.includes(trigger) ? (
        <TextInput value={keywords} onChangeText={setKeywords} placeholder={trigger === 'regex' ? 'Pattern' : 'Keywords, separated by commas'}
          placeholderTextColor={p.muted} autoCapitalize="none" style={input} accessibilityLabel="Keywords" />
      ) : null}

      <Label text="Then" />
      {chips(ACTIONS, ACTION_LABELS, action, setAction)}
      {action === 'forward' ? (
        <>
          <TextInput value={forwardTo} onChangeText={setForwardTo} placeholder="Forward to (+1 555 123 4567)" keyboardType="phone-pad"
            placeholderTextColor={p.muted} style={input} accessibilityLabel="Forward to" />
          <Text style={[styles.hint, { color: p.muted }]}>A copy of the customer’s text is sent to this phone. Replies from it are not sent to the customer; use Text forwarding on the number for that.</Text>
        </>
      ) : null}
      {action === 'tag' ? <TextInput value={tag} onChangeText={setTag} placeholder="Tag" placeholderTextColor={p.muted} style={input} accessibilityLabel="Tag" /> : null}
      <TextInput value={response} onChangeText={setResponse} multiline
        placeholder={action === 'reply' ? 'Reply message' : 'Optional reply to the customer'} placeholderTextColor={p.muted}
        style={[input, { minHeight: 80, textAlignVertical: 'top', paddingTop: 12 }]} accessibilityLabel="Reply message" />

      {numbers.length > 1 ? (
        <>
          <Label text="Applies to" />
          {chips(['all', ...numbers.map((n) => String(n.id))], numberLabels, numberId == null ? 'all' : String(numberId), (v) => setNumberId(v === 'all' ? null : Number(v)))}
        </>
      ) : null}

      <View style={styles.activeRow}>
        <Text style={{ color: p.text, fontSize: 16, flex: 1 }}>Active</Text>
        <Switch value={active} onValueChange={setActive} accessibilityLabel="Active" />
      </View>

      <Button title="Save" onPress={() => void save()} busy={busy} disabled={!name.trim()} />
      {ruleId ? <Button title="Delete rule" kind="danger" onPress={remove} style={{ marginTop: 12 }} /> : null}
    </ScrollView>
  );
}

function Label({ text }: { text: string }) {
  const p = usePalette();
  return <Text style={{ color: p.muted, fontSize: 13, marginTop: 18, marginBottom: 6, textTransform: 'uppercase' }}>{text}</Text>;
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  input: { minHeight: 46, borderRadius: 10, paddingHorizontal: 12, fontSize: 16, marginTop: 8 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 16, overflow: 'hidden', fontSize: 14 },
  hint: { fontSize: 13, marginTop: 6 },
  activeRow: { flexDirection: 'row', alignItems: 'center', marginVertical: 20 },
});
