// Search on a phone: the full-screen app and message search the Inbox search field opens.
import { SearchPage } from '@/components/workbench/SearchPage';

import { frames } from '../../frame';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { ratelimitResults, searchApi } from './fixtures';

const page = phoneShell('/search', <SearchPage />);

const designFrames = frames([
  {
    name: 'results',
    title: '搜索 · 手机 · 结果按会话分组',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/search?q=ratelimit',
    // The search waits out its debounce before it reads.
    wait: 400,
    fixtures: workbenchFixtures(searchApi('ratelimit', ratelimitResults)),
    render: page,
  },
  {
    name: 'no-results',
    title: '搜索 · 手机 · 没有匹配',
    width: 390,
    height: 600,
    status: 'shipped',
    route: '/search?q=kubernetes',
    wait: 400,
    fixtures: workbenchFixtures(searchApi('kubernetes', { total: 0, session_count: 0, sessions: [] })),
    render: page,
  },
  {
    name: 'start',
    title: '搜索 · 手机 · 刚打开',
    width: 390,
    height: 600,
    status: 'shipped',
    route: '/search',
    fixtures: workbenchFixtures(),
    render: page,
  },
]);

export default designFrames;
