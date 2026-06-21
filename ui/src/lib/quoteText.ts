// Wrap a chat selection in quotes for the composer / a forked draft. Text that
// contains any Han ideograph (pure Chinese OR mixed Chinese+Latin, including
// supplementary-plane Ext-B+ characters) uses the Chinese corner brackets 「」;
// pure Latin/ASCII text uses straight double quotes. Mixed text counts as
// Chinese — any Han character flips to 「」.
const HAN = /\p{Script=Han}/u;

export function quoteText(text: string): string {
  const trimmed = text.trim();
  return HAN.test(trimmed) ? `「${trimmed}」` : `"${trimmed}"`;
}
