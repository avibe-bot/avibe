# Admin Console Restructure

Page-feedback-driven information-architecture pass on the `/admin` console (the
"Control Panel"). Final scope confirmed with the user (see the Show Page proposal).

## New sidebar order (desktop `AppShell` adminItems — now supports nesting)
1. **控制台** (Dashboard) → `/admin/dashboard` · LayoutDashboard
2. **远程访问** (Remote Access, extracted page) → `/admin/remote-access` · Globe
3. **通讯平台** (nested parent, Link icon) — collapsible submenu:
   - 平台 → `/admin/settings/platforms` · PlugZap
   - 群组 → `/admin/groups` · Hash  (conditional on channel platforms)
   - 私聊 → `/admin/users` · MessageCircle
4. **后端** (extracted from settings tab) → `/admin/settings/backends` · Bot
5. **展示页面** (Show Pages) → `/admin/show-pages` · MonitorPlay
6. **高级设置** (renamed from 设置) → `/admin/settings/messaging` · Settings

## Changes
1. **Sidebar nesting** — `ShellNavItem.children[]`; `ShellNavGroup` collapsible
   submenu (auto-expands when a child is active). Mobile tab bar flattens parents
   to their first child.
2. **Settings tabs** (`SettingsPageShell`) — drop platforms + backends (now their
   own sidebar destinations); reorder remaining to 消息·服务·依赖·诊断; auto-hide
   the tab bar on pages whose `activeTab` isn't a remaining tab (platforms/backends
   render standalone). 设置 nav label → `nav.advancedSettings` (高级设置).
3. **Remote Access** — extract the `#remote-access` block from `SettingsServicePage`
   into `RemoteAccessPage` at `/admin/remote-access`; repoint every link to the old
   `/admin/settings/service#remote` (incl. the dashboard Avibe card).
4. **Agent selector unification** — replace `RoutingConfigPanel`'s 3 selects
   (agent/model/reasoning) with the workbench `AgentRoutePicker` in `UserList` (私聊)
   + `ChannelList` (群组). The picker's built-in "+ new agent → /agents" covers the
   original "manage / add agents" asks.
5. **Entry rename** — `appShell.openControlPanel` 控制面板 → 设置.
6. **Small tweaks** — `/admin/users` workdir input → ~40% width; dashboard Avibe
   (remote) card → leftmost + relinked to `/admin/remote-access`.

## Evidence
UI build (`cd ui && npm run build`) per commit; manual sanity on the admin console.
