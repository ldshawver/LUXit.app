// Pure-logic tests (engine, API client, outbox, phone utils) run under plain
// ts-aware babel-jest in node: they never import react-native or native SDKs.
module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/__tests__'],
  testMatch: ['**/*.test.ts'],
  transform: { '^.+\\.[jt]sx?$': ['babel-jest', { presets: ['babel-preset-expo'] }] },
};
