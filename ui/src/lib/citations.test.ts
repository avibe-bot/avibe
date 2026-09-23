// The citation binding, tested as the pure decision it is.
//
// `citation-bridge.test.tsx` renders a real producer run through the real
// component, which is what proves the contract end to end. This file covers the
// answers that file cannot reach from a recording: a digest agreeing with
// Python's over text no producer case contains, a span the renderer must refuse,
// and an edit that reaches into a citation — which the real passes, on the real
// producer's output, never do (the producer percent-encodes `<`/`>`, so only
// prose outside the link ever holds a live marker). The rule still has to hold,
// because the next transform added to that surface may not be so careful.

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  bindCitations,
  bodyDigest,
  remapCitations,
  type CitationSource,
  type TextEdit,
} from './citations';

// Written by the Python side, which computes the same digest with hashlib over
// the same UTF-16LE code units. Read at runtime: it holds unpaired surrogates,
// which is the whole reason it exists, and they survive as JSON escapes.
const DIGEST_VECTORS = JSON.parse(
  readFileSync(resolve(process.cwd(), '../tests/fixtures/citation_body_digest.json'), 'utf8'),
) as Array<{ name: string; text: string; sha256: string }>;

const BODY = 'Cited. [example.com](https://example.com/x) and more.';
const SPAN: [number, number] = [7, 43];

const row = (over: Partial<CitationSource> = {}): CitationSource => ({
  index: 1,
  ref_id: 'turn0view0',
  title: 'Example',
  url: 'https://example.com/x',
  label: 'example.com',
  spans: [[...SPAN]],
  body_sha256: bodyDigest(BODY),
  ...over,
});

describe('bodyDigest', () => {
  it.each(DIGEST_VECTORS.map((v) => [v.name, v] as const))(
    'agrees with the backend on %s',
    (_name, vector) => {
      expect(bodyDigest(vector.text)).toBe(vector.sha256);
    },
  );

  it('measures the span the sidecar names', () => {
    // The unit the two ends count in: a JavaScript string index is a UTF-16 code
    // unit, which is what `core.citations` encodes before it measures.
    expect(BODY.slice(...SPAN)).toBe('[example.com](https://example.com/x)');
  });

  it('separates two bodies that differ only past the astral plane', () => {
    expect(bodyDigest('🙂')).not.toBe(bodyDigest('🙃'));
  });
});

describe('bindCitations', () => {
  it('binds a row measured in this exact body', () => {
    const binding = bindCitations([row()], BODY);

    expect(binding?.bound).toHaveLength(1);
    expect(binding?.bound[0].spans).toEqual([SPAN]);
    expect(binding?.legacy).toEqual([]);
  });

  it('has nothing to say without a sidecar', () => {
    expect(bindCitations(undefined, BODY)).toBeNull();
    expect(bindCitations([], BODY)).toBeNull();
  });

  it('refuses the whole sidecar when the body is not the one it describes', () => {
    // The paragraph above the citation was deleted, and the surviving link now
    // sits exactly where the citation used to. Only the digest can tell.
    expect(bindCitations([row()], `${BODY} Edited.`)).toBeNull();
    expect(bindCitations([row({ body_sha256: undefined })], BODY)).toBeNull();
  });

  it('refuses a span this body cannot hold', () => {
    for (const spans of [
      [[7]],
      [[7, 43, 44]],
      [[-1, 43]],
      [[43, 7]],
      [[7, 7]],
      [[7, BODY.length + 1]],
      [[7.5, 43]],
      [],
      'nope',
    ]) {
      expect(bindCitations([row({ spans: spans as number[][] })], BODY)).toBeNull();
    }
  });

  it('refuses two rows that claim the same characters', () => {
    // One link is one citation. Overlapping claims mean the measurement is not
    // describing this text, whatever else it describes.
    const overlap = [row(), row({ index: 2, spans: [[20, 43]] })];

    expect(bindCitations(overlap, BODY)).toBeNull();
  });

  it('accepts two rows that sit side by side', () => {
    const body = `${BODY}\n\n[example.org](https://example.org/y)`;
    const first = row({ body_sha256: bodyDigest(body) });
    const second = row({
      index: 2,
      url: 'https://example.org/y',
      label: 'example.org',
      spans: [[54, 90]],
      body_sha256: bodyDigest(body),
    });

    expect(bindCitations([first, second], body)?.bound).toHaveLength(2);
  });

  it('drops a row that could never be an anchor, and keeps the rest', () => {
    const body = `${BODY}\n\n[example.org](https://example.org/y)`;
    const digest = bodyDigest(body);
    const unusable = row({ index: 2, url: 'javascript:alert(1)', body_sha256: digest });

    const binding = bindCitations([row({ body_sha256: digest }), unusable], body);

    // It names no link this renderer will draw, so its absence changes nothing
    // about the body — unlike a row that disagrees with the body it names.
    expect(binding?.bound.map((entry) => entry.source.index)).toEqual([1]);
  });

  it('reads a row persisted before the contract existed', () => {
    const legacy = row({ spans: undefined, body_sha256: undefined });

    expect(bindCitations([legacy], BODY)).toEqual({ bound: [], legacy: [legacy] });
  });

  it('refuses a sidecar that mixes the two', () => {
    // Half of it describes this body and half of it describes nothing. Picking
    // the half to believe is the guess this contract exists to avoid.
    const mixed = [row(), row({ index: 2, spans: undefined, body_sha256: undefined })];

    expect(bindCitations(mixed, BODY)).toBeNull();
  });
});

