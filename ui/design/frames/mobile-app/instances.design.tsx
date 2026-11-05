import { frames } from '../../frame';
import { InstancePicker } from '../../kit/mobile-app';
import { bilingual, longInstances, mixedInstances, ownInstances, personal } from './fixtures';

const designFrames = frames([
  ...bilingual('own', '首次选择 · 自己的实例', () => <InstancePicker state="own" instances={ownInstances} />),
  ...bilingual('mixed', '首次选择 · 自己的与共享实例', () => <InstancePicker state="mixed" instances={mixedInstances} />),
  ...bilingual('current', '切换实例 · 当前标记', () => <InstancePicker state="current" instances={mixedInstances} currentId={personal.id} />),
  ...bilingual('offline', '列表 · 离线实例', () => <InstancePicker state="offline" instances={[personal, mixedInstances[2]]} />),
  ...bilingual('offline-tapped', '点离线实例 · 保留列表并说明恢复方法', () => <InstancePicker state="offline-tapped" selectedOffline={mixedInstances[2]} instances={[personal, mixedInstances[2]]} />),
  ...bilingual('loading', '列表 · 首次加载', () => <InstancePicker state="loading" instances={[]} />),
  ...bilingual('error', '列表 · 加载失败', () => <InstancePicker state="error" instances={[]} />),
  ...bilingual('pulling', '列表 · 下拉至松手刷新', () => <InstancePicker state="pulling" instances={mixedInstances} />),
  ...bilingual('refreshing', '列表 · 刷新中保留数据', () => <InstancePicker state="refreshing" instances={mixedInstances} />),
  ...bilingual('long-small', '列表 · 长名称 · 375×667', () => <InstancePicker state="current" instances={longInstances} currentId={personal.id} />, [375, 667]),
]);

export default designFrames;
