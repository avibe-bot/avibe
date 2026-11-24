// The page an open tab shows when the address it was loaded from no longer opens Avibe.
import { AccessBlocked } from '@/components/AccessBlocked';

import { frames } from '../../frame';

const designFrames = frames([
  {
    name: 'host-mismatch',
    title: '这个地址打不开 · 手机上还开着的旧页面',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { bots: [] },
    render: () => <AccessBlocked code="remote_access_host_mismatch" />,
  },
]);

export default designFrames;
