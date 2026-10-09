import { registerRootComponent } from 'expo';
import { prepareIncomingCalls } from './src/voice/twilioAdapter';
import App from './src/App';

// Before anything renders or touches the network: a VoIP push that launched
// the app must reach CallKit without waiting for sign-in (pushKitBootstrap.ts).
prepareIncomingCalls().catch((e) => console.log('[voice] early PushKit init failed', String(e)));

registerRootComponent(App);
