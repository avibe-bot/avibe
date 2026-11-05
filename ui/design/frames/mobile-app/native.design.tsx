import { frames } from '../../frame';
import { NativeInstanceError, NativeWindowTopBar } from '../../kit/mobile-app';
import { bilingual, personal } from './fixtures';

const designFrames = frames([
  ...bilingual('instance-error', '原生 · 实例不可达', () => <NativeInstanceError instance={personal} />),
  ...bilingual('window-topbar', '原生 · 新窗口顶栏', () => <NativeWindowTopBar />),
]);

export default designFrames;
