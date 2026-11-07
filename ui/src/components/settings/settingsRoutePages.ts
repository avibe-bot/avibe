// Every page the Settings routes render, as one chunk: Settings opens as a
// whole, and its sections move between each other without another fetch.
// Model Hub keeps its own, larger chunk (SettingsModelsPage).
export { ChannelList } from '../steps/ChannelList';
export { UserList } from '../steps/UserList';
export { RemoteAccessPage } from '../RemoteAccessPage';
export { PermissionsPage } from '../../features/permissions/PermissionsPage';
export { SettingsLayout } from './SettingsLayout';
export { SettingsDiagnosticsPage } from './SettingsDiagnosticsPage';
export { SettingsBackendsPage } from './SettingsBackendsPage';
export { SettingsDependenciesPage } from './SettingsDependenciesPage';
export { SettingsClaudeProviderPage } from './SettingsClaudeProviderPage';
export { SettingsCodexProviderPage } from './SettingsCodexProviderPage';
export { SettingsBackendPage } from './SettingsBackendPage';
export { SettingsOpencodeProviderPage } from './SettingsOpencodeProviderPage';
export { SettingsLogsPage } from './SettingsLogsPage';
export { SettingsMessagingPage } from './SettingsMessagingPage';
export { SettingsPlatformsPage } from './SettingsPlatformsPage';
export { SettingsServicePage } from './SettingsServicePage';
export { SettingsGeneralPage } from './SettingsGeneralPage';
export { SettingsShortcutsPage } from './SettingsShortcutsPage';
export { SettingsNotificationsPage } from './SettingsNotificationsPage';
export { SettingsComputerUsePage } from './SettingsComputerUsePage';
