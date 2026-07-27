// The Web UI half of the message-type policy catalog. This reads the SAME tracked
// file as ``vibe/message_types.py`` — there is deliberately no TypeScript copy of
// the type names or their properties. Vite inlines the JSON at build time, so the
// browser bundle carries the values with no runtime coupling to the Python package.
//
// Property NAMES are derived from the catalog's ``defaults`` block via ``keyof``,
// so renaming a property in the JSON fails ``tsc -b`` at every call site instead of
// silently reading ``undefined``. Value VALIDATION is not repeated here: the Python
// loader is the validating authority for this file (it raises on an unknown property,
// a wrong value kind, or an unsupported enum member) and its tests gate CI.
import catalog from '../../../vibe/message_types.json';

// ``defaults`` declares every public property exactly once, which makes it the
// natural source for the property-name set.
type CatalogDefaults = typeof catalog.defaults;

// Empty JSON arrays infer as ``never[]``; the catalog's list properties are all
// lists of strings, so widen them and freeze everything else as-is.
type SpecValue<T> = T extends readonly unknown[] ? readonly string[] : T;

export type MessageTypeProperty = keyof CatalogDefaults;

export type MessageTypeSpec = {
  readonly [K in MessageTypeProperty]: SpecValue<CatalogDefaults[K]>;
};

const DEFAULT_SPEC: MessageTypeSpec = catalog.defaults;

// Catalog order is the JSON's key order (``Object.entries`` on string keys, then a
// ``Map``), matching the ordering the Python reader relies on.
const TYPE_SPECS: ReadonlyMap<string, MessageTypeSpec> = new Map(
  Object.entries(catalog.types).map(([messageType, overrides]): [string, MessageTypeSpec] => [
    messageType,
    { ...DEFAULT_SPEC, ...overrides },
  ]),
);

/** Resolved spec for *messageType*; unknown types receive the catalog defaults. */
export const specFor = (messageType: string): MessageTypeSpec =>
  TYPE_SPECS.get(messageType) ?? DEFAULT_SPEC;

/** Declared message types in catalog order (the enumeration tests assert against). */
export const messageTypeNames = (): readonly string[] => [...TYPE_SPECS.keys()];
