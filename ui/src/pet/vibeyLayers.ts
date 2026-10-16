import messageBody from '@/assets/pet/layers/message-body.png';
import messageDot1 from '@/assets/pet/layers/message-dot1.png';
import messageDot2 from '@/assets/pet/layers/message-dot2.png';
import messageDot3 from '@/assets/pet/layers/message-dot3.png';
import messageEnvelope from '@/assets/pet/layers/message-envelope.png';
import sleepBody from '@/assets/pet/layers/sleep-body.png';
import sleepZ1 from '@/assets/pet/layers/sleep-z1.png';
import sleepZ2 from '@/assets/pet/layers/sleep-z2.png';
import celebrateBody from '@/assets/pet/layers/celebrate-body.png';
import celebrateC01 from '@/assets/pet/layers/celebrate-c01.png';
import celebrateC02 from '@/assets/pet/layers/celebrate-c02.png';
import celebrateC03 from '@/assets/pet/layers/celebrate-c03.png';
import celebrateC04 from '@/assets/pet/layers/celebrate-c04.png';
import celebrateC05 from '@/assets/pet/layers/celebrate-c05.png';
import celebrateC06 from '@/assets/pet/layers/celebrate-c06.png';
import celebrateC07 from '@/assets/pet/layers/celebrate-c07.png';
import celebrateC08 from '@/assets/pet/layers/celebrate-c08.png';
import celebrateC09 from '@/assets/pet/layers/celebrate-c09.png';
import celebrateC10 from '@/assets/pet/layers/celebrate-c10.png';
import celebrateC11 from '@/assets/pet/layers/celebrate-c11.png';
import celebrateC12 from '@/assets/pet/layers/celebrate-c12.png';
import thinkingBody from '@/assets/pet/layers/thinking-body.png';
import thinkingDot1 from '@/assets/pet/layers/thinking-dot1.png';
import thinkingDot2 from '@/assets/pet/layers/thinking-dot2.png';
import thinkingBubble from '@/assets/pet/layers/thinking-bubble.png';
import thinkingQuestion from '@/assets/pet/layers/thinking-question.png';
import errorBody from '@/assets/pet/layers/error-body.png';
import errorDrop from '@/assets/pet/layers/error-drop.png';

export type LayerPart = {
  src: string;
  role: string;
  step: number;
  x: number;
  y: number;
  w: number;
  h: number;
  dx?: number;
  dy?: number;
};

export type LayerSet = {
  body: string;
  parts: LayerPart[];
};

export const GLYPH_COLOR = 'rgb(201,203,227)';

export const LAYERS: Record<'message' | 'sleep' | 'celebrate' | 'thinking' | 'error', LayerSet> = {
  message: {
    body: messageBody,
    parts: [
      { src: messageDot1, role: 'dot', step: 0, x: 141, y: 55, w: 11, h: 8 },
      { src: messageDot2, role: 'dot', step: 1, x: 147, y: 46, w: 8, h: 10 },
      { src: messageDot3, role: 'dot', step: 2, x: 153, y: 42, w: 9, h: 11 },
      { src: messageEnvelope, role: 'envelope', step: 3, x: 151, y: 52, w: 33, h: 33 },
    ],
  },
  sleep: {
    body: sleepBody,
    parts: [
      { src: sleepZ1, role: 'z', step: 0, x: 141, y: 40, w: 10, h: 12 },
      { src: sleepZ2, role: 'z', step: 1, x: 146, y: 20, w: 17, h: 20 },
    ],
  },
  celebrate: {
    body: celebrateBody,
    parts: [
      { src: celebrateC01, role: 'confetti', step: 0, x: 140, y: 28, w: 15, h: 15, dx: 0.631, dy: -0.776 },
      { src: celebrateC02, role: 'confetti', step: 1, x: 154, y: 50, w: 10, h: 9, dx: 0.815, dy: -0.579 },
      { src: celebrateC03, role: 'confetti', step: 2, x: 165, y: 65, w: 10, h: 11, dx: 0.931, dy: -0.366 },
      { src: celebrateC04, role: 'confetti', step: 3, x: 167, y: 88, w: 8, h: 11, dx: 0.996, dy: -0.085 },
      { src: celebrateC05, role: 'confetti', step: 4, x: 165, y: 112, w: 14, h: 14, dx: 0.971, dy: 0.24 },
      { src: celebrateC06, role: 'confetti', step: 5, x: 156, y: 137, w: 11, h: 10, dx: 0.845, dy: 0.534 },
      { src: celebrateC07, role: 'confetti', step: 6, x: 19, y: 124, w: 10, h: 11, dx: -0.923, dy: 0.384 },
      { src: celebrateC08, role: 'confetti', step: 7, x: 16, y: 98, w: 10, h: 10, dx: -0.999, dy: 0.041 },
      { src: celebrateC09, role: 'confetti', step: 8, x: 18, y: 70, w: 13, h: 13, dx: -0.949, dy: -0.316 },
      { src: celebrateC10, role: 'confetti', step: 9, x: 21, y: 50, w: 11, h: 10, dx: -0.836, dy: -0.549 },
      { src: celebrateC11, role: 'confetti', step: 10, x: 35, y: 33, w: 15, h: 15, dx: -0.662, dy: -0.75 },
      { src: celebrateC12, role: 'confetti', step: 11, x: 72, y: 26, w: 9, h: 10, dx: -0.259, dy: -0.966 },
    ],
  },
  thinking: {
    body: thinkingBody,
    parts: [
      { src: thinkingDot1, role: 'tdot', step: 0, x: 142, y: 59, w: 7, h: 9 },
      { src: thinkingDot2, role: 'tdot', step: 1, x: 145, y: 53, w: 10, h: 11 },
      { src: thinkingBubble, role: 'bubble', step: 2, x: 139, y: 20, w: 40, h: 36 },
      { src: thinkingQuestion, role: 'glyph', step: 3, x: 151, y: 28, w: 16, h: 24 },
    ],
  },
  error: {
    body: errorBody,
    parts: [
      { src: errorDrop, role: 'drop', step: 0, x: 132, y: 61, w: 15, h: 21 },
    ],
  },
};
