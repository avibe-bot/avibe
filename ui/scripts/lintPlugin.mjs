import { builtinRules } from 'eslint/use-at-your-own-risk';
import localizedUserText from './localizedUserText.mjs';

// This is an Oxlint JS plugin, not a second ESLint pass. React's plugins require
// the ESLint peer anyway; reuse its two required core implementations instead
// of forking them. Native no-useless-assignment misses try/finally writes.
export default {
  meta: { name: 'avibe' },
  rules: {
    'localized-user-text': localizedUserText,
    'no-octal': builtinRules.get('no-octal'),
    'no-useless-assignment': builtinRules.get('no-useless-assignment'),
  },
};
