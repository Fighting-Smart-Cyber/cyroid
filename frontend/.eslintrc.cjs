// ESLint configuration.
//
// This file did not exist. `npm run lint` has been in package.json since the
// project started and could never have passed — eslint, the TypeScript parser
// and plugin, and eslint-plugin-react-hooks were all installed, but there was
// nothing for them to read.
//
// The rule set is deliberately narrow, for the same reason as backend ruff
// config: a gate that fires thousands of findings on a codebase that has never
// been linted gets switched off within a week. These are the rules that catch
// real defects. Widen once green.
//
// `frontend:lint` is `allow_failure: true` in CI until this passes clean.

module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  plugins: ['@typescript-eslint', 'react-hooks'],
  ignorePatterns: [
    'dist',
    'node_modules',
    '.eslintrc.cjs',
    // Playwright specs — different globals and test API from the app and from
    // vitest. They are linted separately if at all.
    'e2e',
  ],
  rules: {
    // `any` is pervasive in the hand-written api.ts, which UX-2 replaces with a
    // generated client. Warn rather than error so the signal is visible without
    // drowning everything else.
    '@typescript-eslint/no-explicit-any': 'warn',
    // Caught by tsc with noUnusedLocals; leaving it on here duplicates errors.
    '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    // This one is load-bearing — a wrong dependency array is a real, subtle bug.
    'react-hooks/exhaustive-deps': 'warn',
    'react-hooks/rules-of-hooks': 'error',
  },
}
