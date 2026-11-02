// Vault requests where a conversation shows them: the request cards at the end of the transcript (provide,
// use, sign, delete), the `$<NAME>` ask inline in an agent's reply, and the floating approval bar above the
// composer once an approval card has scrolled away. Each opens the same dialogs the Vaults page does.
import { Markdown } from '@/components/ui/markdown';
import { VaultApprovalFloat, VaultChatRequests } from '@/components/ui/vault-chat-requests';

import { frames } from '../../frame';
import { accessRequest, deleteRequest, provisionRequest, signRequest, vaultFixtures } from './fixtures';

const requests = [provisionRequest, accessRequest, signRequest, deleteRequest];
const noop = () => {};
// An agent reply asks for a secret by name; an archived transcript keeps the ask, locked.
const reply = '向量库同步还差一个 Anthropic 的 key：$<ANTHROPIC_API_KEY>，填好后我接着跑。';
const archived = '（已归档的会话）同步还差 $<ANTHROPIC_API_KEY>。';

// The transcript column: ChatPage's message list is at most 760 wide.
const column = () => (
  <div className="mx-auto flex w-full max-w-[760px] flex-col gap-4 p-6">
    <Markdown secretRequests content={reply} />
    <Markdown secretRequests readOnly content={archived} />
    <VaultChatRequests requests={requests} onResolved={noop} />
    <VaultApprovalFloat offscreen={[accessRequest]} pending={[accessRequest]} onResolved={noop} />
  </div>
);

const fixtures = vaultFixtures();

const designFrames = frames([
  {
    name: 'cards',
    title: '会话里的保险库请求 · 卡片、$<NAME> 与浮动授权条',
    width: 808,
    height: 560,
    status: 'shipped',
    fixtures,
    render: column,
  },
  {
    name: 'phone',
    title: '会话里的保险库请求 · 手机',
    width: 390,
    height: 700,
    status: 'shipped',
    fixtures,
    render: column,
  },
]);

export default designFrames;
