// The Web UI's copy, split by surface when the UI is built
// (`scripts/localeSurfaces.ts`). Load it through src/i18n/app.ts.
declare module 'virtual:avibe-copy/*' {
  const copy: Record<string, unknown>;
  export default copy;
}

declare module 'virtual:avibe-copy/loaders' {
  /** The surfaces with copy of their own, each an i18next namespace. */
  export const SURFACES: string[];
  /** Per language, a loader for `translation` (the first screen's copy) and for each surface's. */
  const parts: Record<'en' | 'zh', Record<string, () => Promise<{ default: object }>>>;
  export default parts;
}
