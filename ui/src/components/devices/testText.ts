// For tests: a sentence as the page says it, however its paragraph is split into spans to wrap.
// Matches the innermost element that holds the whole text.
export const said = (text: string | RegExp) => (_: string, node: Element | null): boolean => {
  if (!node) return false;
  const has = (value: string | null) => (typeof text === 'string' ? (value ?? '').includes(text) : text.test(value ?? ''));
  return has(node.textContent) && Array.from(node.children).every((child) => !has(child.textContent));
};
