// A frame helper for the shipped voice mark (src/components/voice/VoiceMark): a frame whose
// fixture message carries no voice metadata places the mark over the shipped bubble with this.
import clsx from 'clsx';

import { VoiceMark } from '@/components/voice/VoiceMark';

/** Where a frame puts the mark on a shipped message bubble it cannot reach into: an absolute
 *  layer at the bubble's corner. */
export function VoiceMarkLayer({ className }: { className: string }) {
  return (
    <div className={clsx('pointer-events-none fixed z-50', className)}>
      <VoiceMark />
    </div>
  );
}
