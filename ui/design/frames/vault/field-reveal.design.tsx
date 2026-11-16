// The owner disliked the "…" menu's native "Fields to show" select (2026-11-16 17:10). Three
// proposals for a secret's fields, drawn on the Vaults card with one, two and several fields, for both
// tiers (kit/vault-field-reveal). Protected: Show / Copy open the secure window from the click and the
// value appears only there; the card never draws a value. Standard: field names only (no Web reveal).
import { useDraft } from '../../kit/draft';
import { FieldRevealCard, type FieldRevealOption, type FieldRevealSecret } from '../../kit/vault-field-reveal';
import { frames } from '../../frame';
import { vaultFixtures } from './fixtures';

const fixtures = vaultFixtures();

const useSecrets = (): FieldRevealSecret[] => {
  const d = useDraft();
  return [
    { name: 'BANK_PIN', description: d('银行卡取现', 'Bank card PIN'), protection: 'protected', fields: ['value'], defaultField: 'value', tags: ['finance'], used: d('已使用 2 次 · 3 天前', 'Used 2 times · 3 days ago') },
    { name: 'EMAIL_LOGIN', description: d('工作邮箱', 'Work email'), protection: 'protected', fields: ['username', 'password'], defaultField: 'password', tags: ['mail'], used: d('已使用 9 次 · 1 天前', 'Used 9 times · 1 day ago') },
    { name: 'EXCHANGE_API', description: d('交易所 API', 'Exchange API'), protection: 'protected', fields: ['api_key', 'api_secret', 'passphrase', 'totp_seed'], defaultField: 'api_key', tags: ['crypto'], used: d('从未使用', 'Never used') },
    { name: 'AWS_DEPLOY', description: d('部署到 ap-east-1 的 IAM 用户', 'IAM user for ap-east-1 deploys'), protection: 'standard', fields: ['access_key_id', 'secret_access_key'], defaultField: 'access_key_id', tags: ['prod'], used: d('已使用 31 次 · 5 小时前', 'Used 31 times · 5 hours ago') },
    { name: 'OPENAI_API_KEY', description: d('OpenAI 主账号', 'OpenAI main account'), protection: 'standard', fields: ['value'], defaultField: 'value', tags: ['ai'], used: d('已使用 142 次 · 2 小时前', 'Used 142 times · 2 hours ago') },
  ];
};

function Heading({ option }: { option: FieldRevealOption }) {
  const d = useDraft();
  const text = {
    rows: d('A · 字段直接列在卡片上：每个字段一行，显示 / 复制紧跟字段名（像 1Password）', 'A · Fields listed on the card: one row each, Show / Copy right after the name (like 1Password)'),
    expand: d('B · 卡片上折叠「N 个字段」，展开后同 A；只有一个值时，显示 / 复制直接在卡片右侧', 'B · "N fields" folded on the card, expanding to A’s rows; a one-value secret has Show / Copy on the card'),
    menu: d('C · 仍在「…」菜单里，但去掉下拉框：每个字段一行，带显示 / 复制', 'C · Still in the "…" menu, without the select: one row per field with Show / Copy'),
  }[option];
  return <p className="text-[13px] font-medium text-foreground">{text}</p>;
}

const list = (option: FieldRevealOption, { expanded = false, menuOn = '' } = {}) => function List() {
  const secrets = useSecrets();
  return (
    <div className="flex min-h-full flex-col gap-3 bg-background p-6">
      <Heading option={option} />
      <ul className="flex flex-col gap-2.5">
        {secrets.map((secret) => (
          <FieldRevealCard key={secret.name} secret={secret} option={option} expanded={expanded} menuOpen={secret.name === menuOn} />
        ))}
      </ul>
    </div>
  );
};

const designFrames = frames([
  { name: 'a-rows', title: 'A · 字段列在卡片上（1 / 2 / 多个字段，保护档与标准档）', width: 1192, height: 900, status: 'proposed', fixtures, render: list('rows') },
  { name: 'a-rows-phone', title: 'A · 手机', width: 390, height: 1300, status: 'proposed', fixtures, render: list('rows') },
  { name: 'b-expand', title: 'B · 折叠「N 个字段」（收起）', width: 1192, height: 760, status: 'proposed', fixtures, render: list('expand') },
  { name: 'b-expand-open', title: 'B · 展开后', width: 1192, height: 900, status: 'proposed', fixtures, render: list('expand', { expanded: true }) },
  { name: 'b-expand-phone', title: 'B · 手机 · 展开后', width: 390, height: 1300, status: 'proposed', fixtures, render: list('expand', { expanded: true }) },
  { name: 'c-menu', title: 'C · 「…」菜单里的字段列表（多个字段）', width: 1192, height: 900, status: 'proposed', fixtures, render: list('menu', { menuOn: 'EXCHANGE_API' }) },
  { name: 'c-menu-single', title: 'C · 「…」菜单（只有一个值）', width: 1192, height: 760, status: 'proposed', fixtures, render: list('menu', { menuOn: 'BANK_PIN' }) },
  { name: 'c-menu-phone', title: 'C · 手机 · 菜单', width: 390, height: 1100, status: 'proposed', fixtures, render: list('menu', { menuOn: 'EXCHANGE_API' }) },
]);

export default designFrames;
