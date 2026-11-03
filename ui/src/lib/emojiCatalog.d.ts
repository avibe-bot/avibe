// The full emoji set, built from Emojibase when the UI is built
// (`scripts/emojiCatalog.ts`). Import it lazily: it is about 90 kB gzipped.
declare module 'virtual:emoji-catalog' {
  /** One emoji, in Emojibase order. */
  export type EmojiCatalogEntry = [
    emoji: string,
    /** Emojibase's group, from 0 (smileys and emotion) to 9 (flags). */
    group: number,
    /** The Emoji version that added it. */
    version: number,
    /** 1 for a country or subdivision flag, which some systems draw as letters. */
    flag: 0 | 1,
    /** CLDR name, then keywords. */
    en: string[],
    zh: string[],
    /** Its five single skin tones, light to dark, when it has them. */
    skins?: string[],
  ];
  const catalog: EmojiCatalogEntry[];
  export default catalog;
}
