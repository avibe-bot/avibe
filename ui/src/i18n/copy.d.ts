// One language's copy for one surface, split when the UI is built
// (`scripts/localeSurfaces.ts`). Load it through src/i18n/app.ts.
declare module 'virtual:avibe-copy/*' {
  const copy: Record<string, unknown>;
  export default copy;
}
