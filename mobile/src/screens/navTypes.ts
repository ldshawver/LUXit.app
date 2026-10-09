export type RootStackParams = {
  Tabs: undefined;
  Conversation: { id?: number; title: string; number: string };
  NewMessage: { to?: string } | undefined;
  Settings: undefined;
  Devices: undefined;
  NumbersAdmin: undefined;
};
