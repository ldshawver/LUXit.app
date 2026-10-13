import { registerRootComponent } from 'expo';
import { prepareIncomingCalls } from './src/voice/twilioAdapter';
import App from './src/App';
import { startupSplash } from './src/ui/StartupScreen';

// Before anything renders or touches the network: a VoIP push that launched
// the app must reach CallKit without waiting for sign-in (pushKitBootstrap.ts).
prepareIncomingCalls().catch((e) => console.log('[voice] early PushKit init failed', String(e)));

// After PushKit, never before it: keep the black LUX splash up until boot is done.
startupSplash.hold();

registerRootComponent(App);
