/** Model-list search shared by Manage models and Add models.
 *
 *  Model IDs spell a name with separators (`claude-fable-5-2`) that people type
 *  as words and dots (`Fable 5.2`), and many rows have no display name to fall
 *  back on. So both sides are case-folded and read as tokens split on `-`, `_`,
 *  `.`, `/`, and whitespace; a query matches a field holding its tokens in
 *  order. A plain case-insensitive substring still matches, so a query that
 *  keeps its separators (`5-2`, `gpt-5.`) finds what it always found. */

const SEPARATORS = /[-_./\s]+/;

const normalized = (text: string): string =>
  text.toLowerCase().split(SEPARATORS).filter(Boolean).join(' ');

/** Whether `query` finds a row showing `fields` (ID, label, supplier names). */
export const modelSearchMatches = (query: string, fields: ReadonlyArray<string | null | undefined>): boolean => {
  const needle = query.trim().toLowerCase();
  if (needle === '') return true;
  const shown = fields.filter((field): field is string => !!field);
  if (shown.some((field) => field.toLowerCase().includes(needle))) return true;
  const wanted = normalized(needle);
  return wanted !== '' && shown.some((field) => normalized(field).includes(wanted));
};
