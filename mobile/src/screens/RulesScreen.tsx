import React, { useCallback, useState } from 'react';
import { Alert, FlatList, Switch, Text, View } from 'react-native';
import { useFocusEffect } from '@react-navigation/native';
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { AutomationRule } from '../api/types';
import { useController } from '../state/hooks';
import { Button, EmptyState, Row } from '../ui/components';
import { usePalette } from '../ui/theme';
import type { RootStackParams } from './navTypes';
import { describeRule } from './ruleText';

export function RulesScreen({ route, navigation }: NativeStackScreenProps<RootStackParams, 'Rules'>) {
  const p = usePalette();
  const c = useController();
  const numberId = route.params?.numberId;
  const [rules, setRules] = useState<AutomationRule[] | null>(null);
  const [editable, setEditable] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await c.api.rules(numberId);
      setRules(res.rules);
      setEditable(res.editable);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [c, numberId]);

  useFocusEffect(useCallback(() => {
    void load();
  }, [load]));

  const toggle = async (r: AutomationRule, on: boolean) => {
    setRules((list) => list?.map((x) => (x.id === r.id ? { ...x, is_active: on } : x)) ?? null);
    try {
      await c.api.updateRule(r.id, { is_active: on });
    } catch (e) {
      Alert.alert('Couldn’t change rule', (e as Error).message);
      void load();
    }
  };

  if (error) return <EmptyState title="Couldn’t load rules" detail={error} />;
  return (
    <View style={{ flex: 1, backgroundColor: p.bg }}>
      <FlatList
        data={rules ?? []}
        keyExtractor={(r) => String(r.id)}
        ListHeaderComponent={<Text style={{ color: p.muted, fontSize: 13, padding: 16 }}>
          Rules run on incoming texts, highest priority first. Texts of STOP, HELP and START are always handled by the system first.
          {editable ? '' : ' Only an administrator can change rules.'}
        </Text>}
        ListEmptyComponent={rules ? <EmptyState title="No rules yet" /> : null}
        renderItem={({ item }) => (
          <Row onPress={editable && !item.system ? () => navigation.navigate('RuleEdit', { ruleId: item.id, numberId }) : undefined}>
            <View style={{ flex: 1 }}>
              <Text style={{ color: p.text, fontSize: 16 }}>{item.name}{item.system ? ' (system)' : ''}</Text>
              <Text style={{ color: p.muted, fontSize: 13 }} numberOfLines={2}>{describeRule(item)}</Text>
            </View>
            <Switch value={item.is_active} disabled={!editable || item.system} onValueChange={(v) => void toggle(item, v)} accessibilityLabel={`${item.name} active`} />
          </Row>
        )}
      />
      {editable ? <View style={{ padding: 16 }}><Button title="New rule" onPress={() => navigation.navigate('RuleEdit', { numberId })} /></View> : null}
    </View>
  );
}
