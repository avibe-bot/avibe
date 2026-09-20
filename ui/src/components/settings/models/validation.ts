export const unicodeCodePointLength = (value: string): number => Array.from(value).length;

export const optionalTrimmedTextWithin = (value: string, maxLength: number): boolean => {
  if (value.length === 0) return true;
  const trimmed = value.trim();
  return trimmed.length > 0 && unicodeCodePointLength(trimmed) <= maxLength;
};
