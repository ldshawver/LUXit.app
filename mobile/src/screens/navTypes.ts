export type RootStackParams = {
  Tabs: undefined;
  Conversation: { id?: number; title: string; number: string; fromNumberId?: number };
  NewMessage: { to?: string } | undefined;
  Settings: undefined;
  Devices: undefined;
  NumbersAdmin: undefined;
  PhoneSettings: { numberId: number };
  Rules: { numberId?: number } | undefined;
  RuleEdit: { ruleId?: number; numberId?: number } | undefined;
};
