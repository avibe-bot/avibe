import { describe, expect, it } from 'vitest';

import { annotationStandIn, type AnnotationView } from './annotationView';

const view = (over: Partial<AnnotationView> = {}): AnnotationView => ({
  direction: 'user',
  resolved: false,
  ...over,
});

describe('annotationStandIn', () => {
  it('stays out of the way when the annotator wrote something', () => {
    expect(annotationStandIn(view({ quote: 'Model Hub' }), 'The heading is too small', true)).toBeNull();
  });

  it('treats blank text as no text', () => {
    expect(annotationStandIn(view({ quote: 'Model Hub' }), '   ', false)).toEqual({
      kind: 'quote',
      quote: 'Model Hub',
    });
    expect(annotationStandIn(view(), '', true)).toEqual({ kind: 'screenshot' });
    expect(annotationStandIn(view(), null, true)).toEqual({ kind: 'screenshot' });
  });

  // The card stacks quote above attachments; one line can hold one of them, so
  // it takes the upper. A highlight the reader can find on the page beats
  // "there is a picture".
  it('prefers the quote to the screenshot, as the card stacks them', () => {
    expect(annotationStandIn(view({ quote: 'Model Hub' }), '', true)).toEqual({
      kind: 'quote',
      quote: 'Model Hub',
    });
  });

  it('leaves the title to stand alone when there is nothing to say', () => {
    expect(annotationStandIn(view(), '', false)).toBeNull();
  });

  it('reads the same for a reverse mark', () => {
    expect(annotationStandIn(view({ direction: 'agent' }), '', true)).toEqual({ kind: 'screenshot' });
  });
});
