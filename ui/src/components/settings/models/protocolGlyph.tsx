import * as React from 'react';

import { cn } from '@/lib/utils';
import type { SourceProtocol } from './types';
import { ANTHROPIC_MARK, OPENAI_MARK } from './vendorGlyph';

type GlyphProps = React.SVGProps<SVGSVGElement>;

const glyphClassName = (className?: string) => cn('model-hub-add-key-protocol-glyph', className);

// A protocol family is named after the vendor that published it, so its glyph is
// that vendor's mark. The artwork lives with the other vendor marks; this file
// owns only the mapping from an interface to the one that stands for it.
export const AnthropicGlyph: React.FC<GlyphProps> = ({ className, ...props }) => (
  <svg
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
    fill="currentColor"
    className={glyphClassName(className)}
    {...props}
  >
    <path d={ANTHROPIC_MARK} />
  </svg>
);

export const OpenAIGlyph: React.FC<GlyphProps> = ({ className, ...props }) => (
  <svg
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
    fill="currentColor"
    className={glyphClassName(className)}
    {...props}
  >
    <path d={OPENAI_MARK} />
  </svg>
);

export const ProtocolGlyph: React.FC<{ protocol: SourceProtocol; className?: string }> = ({
  protocol,
  className,
}) => (
  protocol === 'anthropic'
    ? <AnthropicGlyph className={className} />
    : <OpenAIGlyph className={className} />
);
