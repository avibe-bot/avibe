import * as React from 'react';

import { cn } from '@/lib/utils';
import type { SourceProtocol } from './types';
import { Mark } from './vendorGlyph';
import { ANTHROPIC_MARK, OPENAI_MARK } from './vendorMarks';

type GlyphProps = React.SVGProps<SVGSVGElement>;

const glyphClassName = (className?: string) => cn('model-hub-add-key-protocol-glyph', className);

// A protocol family is named after the vendor that published it, so its glyph is
// that vendor's mark. The artwork lives with the other vendor marks and is drawn
// by their `Mark`, which is what keeps one piece of artwork at one optical size
// in a dialog that shows it as both an interface and a vendor. This file owns
// only the mapping from an interface to the mark that stands for it.
export const AnthropicGlyph: React.FC<GlyphProps> = ({ className, ...props }) => (
  <Mark mark={ANTHROPIC_MARK} className={glyphClassName(className)} {...props} />
);

export const OpenAIGlyph: React.FC<GlyphProps> = ({ className, ...props }) => (
  <Mark mark={OPENAI_MARK} className={glyphClassName(className)} {...props} />
);

export const ProtocolGlyph: React.FC<{ protocol: SourceProtocol; className?: string }> = ({
  protocol,
  className,
}) => (
  protocol === 'anthropic'
    ? <AnthropicGlyph className={className} />
    : <OpenAIGlyph className={className} />
);
