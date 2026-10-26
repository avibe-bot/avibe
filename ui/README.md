# React + TypeScript + Vite

## Development toolchain

Use Node.js `^20.19.0 || ^22.13.0 || >=24` (UI CI uses 22.23.4), then run
`npm ci`. ESLint 10 does not support Node 22.12.x or Node 23.x.

`npm run lint` checks the full TypeScript source domain against the reviewed
per-file/per-rule debt ledger. Do not regenerate it merely to make new errors
pass. The [ESLint 10 migration](../docs/maintenance/eslint-10-migration.md)
records the paired tool upgrade and its one-time, unchanged-source baseline
remeasurement. This development requirement does not change the separately
packaged Show Runtime's Node contract.

Production TSX also enforces `avibe/localized-user-text`: put visible copy and
sentence/list composition in locale keys, `Trans`, or locale-aware formatters,
not literal JSX text, templates, `+`, or `.join` glue. JSX children and
`aria-label`, `title`, `placeholder`, and `alt` are checked, including immutable
local aliases. The [localized-text guard contract](../docs/plans/ui-localized-text-guard.md)
documents narrow decorative/technical exceptions, the initial measured debt,
and the bounded analysis/count-ratchet limitations. Existing debt is not a budget
for new copy; record reductions with `npm run lint:baseline`.

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Babel](https://babeljs.io/) (or [oxc](https://oxc.rs) when used in [rolldown-vite](https://vite.dev/guide/rolldown)) for Fast Refresh
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/) for Fast Refresh

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
