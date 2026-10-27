import { describe, expect, it } from 'vitest';

import { unifyCjkRadicals } from './cjkRadicals';

describe('unifyCjkRadicals', () => {
  it('maps the Kangxi radicals Chrome-made PDFs extract to the characters they show', () => {
    // As extracted from a Chrome-printed PDF set in PingFang: ⽉ ⾃ ⽣ ⾏ ⾄ are Kangxi radicals.
    expect(unifyCjkRadicals('10 ⽉ · ⾃动⽣成 · 运⾏稳定，降⾄ 2.1%')).toBe('10 月 · 自动生成 · 运行稳定，降至 2.1%');
  });

  it('maps CJK Radicals Supplement forms, including simplified ones NFKC leaves alone', () => {
    expect(unifyCjkRadicals('共 4 ⻚')).toBe('共 4 页');
    expect(unifyCjkRadicals('⻥⻢⻛')).toBe('鱼马风');
    expect('⻚'.normalize('NFKC')).toBe('⻚');
  });

  it('keeps an equivalent outside the BMP whole', () => {
    expect(unifyCjkRadicals('⺇')).toBe('\u{20628}');
  });

  it('leaves full-width punctuation, Latin text and ordinary ideographs as they are', () => {
    const text = '第 1.1 节说明：本季度 Agent 运行稳定，The weekly digest（摘要）！';
    expect(unifyCjkRadicals(text)).toBe(text);
  });
});