describe('remapCitations', () => {
  const bound = () => bindCitations([row()], BODY);

  const remapped = (edits: TextEdit[]) => remapCitations(bound(), edits)?.bound[0]?.spans;

  it('carries a span across an edit that lands before it', () => {
    // A chip minted from a 6-unit marker in the prose above becomes a 40-unit
    // link, and the citation moves the 34 units that pass added.
    expect(remapped([{ start: 0, end: 6, inserted: 40 }])).toEqual([[41, 77]]);
  });

  it('carries a span across an edit that shortens the text', () => {
    expect(remapped([{ start: 0, end: 7, inserted: 2 }])).toEqual([[2, 38]]);
  });

  it('leaves a span alone when the edit is after it', () => {
    expect(remapped([{ start: 44, end: 52, inserted: 0 }])).toEqual([SPAN]);
  });

  it('composes several edits in one pass', () => {
    expect(remapped([
      { start: 0, end: 2, inserted: 5 },
      { start: 44, end: 45, inserted: 9 },
    ])).toEqual([[10, 46]]);
  });

  it('treats an insertion at the span edges as outside it', () => {
    // Written where the link begins, so the link begins just after it...
    expect(remapped([{ start: SPAN[0], end: SPAN[0], inserted: 4 }])).toEqual([[11, 47]]);
    // ...and written where it ends, so the link is unmoved.
    expect(remapped([{ start: SPAN[1], end: SPAN[1], inserted: 4 }])).toEqual([SPAN]);
  });

  it('invalidates a citation an edit reached into', () => {
    // A card minted inside the destination would take the address apart, and
    // there is no honest answer to which half kept the attribution.
    for (const edit of [
      { start: 10, end: 14, inserted: 30 },
      { start: 6, end: 8, inserted: 2 },
      { start: 42, end: 44, inserted: 2 },
      { start: 0, end: BODY.length, inserted: 3 },
      { start: 20, end: 20, inserted: 7 },
    ]) {
      expect(remapCitations(bound(), [edit])).toBeNull();
    }
  });

  it('drops one citation whole when any of its spans is invalidated', () => {
    const body = `${BODY}\n\n[example.com](https://example.com/x)`;
    const twice = row({ spans: [[7, 43], [54, 90]], body_sha256: bodyDigest(body) });

    expect(remapCitations(bindCitations([twice], body), [{ start: 60, end: 62, inserted: 0 }]))
      .toBeNull();
  });

  it('keeps a legacy row, which is matched on text rather than position', () => {
    const legacy = row({ spans: undefined, body_sha256: undefined });

    expect(remapCitations(bindCitations([legacy], BODY), [{ start: 0, end: 9, inserted: 41 }]))
      .toEqual({ bound: [], legacy: [legacy] });
  });

  it('passes an empty edit list straight through', () => {
    const binding = bound();

    expect(remapCitations(binding, [])).toBe(binding);
    expect(remapCitations(null, [{ start: 0, end: 1, inserted: 0 }])).toBeNull();
  });
});
