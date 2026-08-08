/**
 * Read the `message` of an unknown thrown value.
 *
 * A `catch` binding is `unknown`, so reaching `.message` needs a guard. The
 * codebase used to spell that guard as `catch (err: any)`, which typed away the
 * problem instead of solving it and let a non-string `message` land in string
 * state.
 *
 * The helper deliberately stops at *reading* the message and returns
 * `undefined` when there is none. Choosing what to show instead stays at the
 * call site, because the two fallbacks are not interchangeable:
 *
 * - `errorMessage(err) ?? fallback` keeps an empty message as an empty string
 * - `errorMessage(err) || fallback` replaces an empty message with the fallback
 */
export function errorMessage(err: unknown): string | undefined {
  const message = (err as { message?: unknown } | null | undefined)?.message;
  return typeof message === 'string' ? message : undefined;
}
