// Choice controls: Checkbox, the two switches (the shared Switch, and Settings' ToggleSwitch with its
// lit track), and SegmentedRadio, the one radio group and segmented control the product shares.
// Tab strips are not a shared primitive: each page draws its own (see the page frames).
import { Checkbox } from '@/components/ui/checkbox';
import { SegmentedRadio, type SegmentedTone } from '@/components/ui/segmented';
import { Switch, SwitchTrack } from '@/components/ui/switch';
import { ToggleSwitch } from '@/components/settings/SettingsPrimitives';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

const noop = () => {};

const modes = [
  { id: 'gateway', label: '模型网关' },
  { id: 'direct', label: '直连' },
  { id: 'off', label: '关闭' },
] as const;

const tones: { tone: SegmentedTone; use: string }[] = [
  { tone: 'mint', use: '默认' },
  { tone: 'cyan', use: '信息类' },
  { tone: 'gold', use: '公开范围' },
  { tone: 'muted', use: '中性' },
];

const choicesSheet = () => (
  <Sheet>
    <Section title="Checkbox" note="18px · 选中为 mint 填充">
      <Specimen name="checked" className="w-28"><Checkbox checked label="已选" /></Specimen>
      <Specimen name="unchecked" className="w-28"><Checkbox checked={false} label="未选" /></Specimen>
      <Specimen name="disabled" className="w-28">
        <div className="flex gap-2">
          <Checkbox checked disabled label="已选" />
          <Checkbox checked={false} disabled label="未选" />
        </div>
      </Specimen>
      <Specimen name="presentational" use="整行可点时只画框" className="w-40">
        <div className="flex gap-2">
          <Checkbox checked presentational />
          <Checkbox checked={false} presentational />
        </div>
      </Specimen>
    </Section>
    <Section title="Switch" note="components/ui/switch：44×24 与 sm 28×16">
      <Specimen name="on" className="w-24"><Switch checked onCheckedChange={noop} label="开" /></Specimen>
      <Specimen name="off" className="w-24"><Switch checked={false} onCheckedChange={noop} label="关" /></Specimen>
      <Specimen name="disabled" className="w-28">
        <div className="flex gap-2">
          <Switch checked disabled onCheckedChange={noop} label="开" />
          <Switch checked={false} disabled onCheckedChange={noop} label="关" />
        </div>
      </Specimen>
      <Specimen name='size="sm"' className="w-24">
        <div className="flex gap-2">
          <Switch size="sm" checked onCheckedChange={noop} label="开" />
          <Switch size="sm" checked={false} onCheckedChange={noop} label="关" />
        </div>
      </Specimen>
      <Specimen name="SwitchTrack" use="菜单行本身就是开关时，只画轨道" className="w-56">
        <div className="flex gap-2">
          <SwitchTrack checked />
          <SwitchTrack checked={false} />
        </div>
      </Specimen>
    </Section>
    <Section title="ToggleSwitch" note="设置页的行内开关：36×20，开启时 mint 轨道带 xs 光晕">
      <Specimen name="enabled" className="w-24"><ToggleSwitch enabled onClick={noop} label="开" /></Specimen>
      <Specimen name="off" className="w-24"><ToggleSwitch enabled={false} onClick={noop} label="关" /></Specimen>
      <Specimen name="disabled" className="w-28">
        <div className="flex gap-2">
          <ToggleSwitch enabled disabled onClick={noop} label="开" />
          <ToggleSwitch enabled={false} disabled onClick={noop} label="关" />
        </div>
      </Specimen>
    </Section>
    <Section title="SegmentedRadio" note="单选组与分段控件；选中项按 tone 着色">
      {tones.map(({ tone, use }) => (
        <Specimen key={tone} name={`tone="${tone}"`} use={use} className="w-72">
          <SegmentedRadio value="gateway" onChange={noop} options={modes} ariaLabel="模型管理方式" tone={tone} />
        </Specimen>
      ))}
      <Specimen name="disabled" className="w-72">
        <SegmentedRadio value="direct" onChange={noop} options={modes} ariaLabel="模型管理方式" disabled />
      </Specimen>
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'all', title: '选择控件 · 复选、开关、单选组', width: 1280, height: 620, status: 'shipped', fixtures: systemFixtures(), render: choicesSheet },
]);

export default designFrames;
