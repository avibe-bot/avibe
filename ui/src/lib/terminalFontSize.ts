import { createFontSizeStore } from './fontSizeStore';

const STORAGE_KEY = 'avibe.terminal.fontSize.v1';

export const TERMINAL_FONT_MIN = 9;
export const TERMINAL_FONT_MAX = 24;
export const TERMINAL_FONT_DEFAULT = 13;

const terminalFontSize = createFontSizeStore(STORAGE_KEY, {
  min: TERMINAL_FONT_MIN,
  max: TERMINAL_FONT_MAX,
  default: TERMINAL_FONT_DEFAULT,
});

export const getTerminalFontSize = terminalFontSize.get;
export const adjustTerminalFontSize = terminalFontSize.adjust;
export const resetTerminalFontSize = terminalFontSize.reset;
export const subscribeTerminalFontSize = terminalFontSize.subscribe;
export const _resetTerminalFontSize = terminalFontSize._reset;
