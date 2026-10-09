import React, { useEffect, useMemo, useRef } from 'react';
import { ActivityIndicator, Pressable, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as Notifications from 'expo-notifications';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { DarkTheme, DefaultTheme, NavigationContainer, createNavigationContainerRef } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { useColorScheme } from 'react-native';
import { CallScreen } from './screens/CallScreen';
import { ContactsScreen } from './screens/ContactsScreen';
import { ConversationScreen } from './screens/ConversationScreen';
import { DevicesScreen } from './screens/DevicesScreen';
import { KeypadScreen } from './screens/KeypadScreen';
import { MessagesScreen } from './screens/MessagesScreen';
import type { RootStackParams } from './screens/navTypes';
import { NewMessageScreen } from './screens/NewMessageScreen';
import { NumbersAdminScreen } from './screens/NumbersAdminScreen';
import { PhoneSettingsScreen } from './screens/PhoneSettingsScreen';
import { RuleEditScreen } from './screens/RuleEditScreen';
import { RulesScreen } from './screens/RulesScreen';
import { RecentsScreen } from './screens/RecentsScreen';
import { SettingsScreen } from './screens/SettingsScreen';
import { SignInScreen } from './screens/SignInScreen';
import { VoicemailScreen } from './screens/VoicemailScreen';
import { useController, useSession, useSync } from './state/hooks';
import { usePalette, type Palette } from './ui/theme';

const Stack = createNativeStackNavigator<RootStackParams>();
const Tab = createBottomTabNavigator();
const navRef = createNavigationContainerRef<RootStackParams>();

Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: true }),
});

const TAB_ICONS: Record<string, keyof typeof Ionicons.glyphMap> = {
  Keypad: 'keypad', Recents: 'time', Messages: 'chatbubbles', Contacts: 'people', Voicemail: 'recording',
};

function Tabs() {
  const p = usePalette();
  const sync = useSync();
  return (
    <Tab.Navigator
      initialRouteName="Keypad"
      screenOptions={({ route, navigation }) => ({
        tabBarIcon: ({ color, size }) => <Ionicons name={TAB_ICONS[route.name] ?? 'ellipse'} color={color} size={size} />,
        tabBarActiveTintColor: p.accent,
        headerRight: () => (
          <View style={{ flexDirection: 'row', gap: 18, marginRight: 16 }}>
            {route.name === 'Messages' ? (
              <Pressable accessibilityLabel="New message" hitSlop={10} onPress={() => navigation.getParent()?.navigate('NewMessage')}>
                <Ionicons name="create-outline" size={24} color={p.accent} />
              </Pressable>
            ) : null}
            <Pressable accessibilityLabel="Settings" hitSlop={10} onPress={() => navigation.getParent()?.navigate('Settings')}>
              <Ionicons name="settings-outline" size={24} color={p.accent} />
            </Pressable>
          </View>
        ),
      })}
    >
      <Tab.Screen name="Keypad" component={KeypadScreen} />
      <Tab.Screen name="Recents" component={RecentsScreen} options={{ tabBarBadge: sync.missedCalls || undefined }} />
      <Tab.Screen name="Messages" component={MessagesScreen as never} options={{ tabBarBadge: sync.unreadMessages || undefined }} />
      <Tab.Screen name="Contacts" component={ContactsScreen as never} />
      <Tab.Screen name="Voicemail" component={VoicemailScreen} />
    </Tab.Navigator>
  );
}

function SignedIn() {
  return (
    <>
      <Stack.Navigator>
        <Stack.Screen name="Tabs" component={Tabs} options={{ headerShown: false }} />
        <Stack.Screen name="Conversation" component={ConversationScreen} options={{ title: '' }} />
        <Stack.Screen name="NewMessage" component={NewMessageScreen} options={{ title: 'New message', presentation: 'modal' }} />
        <Stack.Screen name="Settings" component={SettingsScreen} />
        <Stack.Screen name="Devices" component={DevicesScreen} options={{ title: 'Devices' }} />
        <Stack.Screen name="NumbersAdmin" component={NumbersAdminScreen} options={{ title: 'Numbers' }} />
        <Stack.Screen name="PhoneSettings" component={PhoneSettingsScreen} options={{ title: 'Number' }} />
        <Stack.Screen name="Rules" component={RulesScreen} options={{ title: 'Automatic replies and rules' }} />
        <Stack.Screen name="RuleEdit" component={RuleEditScreen} options={{ title: 'Rule' }} />
      </Stack.Navigator>
      <CallScreen />
    </>
  );
}

/** Notification tap -> the right screen. Data comes from services/native_push.py. */
function useNotificationRouting(enabled: boolean) {
  const handled = useRef(new Set<string>());
  useEffect(() => {
    if (!enabled) return undefined;
    const go = (resp: Notifications.NotificationResponse | null) => {
      if (!resp || handled.current.has(resp.notification.request.identifier)) return;
      handled.current.add(resp.notification.request.identifier);
      const data = (resp.notification.request.content.data ?? {}) as Record<string, string>;
      if (!navRef.isReady()) return;
      if (data.route === 'conversation' && data.conversation_id) {
        navRef.navigate('Conversation', { id: Number(data.conversation_id), title: resp.notification.request.content.title ?? '', number: '' });
      } else if (data.route === 'voicemail' || data.route === 'recents' || data.route === 'messages') {
        navRef.navigate('Tabs', { screen: data.route === 'voicemail' ? 'Voicemail' : data.route === 'recents' ? 'Recents' : 'Messages' } as never);
      }
    };
    void Notifications.getLastNotificationResponseAsync().then(go);
    const sub = Notifications.addNotificationResponseReceivedListener(go);
    return () => sub.remove();
  }, [enabled]);
}

function navTheme(dark: boolean, p: Palette) {
  const base = dark ? DarkTheme : DefaultTheme;
  return { ...base, colors: { ...base.colors, primary: p.accent, background: p.bg, card: dark ? p.surface : base.colors.card, text: p.text, border: p.border } };
}

export default function App() {
  const c = useController();
  const session = useSession();
  const scheme = useColorScheme();
  const palette = usePalette();
  const theme = useMemo(() => navTheme(scheme === 'dark', palette), [scheme, palette]);
  useEffect(() => {
    void c.boot();
  }, [c]);
  useNotificationRouting(session.status === 'signed_in');

  return (
    <SafeAreaProvider>
      <StatusBar style="auto" />
      <NavigationContainer ref={navRef} theme={theme}>
        {session.status === 'booting' ? (
          <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center' }}><ActivityIndicator /></View>
        ) : session.status === 'signed_out' ? (
          <SignInScreen />
        ) : (
          <SignedIn />
        )}
      </NavigationContainer>
    </SafeAreaProvider>
  );
}
