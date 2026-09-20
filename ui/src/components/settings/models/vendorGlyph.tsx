// Drawing for the API-key 服务商 picker: one mark per choice, all at one size.
//
// The artwork and the optical rule it is drawn by live in `vendorMarks.ts`. This
// file is only the three ways a choice becomes ink — a vendor's own mark, a
// letter for the vendors that publish none, and the field's own subject for
// 自定义 — each of which reaches that same box, so the shared CSS slot renders
// one ink height for the whole list rather than one per drawing style.
//
// Five shipped vendors publish no single-color mark Simple Icons carries. They
// draw their initial in the same box instead: a letter is honest about standing
// in for artwork, where a neighbour's logo would be a lie and an invented one
// worse. That fallback follows the label rather than a list, so a vendor added
// to the catalog is distinguishable before anyone draws it. The one thing the
// picker cannot survive is two rows drawing the same glyph, which is the
// property `vendorGlyph.test.tsx` holds.
import * as React from 'react';
import { Globe } from 'lucide-react';

import { cn } from '@/lib/utils';
import { apiKeyVendorPreset, CUSTOM_VENDOR } from './apiKeyVendors';
import {
  BOX_HEIGHT,
  GLOBE_INK,
  INK_HEIGHT,
  markViewBox,
  VENDOR_MARKS,
  type VendorMark,
} from './vendorMarks';

const glyphClassName = (className?: string) => cn('model-hub-add-key-vendor-glyph', className);

/** The one place a mark becomes pixels. `protocolGlyph.tsx` draws through this
 *  too, so the two marks it shares cannot end up at a different size. */
export const Mark: React.FC<{ mark: VendorMark } & React.SVGProps<SVGSVGElement>> = ({
  mark,
  ...props
}) => (
  <svg viewBox={markViewBox(mark.ink)} aria-hidden="true" focusable="false" fill="currentColor" {...props}>
    <path d={mark.path} />
  </svg>
);

/** A letter has no ink to measure ahead of time, so it reaches the same box from
 *  the other side: a cap height is a known fraction of a font size, ~0.70–0.73
 *  across this app's sans stack, so 26 puts the cap at 18.2–19.2 units and the
 *  baseline at `12 + INK_HEIGHT / 2` centres it. Weight 700 answers the other
 *  half of the complaint this rule exists for — a regular-weight letter is not
 *  only shorter than a solid mark, it is thinner. */
const Monogram: React.FC<{ letter: string; className?: string }> = ({ letter, className }) => (
  <svg
    viewBox={markViewBox([0, 12 - INK_HEIGHT / 2, BOX_HEIGHT, INK_HEIGHT])}
    aria-hidden="true"
    focusable="false"
    className={glyphClassName(className)}
  >
    <text
      x="12"
      y={12 + INK_HEIGHT / 2}
      textAnchor="middle"
      fontSize="26"
      fontWeight="700"
      fill="currentColor"
    >
      {letter}
    </text>
  </svg>
);

/**
 * The mark for one vendor id. 自定义 is not a vendor and gets no logo: it takes
 * the field's own subject, an address you supply.
 */
export const VendorGlyph: React.FC<{ vendor: string; className?: string }> = ({ vendor, className }) => {
  if (vendor === CUSTOM_VENDOR) {
    return (
      <Globe
        viewBox={markViewBox(GLOBE_INK)}
        aria-hidden="true"
        focusable="false"
        className={glyphClassName(className)}
      />
    );
  }
  const mark = VENDOR_MARKS[vendor];
  if (mark) return <Mark mark={mark} className={glyphClassName(className)} />;
  const label = apiKeyVendorPreset(vendor)?.label ?? vendor;
  return <Monogram letter={Array.from(label.trim())[0].toUpperCase()} className={className} />;
};
