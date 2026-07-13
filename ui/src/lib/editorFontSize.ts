import { createFontSizeStore } from './fontSizeStore';

const STORAGE_KEY = 'avibe.editor.fontSize.v1';

export const EDITOR_FONT_MIN = 9;
export const EDITOR_FONT_MAX = 24;
export const EDITOR_FONT_DEFAULT = 13;

const editorFontSize = createFontSizeStore(STORAGE_KEY, {
  min: EDITOR_FONT_MIN,
  max: EDITOR_FONT_MAX,
  default: EDITOR_FONT_DEFAULT,
});

export const getEditorFontSize = editorFontSize.get;
export const adjustEditorFontSize = editorFontSize.adjust;
export const resetEditorFontSize = editorFontSize.reset;
export const subscribeEditorFontSize = editorFontSize.subscribe;
