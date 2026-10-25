import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'
import localizedUserText from './scripts/localizedUserText.mjs'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: { avibe: { rules: { 'localized-user-text': localizedUserText } } },
    rules: {
      'avibe/localized-user-text': 'error',
      // The codebase already marks a binding as deliberately unused by naming it
      // `_something` — an argument kept for signature shape, a destructured slot
      // being skipped, a caught error that is intentionally swallowed. Opt the
      // rule into that convention instead of asking every such site for a
      // suppression comment. Anything not explicitly marked still errors, which
      // `scripts/eslintConventions.test.mjs` pins.
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          destructuredArrayIgnorePattern: '^_',
        },
      ],
    },
  },
])
