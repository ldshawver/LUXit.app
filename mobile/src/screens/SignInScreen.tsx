import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { ApiError, NetworkError } from '../api/http';
import { APP_VARIANT } from '../state/controller';
import { useController, useSession } from '../state/hooks';
import { Button } from '../ui/components';
import { usePalette } from '../ui/theme';

export function SignInScreen() {
  const p = usePalette();
  const c = useController();
  const session = useSession();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await c.signIn(email.trim(), password);
    } catch (e) {
      setError(e instanceof NetworkError ? 'No connection. Check your internet and try again.'
        : e instanceof ApiError ? e.message : 'Sign-in failed. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <SafeAreaView style={[styles.fill, { backgroundColor: p.bg }]}>
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.body}>
        <Text style={[styles.brand, { color: p.text }]}>LUX Connect</Text>
        <Text style={{ color: p.muted, marginBottom: 28 }}>Your business line, on your phone.{APP_VARIANT !== 'production' ? ' (Staging)' : ''}</Text>
        {session.signOutReason ? <Text style={[styles.notice, { color: p.amber }]}>{session.signOutReason}</Text> : null}
        <TextInput value={email} onChangeText={setEmail} placeholder="Email" autoCapitalize="none" autoComplete="email"
          keyboardType="email-address" textContentType="username" placeholderTextColor={p.muted}
          style={[styles.input, { color: p.text, backgroundColor: p.surface }]} />
        <TextInput value={password} onChangeText={setPassword} placeholder="Password" secureTextEntry autoComplete="password"
          textContentType="password" placeholderTextColor={p.muted} onSubmitEditing={submit}
          style={[styles.input, { color: p.text, backgroundColor: p.surface }]} />
        {error ? <Text accessibilityLiveRegion="polite" style={{ color: p.red, marginBottom: 12 }}>{error}</Text> : null}
        <Button title="Sign in" onPress={submit} busy={busy} disabled={!email || !password} />
        <View style={{ flex: 1 }} />
        <Text style={{ color: p.muted, fontSize: 12, textAlign: 'center' }}>Use the same email and password as LUXit.</Text>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  body: { flex: 1, padding: 24, paddingTop: 64 },
  brand: { fontSize: 32, fontWeight: '700', marginBottom: 6 },
  notice: { marginBottom: 16 },
  input: { height: 50, borderRadius: 12, paddingHorizontal: 14, fontSize: 17, marginBottom: 12 },
});
