// Form fields. Input, Textarea, Select, and the Combobox trigger share one surface (`field.ts`:
// background fill, input border, 6px radius, mint focus ring); Label, TagInput, InlineNameInput, and
// the Command list that a Combobox opens are drawn beside them.
import { Search } from 'lucide-react';

import { Combobox } from '@/components/ui/combobox';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command';
import { InlineNameInput } from '@/components/ui/inline-name-input';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { TagInput } from '@/components/ui/tag-input';
import { Textarea } from '@/components/ui/textarea';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

const noop = () => {};

const field = 'w-64';

const projects = [
  { value: 'avibe', label: 'avibe' },
  { value: 'avibe-docs', label: 'avibe-docs' },
  { value: 'avault', label: 'avault' },
  { value: 'incus-ops', label: 'incus-ops' },
];

const fieldsSheet = () => (
  <Sheet>
    <Section title="Input" note="h-9 · 文字 14px">
      <Specimen name="placeholder" className={field}>
        <Input placeholder="例如 my-project" />
      </Specimen>
      <Specimen name="已填写" className={field}>
        <Input defaultValue="/Users/demo/dev/avibe" />
      </Specimen>
      <Specimen name="disabled" className={field}>
        <Input defaultValue="127.0.0.1:19782" disabled />
      </Specimen>
      <Specimen name='variant="bare"' use="放在自带边框的容器里" className={field}>
        <div className="flex h-9 items-center gap-2 rounded-full border border-border bg-surface-2 px-3">
          <Search className="size-4 text-muted" />
          <Input variant="bare" placeholder="搜索会话" className="flex-1 text-[13px]" />
        </div>
      </Specimen>
    </Section>
    <Section title="Label + 字段">
      <Specimen name="Label" use="text-sm 中等字重" className={field}>
        <div className="flex flex-col gap-2">
          <Label htmlFor="system-field-name">项目名称</Label>
          <Input id="system-field-name" defaultValue="avibe" />
        </div>
      </Specimen>
      <Specimen name="Textarea" use="min-h-[72px]" className="w-80">
        <Textarea placeholder="告诉这个伙伴它负责什么" />
      </Specimen>
      <Specimen name="Textarea · 已填写" className="w-80">
        <Textarea defaultValue="每周一上午汇总依赖与权限的安全审计结果，高危项直接在收件箱提醒我。" />
      </Specimen>
    </Section>
    <Section title="选择">
      <Specimen name="Select" use="原生下拉，自带箭头" className={field}>
        <Select defaultValue="zh">
          <option value="zh">简体中文</option>
          <option value="en">English</option>
        </Select>
      </Specimen>
      <Specimen name="Select · disabled" className={field}>
        <Select defaultValue="info" disabled>
          <option value="info">INFO</option>
        </Select>
      </Specimen>
      <Specimen name="Combobox · 未选" use="可搜索的选择" className={field}>
        <Combobox options={projects} value="" onValueChange={noop} placeholder="选择项目" />
      </Specimen>
      <Specimen name="Combobox · withFolderIcon" use="项目选择" className={field}>
        <Combobox options={projects} value="avibe" onValueChange={noop} withFolderIcon />
      </Specimen>
    </Section>
    <Section title="多值与行内">
      <Specimen name="TagInput" use="回车或逗号添加" className="w-80">
        <TagInput values={['api.openai.com', 'api.anthropic.com']} onChange={noop} placeholder="添加域名" />
      </Specimen>
      <Specimen name="InlineNameInput" use="文件树里的新建、重命名" className={field}>
        <div className="flex items-center gap-2 rounded-md bg-surface-2 px-2 py-1">
          <InlineNameInput initial="notes.md" onCommit={noop} onCancel={noop} />
        </div>
      </Specimen>
    </Section>
  </Sheet>
);

// The trigger a person clicks, at the top left so its list opens into the frame.
const comboboxOpen = (withFolderIcon: boolean) => () => (
  <div className="w-72 p-6">
    <Combobox options={projects} value="avibe" onValueChange={noop} withFolderIcon={withFolderIcon} searchPlaceholder="搜索项目" />
  </div>
);

const commandSheet = () => (
  <Sheet>
    <Section title="Command" note="Combobox 与命令面板共用的可搜索列表">
      <Specimen name="Command · 分组" className="w-80">
        <Command className="rounded-lg border border-border">
          <CommandInput placeholder="搜索设置" />
          <CommandList>
            <CommandGroup heading="Agent">
              <CommandItem>后端</CommandItem>
              <CommandItem>模型</CommandItem>
            </CommandGroup>
            <CommandGroup heading="系统">
              <CommandItem>服务</CommandItem>
              <CommandItem disabled>诊断与日志</CommandItem>
            </CommandGroup>
          </CommandList>
        </Command>
      </Specimen>
      <Specimen name="CommandEmpty" className="w-80">
        <Command className="rounded-lg border border-border">
          <CommandInput placeholder="搜索设置" value="xyz" />
          <CommandList>
            <CommandEmpty>没有找到匹配的设置</CommandEmpty>
          </CommandList>
        </Command>
      </Specimen>
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'all', title: '字段 · 输入、文本域、选择、多值', width: 1280, height: 680, status: 'shipped', fixtures: systemFixtures(), render: fieldsSheet },
  {
    name: 'combobox-open',
    title: '字段 · Combobox 展开',
    width: 360,
    height: 340,
    status: 'shipped',
    fixtures: systemFixtures(),
    click: ['role=combobox'],
    render: comboboxOpen(false),
  },
  {
    name: 'combobox-folder-open',
    title: '字段 · Combobox 展开（项目选择）',
    width: 360,
    height: 340,
    status: 'shipped',
    fixtures: systemFixtures(),
    click: ['role=combobox'],
    render: comboboxOpen(true),
  },
  { name: 'command', title: '字段 · Command 列表', width: 1280, height: 400, status: 'shipped', fixtures: systemFixtures(), render: commandSheet },
]);

export default designFrames;
