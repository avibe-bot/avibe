// @vitest-environment jsdom
// The domain name's rules and the one sentence the field says for each (product, 2026-11-25).
import { cleanup, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';

import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import { DomainField } from './DomainField';
import { domainNameProblem, validDomainName, type DomainNameProblem } from './domainName';

afterEach(cleanup);

describe('domainNameProblem', () => {
  it.each<[string, DomainNameProblem | null]>([
    ['max', null],
    ['ab', null],
    ['a'.repeat(46), null],
    ['max-well', null],
    ['m4x', null],
    // One reason each.
    ['max well', 'characters'],
    ['max_1', 'characters'],
    ['max.app', 'characters'],
    ['小明', 'characters'],
    ['m', 'short'],
    ['a'.repeat(47), 'long'],
    ['-max', 'hyphenEnds'],
    ['max-', 'hyphenEnds'],
    ['ab--cd', 'doubleHyphen'],
    ['xn--max', 'doubleHyphen'],
  ])('%s → %s', (name, problem) => {
    expect(domainNameProblem(name)).toBe(problem);
    expect(validDomainName(name)).toBe(problem === null);
  });

  it('says one reason at a time: characters, then length, then where the hyphens are', () => {
    // Characters come first, whatever else is wrong.
    expect(domainNameProblem('-_')).toBe('characters');
    expect(domainNameProblem(`-${'a'.repeat(46)}_`)).toBe('characters');
    // Then length: a lone hyphen is too short before it is a hyphen at the end.
    expect(domainNameProblem('-')).toBe('short');
    expect(domainNameProblem(`-${'a'.repeat(46)}`)).toBe('long');
    // Then the ends before the third and fourth places.
    expect(domainNameProblem('ab--')).toBe('hyphenEnds');
    expect(domainNameProblem('-b--cd')).toBe('hyphenEnds');
  });

  it('an empty name breaks no rule, and is not a name avibe.bot can take', () => {
    expect(domainNameProblem('')).toBeNull();
    expect(validDomainName('')).toBe(false);
  });
});

describe('DomainField', () => {
  const field = (lng: 'en' | 'zh', name: string) => {
    const i18n = createInstance();
    void i18n.init({ lng, resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
    return render(<I18nextProvider i18n={i18n}><DomainField name={name} check="invalid" label="name" onChange={() => undefined} /></I18nextProvider>);
  };

  it.each<[string, keyof typeof en.domainField]>([
    ['max well', 'invalid'],
    ['m', 'short'],
    ['a'.repeat(47), 'long'],
    ['max-', 'hyphenEnds'],
    ['ab--cd', 'doubleHyphen'],
  ])('says why %s cannot be had, in both languages', (name, key) => {
    field('en', name);
    expect(screen.getByRole('status').textContent).toBe(en.domainField[key]);
    cleanup();
    field('zh', name);
    expect(screen.getByRole('status').textContent).toBe(zh.domainField[key]);
  });

  it('says nothing while the field is empty', () => {
    field('en', '');
    expect(screen.getByRole('status').textContent).toBe('');
  });
});
