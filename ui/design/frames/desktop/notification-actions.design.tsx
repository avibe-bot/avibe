// D2 (Desktop): answer an Agent from the macOS notification, confirm a follow-up into the Mac's Reminders,
// and the honest result when an action does not go through. The notifications are illustrations (macOS
// draws them); Settings is the shipped layout with the proposed pieces. The owner decided (2026-11-21) that
// an answer that did not go through is explained by its follow-up notification alone: no in-app notice.
import { frames } from '../../frame';
import {
  DesktopNotificationsSettings,
  OutcomeNotifications,
  QuestionNotification,
  ReminderNotification,
  type DesktopDeviceState,
} from '../../kit/desktop-notification-actions';
import { settingsFixtures, settingsPage } from '../settings/fixtures';

const settings = (state: DesktopDeviceState) => settingsPage('notifications', <DesktopNotificationsSettings state={state} />);
const settingsRoute = '/settings/notifications';
const settingsData = settingsFixtures({}, { desktopShell: true });

const designFrames = frames([
  { name: 'question-options', title: '通知 · 等你回复 · 选项展开（示意）', width: 960, height: 420, status: 'approved', render: () => <QuestionNotification open /> },
  { name: 'question-reply', title: '通知 · 等你回复 · 在通知里回复（示意）', width: 960, height: 420, status: 'approved', render: () => <QuestionNotification reply /> },
  { name: 'reminder-confirm', title: '通知 · 添加到提醒事项 · 确认（示意）', width: 960, height: 420, status: 'approved', render: () => <ReminderNotification /> },
  { name: 'outcomes', title: '通知 · 动作没有生效时的结果（示意）', width: 960, height: 560, status: 'approved', render: () => <OutcomeNotifications /> },
  { name: 'settings-ready', title: '设置 · 通知 · 本设备 · 都已允许', width: 1440, height: 900, status: 'approved', route: settingsRoute, fixtures: settingsData, render: settings('ready') },
  { name: 'settings-needs-permission', title: '设置 · 通知 · 本设备 · 系统未允许通知', width: 1440, height: 900, status: 'approved', route: settingsRoute, fixtures: settingsData, render: settings('needs-permission') },
  { name: 'settings-reminders-denied', title: '设置 · 通知 · 本设备 · 提醒事项被拒绝', width: 1440, height: 900, status: 'approved', route: settingsRoute, fixtures: settingsData, render: settings('reminders-denied') },
  { name: 'settings-unsupported', title: '设置 · 通知 · 本设备 · 系统版本不支持提醒事项', width: 1440, height: 900, status: 'approved', route: settingsRoute, fixtures: settingsData, render: settings('unsupported') },
]);

export default designFrames;
