/* iOS incoming calls from a closed / locked / background launch: PushKit must be
 * live before sign-in or any network request, CallKit configured exactly once
 * and before the registry, and the registry created exactly once. */
import { CALLKIT_CONFIGURATION, INCOMING_CALL_HANDLE_TEMPLATE, createPushKitBootstrap, type PushKitNative } from '../src/voice/pushKitBootstrap';

function fakeNative(overrides: Partial<Record<keyof PushKitNative, () => Promise<void>>> = {}) {
  const calls: string[] = [];
  const native: PushKitNative = {
    setCallKitConfiguration: jest.fn(async () => { calls.push('callkit'); await overrides.setCallKitConfiguration?.(); }),
    setIncomingCallContactHandleTemplate: jest.fn(async () => { calls.push('template'); await overrides.setIncomingCallContactHandleTemplate?.(); }),
    initializePushRegistry: jest.fn(async () => { calls.push('registry'); await overrides.initializePushRegistry?.(); }),
  };
  return { native, calls };
}

describe('createPushKitBootstrap', () => {
  test('configures CallKit before creating the PushKit registry (provider is never replaced after a push can arrive)', async () => {
    const { native, calls } = fakeNative();
    await createPushKitBootstrap(native)();
    expect(calls).toEqual(['callkit', 'template', 'registry']);
    expect(native.setCallKitConfiguration).toHaveBeenCalledWith({ ...CALLKIT_CONFIGURATION, callKitSupportedHandleTypes: [2] });
    expect(native.setIncomingCallContactHandleTemplate).toHaveBeenCalledWith(INCOMING_CALL_HANDLE_TEMPLATE);
  });

  test('concurrent and repeated calls create one registry and configure CallKit once', async () => {
    const { native, calls } = fakeNative();
    const boot = createPushKitBootstrap(native);
    const a = boot();
    const b = boot();
    expect(a).toBe(b);
    await Promise.all([a, b, boot()]);
    await boot();
    expect(calls).toEqual(['callkit', 'template', 'registry']);
  });

  test('a CallKit configuration failure does not block the registry', async () => {
    const log = jest.fn();
    const { native, calls } = fakeNative({ setCallKitConfiguration: async () => { throw new Error('boom'); } });
    await createPushKitBootstrap(native, log)();
    expect(calls).toEqual(['callkit', 'template', 'registry']);
    expect(log).toHaveBeenCalledWith('setCallKitConfiguration failed', expect.anything());
  });

  test('a registry failure can be retried, without reconfiguring CallKit', async () => {
    let fail = true;
    const { native, calls } = fakeNative({ initializePushRegistry: async () => { if (fail) throw new Error('nope'); } });
    const boot = createPushKitBootstrap(native);
    await expect(boot()).rejects.toThrow('nope');
    fail = false;
    await boot();
    await boot();
    expect(calls).toEqual(['callkit', 'template', 'registry', 'registry']);
  });
});

describe('app entry (index.ts) with the real Twilio adapter', () => {
  function loadEntry(os: 'ios' | 'android') {
    jest.resetModules();
    const order: string[] = [];
    const voiceInstances: unknown[] = [];
    const fetchSpy = jest.fn();
    (global as any).fetch = fetchSpy;
    jest.doMock('react-native', () => ({ Platform: { OS: os } }), { virtual: true });
    jest.doMock('@twilio/voice-react-native-sdk', () => ({
      Voice: class {
        constructor() { voiceInstances.push(this); }
        on() { return this; }
        async setCallKitConfiguration() { order.push('callkit'); }
        async setIncomingCallContactHandleTemplate() { order.push('template'); }
        async initializePushRegistry() { order.push('registry'); }
        async showAvRoutePickerView() {}
      },
    }), { virtual: true });
    jest.doMock('expo', () => ({ registerRootComponent: () => order.push('render') }), { virtual: true });
    // The App tree (controller, session restore, API) must not be needed for PushKit.
    jest.doMock('../src/App', () => ({ __esModule: true, default: () => null }));
    jest.doMock('../src/ui/StartupScreen', () => ({ startupSplash: { hold: () => order.push('splash') } }));
    let adapter: any;
    jest.isolateModules(() => {
      require('../index');
      adapter = require('../src/voice/twilioAdapter');
    });
    return { order, voiceInstances, fetchSpy, adapter };
  }

  const flush = () => new Promise((r) => setImmediate(r));

  test('iOS: PushKit is initialized at startup, with no sign-in or network, and the engine reuses it', async () => {
    const { order, voiceInstances, fetchSpy, adapter } = loadEntry('ios');
    expect(order[0]).toBe('callkit'); // started before the app renders
    expect(order.slice(1)).toEqual(['splash', 'render']); // the launch splash never runs ahead of CallKit
    await flush();
    expect(order.filter((s) => s === 'registry')).toHaveLength(1);
    expect(order.indexOf('callkit')).toBeLessThan(order.indexOf('registry'));
    expect(fetchSpy).not.toHaveBeenCalled();

    // VoiceEngine.start() later calls sdk.initializePushRegistry(): same SDK
    // instance, no second registry, no CallKit provider replacement.
    const sdk = adapter.createTwilioSdk();
    await sdk.initializePushRegistry();
    await sdk.initializePushRegistry();
    expect(voiceInstances).toHaveLength(1);
    expect(order.filter((s) => s === 'registry')).toHaveLength(1);
    expect(order.filter((s) => s === 'callkit')).toHaveLength(1);
  });

  test('Android: no PushKit work (Twilio FCM service is native)', async () => {
    const { order } = loadEntry('android');
    await flush();
    expect(order).toEqual(['splash', 'render']);
  });
});
