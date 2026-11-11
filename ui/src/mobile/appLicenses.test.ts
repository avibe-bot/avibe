import { afterEach, describe, expect, it, vi } from 'vitest';

import { type AppLicenses, licenseBlocks, loadAppLicenses } from './appLicenses';

const words = (text: string) => text.split(/\s+/).filter(Boolean);
const shown = (text: string) =>
  licenseBlocks(text)
    .map((block) => block.text)
    .join('\n');

describe('licenseBlocks', () => {
  it('reflows a hard-wrapped paragraph and keeps blank-line boundaries', () => {
    const text =
      'Permission is hereby granted, free of charge, to any\nperson obtaining a copy of this software and associated\ndocumentation files.\n\nThe above copyright notice and this permission notice\nshall be included in all copies.';
    expect(licenseBlocks(text)).toEqual([
      {
        kind: 'paragraph',
        text: 'Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files.',
      },
      {
        kind: 'paragraph',
        text: 'The above copyright notice and this permission notice shall be included in all copies.',
      },
    ]);
  });

  it('turns setext underlines into headings without merging them into the text', () => {
    const text =
      'Mozilla Public License Version 2.0\n==================================\n\n1. Definitions\n--------------\n\n1.1. "Contributor"\n    means each individual or legal entity that creates, contributes to\n    the creation of, or owns Covered Software.';
    const blocks = licenseBlocks(text);
    expect(blocks.slice(0, 2)).toEqual([
      { kind: 'heading', text: 'Mozilla Public License Version 2.0' },
      { kind: 'heading', text: '1. Definitions' },
    ]);
    expect(blocks[2].kind).toBe('lines');
    expect(blocks.map((block) => block.text).join(' ')).not.toMatch(/-{3,}|={3,}/);
  });

  it('keeps copyright lines, lists, and indentation line by line', () => {
    const copyright = 'Copyright (c) 2014 Alex Crichton\nCopyright (c) 2018 The Rust Project Developers';
    expect(licenseBlocks(copyright)).toEqual([{ kind: 'lines', text: copyright }]);
    const list = '- uniffi 0.31.2 (unmodified)\n  Registry source: https://static.crates.io/crates/uniffi/uniffi-0.31.2.crate';
    expect(licenseBlocks(list)).toEqual([{ kind: 'lines', text: list }]);
    const address = 'The Regents of the University of California.\nAll rights reserved.\nBerkeley, CA';
    expect(licenseBlocks(address)[0].kind).toBe('lines');
  });

  it('keeps a lone separator line and a markdown heading', () => {
    expect(licenseBlocks('# Redistribution notices\n\n----\n\nText.')).toEqual([
      { kind: 'heading', text: 'Redistribution notices' },
      { kind: 'paragraph', text: '----' },
      { kind: 'paragraph', text: 'Text.' },
    ]);
  });

  it('never drops or changes a word', () => {
    const text =
      'Apache License\n                           Version 2.0, January 2004\n\nTERMS AND CONDITIONS\n\n1. Definitions.\n\n      "License" shall mean the terms and conditions for use, reproduction,\n      and distribution as defined by Sections 1 through 9 of this document.\r\n\r\nCopyright (c) n0\nPermission is hereby granted.';
    const kept = words(text).filter((word) => !/^(-{3,}|={3,})$/.test(word));
    expect(words(shown(text))).toEqual(kept);
  });
});

describe('loadAppLicenses', () => {
  afterEach(() => vi.unstubAllGlobals());

  const data: AppLicenses = {
    schema_version: 1,
    native: { state: 'published', release_tag: 'iroh-apple-v1.2.0-1' },
    web: { sha256: 'a'.repeat(64), packages: 1 },
    components: 1,
    sources: [],
    groups: [{ title: 'MIT', text: 'Permission is hereby granted.', components: [{ name: 'react', version: '19.3.1', origin: 'web' }] }],
  };
  const respond = (body: unknown, ok = true) =>
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok, json: async () => body })));

  it('reads the bundled data', async () => {
    respond(data);
    await expect(loadAppLicenses()).resolves.toEqual(data);
  });

  it('reads the bundled JavaScript notices while the native pin is pending', async () => {
    const pending = { ...data, native: { state: 'pending', release_tag: 'x' } };
    respond(pending);
    await expect(loadAppLicenses()).resolves.toEqual(pending);
  });

  it('has nothing to show for an empty, missing, or unknown file', async () => {
    respond({ ...data, groups: [] });
    await expect(loadAppLicenses()).resolves.toBeNull();
    respond(null, false);
    await expect(loadAppLicenses()).resolves.toBeNull();
    respond({ ...data, schema_version: 2 });
    await expect(loadAppLicenses()).resolves.toBeNull();
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('offline'))));
    await expect(loadAppLicenses()).resolves.toBeNull();
  });
});
