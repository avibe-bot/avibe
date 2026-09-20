// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import { RoutingConfigPanel } from './RoutingConfigPanel';
import { WeChatConfig } from '../steps/WeChatConfig';

const api = vi.hoisted(() => ({ wechatStartLogin: vi.fn(), wechatPollLogin: vi.fn() }));
vi.mock('@/context/ApiContext', async (loadOriginal) => ({
  ...await loadOriginal<typeof import('@/context/ApiContext')>(), useApi: () => api,
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../workbench/AgentRoutePicker', () => ({ AgentRoutePicker: () => null }));

function manager(role: 'owner' | 'member', children: React.ReactNode) {
  return <InstanceAuthorizationContext.Provider value={{
    remote: true, instanceKind: 'organization', instanceRole: role,
    capabilities: { ...OWNER_INSTANCE_CAPABILITIES, is_instance_owner: role === 'owner', can_manage_access_members: role === 'owner' },
  }}>{children}</InstanceAuthorizationContext.Provider>;
}

afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); api.wechatStartLogin.mockResolvedValue({ qrcode_url: 'https://example.invalid' }); });

describe('Instance Member settings boundaries', () => {
  it.each(['owner', 'member'] as const)('%s can edit mentions; only Owner can change binding', async (role) => {
    const onChange = vi.fn();
    render(manager(role, <RoutingConfigPanel value={{ custom_cwd: '', routing: {}, show_message_types: [], require_mention: false, require_bind: true }} onChange={onChange} onBrowseDirectory={() => {}} globalConfig={{}} />));
    const [mention, binding] = screen.getAllByRole('switch') as HTMLButtonElement[];
    expect(mention.disabled).toBe(false);
    expect(binding.disabled).toBe(role === 'member');
    await userEvent.click(mention);
    expect(onChange).toHaveBeenCalledWith({ require_mention: true });
    await userEvent.click(binding);
    expect(onChange).toHaveBeenCalledTimes(role === 'member' ? 1 : 2);
  });

  it('Member saves a WeChat credential without starting the user-binding QR flow', async () => {
    const apply = vi.fn();
    render(manager('member', <WeChatConfig data={{ wechat: {} }} embedded onNext={() => {}} onApply={apply} />));
    expect(api.wechatStartLogin).not.toHaveBeenCalled();
    expect(screen.getByText('wechatConfig.ownerBindingRequired')).toBeTruthy();
    await userEvent.type(screen.getByLabelText('wechatConfig.botToken'), 'test-credential');
    await userEvent.click(screen.getByRole('button', { name: 'platform.apply' }));
    expect(apply).toHaveBeenCalledWith(expect.objectContaining({ wechat: expect.objectContaining({ bot_token: 'test-credential' }) }));
    expect(api.wechatStartLogin).not.toHaveBeenCalled();
    expect(api.wechatPollLogin).not.toHaveBeenCalled();
  });

  it('Owner retains automatic WeChat QR setup', async () => {
    render(manager('owner', <WeChatConfig data={{ wechat: {} }} onNext={() => {}} />));
    expect(api.wechatStartLogin).toHaveBeenCalledTimes(1);
  });
});
