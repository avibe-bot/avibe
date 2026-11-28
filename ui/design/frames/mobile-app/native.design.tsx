import { frames } from '../../frame';
import { NativeInstanceError, NativeWindowTopBar, SheetLanguage } from '../../kit/mobile-instance-sheet';
import { bilingual, personal } from './fixtures';

// Native iOS SwiftUI/UIKit chrome with no shipped web component to render.
const designFrames = frames([
  ...bilingual('instance-error', '原生 · 实例不可达', (lang) => <SheetLanguage lang={lang}><NativeInstanceError instance={personal} /></SheetLanguage>, { status: 'approved' }),
  ...bilingual('window-topbar', '原生 · 新窗口顶栏', (lang) => <SheetLanguage lang={lang}><NativeWindowTopBar /></SheetLanguage>, { status: 'approved' }),
]);

export default designFrames;
