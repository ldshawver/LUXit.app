import { formatAsTyped, formatDuration, formatPhone, initials, toE164 } from '../src/lib/phone';
import { backoffDelay } from '../src/lib/backoff';
import { relativeTime } from '../src/screens/time';

test('E.164 normalisation', () => {
  expect(toE164('(415) 555-0100')).toBe('+14155550100');
  expect(toE164('1 415 555 0100')).toBe('+14155550100');
  expect(toE164('+44 20 7946 0958')).toBe('+442079460958');
  expect(toE164('555-0100')).toBeNull();
  expect(toE164('')).toBeNull();
});

test('display formatting', () => {
  expect(formatPhone('+14155550100')).toBe('(415) 555-0100');
  expect(formatPhone('+442079460958')).toBe('+442079460958');
  expect(formatAsTyped('415555')).toBe('(415) 555');
  expect(formatAsTyped('14155550100')).toBe('1 (415) 555-0100');
  expect(initials('Ann Marie Lee')).toBe('AL');
  expect(initials('+1 415')).toBe('#');
  expect(formatDuration(84)).toBe('1:24');
  expect(formatDuration(3725)).toBe('1:02:05');
});

test('backoff grows, is capped and never collapses to zero', () => {
  expect(backoffDelay(0, { random: () => 0 })).toBe(500);
  expect(backoffDelay(3, { random: () => 1 })).toBe(8000);
  expect(backoffDelay(50, { random: () => 1 })).toBe(300000);
  expect(backoffDelay(50, { random: () => 0 })).toBe(150000);
});

test('server times without a zone are treated as UTC', () => {
  const now = new Date('2026-10-09T18:00:00Z');
  expect(relativeTime('2026-10-01T10:00:00', now)).not.toBe('');
  expect(relativeTime(null, now)).toBe('');
});
