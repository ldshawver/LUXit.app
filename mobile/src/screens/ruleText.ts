import type { AutomationRule, RuleAction, RuleTrigger } from '../api/types';
import { formatPhone } from '../lib/phone';

export const TRIGGER_LABELS: Record<Exclude<RuleTrigger, 'stop_keyword'>, string> = {
  keyword_contains: 'Message contains',
  keyword_exact: 'Message is exactly',
  regex: 'Message matches pattern',
  first_contact: 'First message from someone',
  after_hours: 'After business hours',
  always: 'Every message',
};

export const ACTION_LABELS: Record<RuleAction, string> = {
  reply: 'Reply',
  forward: 'Forward a copy',
  tag: 'Tag the contact',
  opt_out: 'Opt the sender out',
};

/** One line describing what a rule does. */
export function describeRule(r: Pick<AutomationRule, 'trigger_type' | 'keywords' | 'action' | 'forward_to' | 'tag_value' | 'response'>): string {
  const when = r.trigger_type === 'stop_keyword' ? 'STOP keywords'
    : `${TRIGGER_LABELS[r.trigger_type]}${r.keywords.length ? ` “${r.keywords.join('”, “')}”` : ''}`;
  let what: string;
  if (r.action === 'forward') what = `forward to ${r.forward_to ? formatPhone(r.forward_to) : '?'}`;
  else if (r.action === 'tag') what = `tag “${r.tag_value ?? ''}”`;
  else if (r.action === 'opt_out') what = 'opt out';
  else what = `reply “${r.response.length > 40 ? `${r.response.slice(0, 40)}…` : r.response}”`;
  return `${when} → ${what}`;
}
