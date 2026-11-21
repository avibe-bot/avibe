// I5 · one long Turn on the Lock Screen and in the Dynamic Island (iOS Live Activity), proposed
// 2026-11-20. It follows the one running Turn of the conversation the person was viewing, and
// ends with it (Amendment 1: it starts by itself when the person leaves the App, behind the setting in
// ui/design: settings/live-activity/*; the conversation page has no entry). A card shows the Avibe mark, the session title, the Agent's display name, one of five
// states, and times; never message text, tools, files, or the instance. Title and name are written on the
// phone when tracking starts and never travel through the relay. Updates come from the instance
// through the push relay (state only); the elapsed timer counts on the phone. A card past its stale
// date stops its timer and says it may be out of date. Tapping opens that conversation, where any
// approval or question is answered as today. Contract: docs/plans/2026-11-20-ios-live-activity-contract.md.
// Kit: ui/design/kit/live-activity.tsx.
import { frames } from '../../frame';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import { LockScreen, Specimen, SpecimenSheet } from '../../kit/live-activity';
import { Card, Compact, Expanded, Minimal, type StateName } from './fixtures';
import { chatFixtures, chatSession, runningTurn, userSays } from '../chat/fixtures';

const bot = chatSession({ id: 'ses_cso', project_id: null, scope_id: null, title: '首席安全官', agent_name: 'cso' });
const runningChat = chatFixtures({
  session: bot,
  messages: [userSays('看一下这周新增依赖有没有高危漏洞，有问题直接开修复 PR。', 42)],
  turnState: runningTurn,
  extra: { nativeShell: { ...IOS_APP_CAPABILITIES, liveActivities: 2 } },
});
const lock = (name: string, title: string, state: StateName, time?: string) => ({
  name,
  title,
  width: IPHONE_17_PRO.width,
  height: IPHONE_17_PRO.height,
  status: 'proposed' as const,
  fixtures: runningChat,
  render: () => (
    <LockScreen time={time}>
      <Card state={state} />
    </LockScreen>
  ),
});

const designFrames = frames([
  lock('lock-working', '锁屏 · 正在处理：会话标题 + Agent 名 + 计时（标题和名字在手机本地写入，计时在手机本地走）', 'working'),
  lock('lock-needs-you', '锁屏 · 等你处理：Turn 停在审批或提问上；轻点回到对话里的现有入口，锁屏上不审批', 'needsYou'),
  {
    name: 'lock-states',
    title: '锁屏卡片 · 状态对照：进行中 / 等你处理 / 已完成 / 没有完成 / 已停止 / 可能不是最新 / 伙伴会话（无标题）',
    width: 420,
    height: 1040,
    status: 'proposed',
    fixtures: runningChat,
    render: () => (
      <SpecimenSheet>
        <Specimen label="working"><Card state="working" /></Specimen>
        <Specimen label="needsYou"><Card state="needsYou" /></Specimen>
        <Specimen label="done · ends, dismissed after 1 h"><Card state="done" /></Specimen>
        <Specimen label="failed · ends, dismissed after 1 h"><Card state="failed" /></Specimen>
        <Specimen label="stopped · ends, dismissed after 1 h"><Card state="stopped" /></Specimen>
        <Specimen label="stale · past stale date, timer stops"><Card state="stale" /></Specimen>
        <Specimen label="a Bot's session · no title, the Agent name leads"><Card state="botSession" /></Specimen>
      </SpecimenSheet>
    ),
  },
  {
    name: 'island',
    title: '灵动岛 · 紧凑（进行中计时 / 等你处理 / 已完成 / 可能不是最新）、最小、展开',
    width: 420,
    height: 820,
    status: 'proposed',
    fixtures: runningChat,
    render: () => (
      <SpecimenSheet>
        <Specimen label="compact · working"><Compact state="working" /></Specimen>
        <Specimen label="compact · needsYou"><Compact state="needsYou" /></Specimen>
        <Specimen label="compact · done"><Compact state="done" /></Specimen>
        <Specimen label="compact · stale"><Compact state="stale" /></Specimen>
        <Specimen label="minimal · working / needsYou"><div className="flex gap-3"><Minimal state="working" /><Minimal state="needsYou" /></div></Specimen>
        <Specimen label="expanded · working"><Expanded state="working" /></Specimen>
        <Specimen label="expanded · needsYou"><Expanded state="needsYou" /></Specimen>
      </SpecimenSheet>
    ),
  },
]);

export default designFrames;
