# DM Bind & Admin Permission Plan

## Background

Vibe Remote currently allows any user in an enabled channel to access all bot management functions (settings, routing, CWD changes, etc.) via the `/start` menu buttons. There is no permission control — any user can modify bot configuration. Additionally, DM (private chat) with the bot is technically possible on all three platforms but has no authorization mechanism.

This plan introduces:
1. **DM bind mechanism** — users must authenticate via a bind code before using the bot in private chat.
2. **User management UI** — a User List tab parallel to the Channel List, with per-user settings.
3. **Admin permission control** — only designated admins can use management buttons in channels and DMs.

## Goals

- Add a User List tab in the web UI, structurally identical to Channel List, for managing bound users.
- Implement a bind code system so that DM access is explicitly authorized.
- Allow per-user settings (CWD, routing, message types) just like per-channel settings.
- Add an `is_admin` flag per user; only admins can operate management buttons in `/start` menu.
- Route update notifications to Vibe Remote admins instead of platform workspace owners.
- Support all three platforms: Slack, Discord, Lark/Feishu.

## Non-Goals

- Platform-native admin role detection (Slack `is_admin`, Discord `guild_permissions.administrator`, Lark `is_tenant_manager`) — we use a self-managed admin list instead.
- User list fetching from platform APIs (no `users.list`, `guild.fetch_members()`, etc.) — users appear in the list only after binding.
- Multi-platform simultaneous operation (Vibe Remote runs one platform at a time).

## Key Concepts

### Bind Code

A short alphanumeric code (e.g., `vr-a3x9k2`) that authorizes a user to DM the bot. Two types:

| Type | Behavior |
|------|----------|
| **One-time** | Single use, invalidated after one successful bind |
| **Expiring** | Reusable until expiration date, supports multiple binds |

Bind codes are created in the web UI by whoever has access to the admin panel (localhost). The first bind code is auto-generated during setup wizard completion.

### User Lifecycle

```
Admin creates bind code in Web UI
  → User sends `/bind <code>` to bot in DM
  → Bot validates code, creates user entry in settings
  → User appears in User List with default settings
  → Admin can toggle is_admin for that user in Web UI
```

### Permission Model

| Action | Requires Admin? |
|--------|----------------|
| Send messages to bot (in enabled channel) | No |
| Send messages to bot (in DM, after bind) | No |
| `/start` — view menu | No |
| `/clear` — clear conversation | No |
| `/resume` — resume session | No |
| `/cwd` — view current CWD | No |
| Click Settings button | Yes |
| Click Routing button | Yes |
| Click Change CWD button | Yes |
| Click Update Now button | Yes |
| `/bind <code>` — bind in DM | No (requires valid code) |

Unauthorized button clicks receive an ephemeral/private message: "Only admins can change bot settings."

## Data Model

### New: UserSettings (config/v2_settings.py)

```python
@dataclass
class UserSettings:
    display_name: str = ""
    is_admin: bool = False
    bound_at: str = ""  # ISO 8601 timestamp
    enabled: bool = True
    show_message_types: List[str] = field(default_factory=list)
    custom_cwd: Optional[str] = None
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    require_mention: Optional[bool] = None  # Not applicable for DM, but kept for consistency
```

### New: BindCode (config/v2_settings.py)

```python
@dataclass
class BindCode:
    code: str               # e.g. "vr-a3x9k2"
    type: str               # "one_time" or "expiring"
    created_at: str          # ISO 8601
    expires_at: Optional[str] = None  # ISO 8601, only for "expiring" type
    is_active: bool = True   # False after one-time code is used
    used_by: List[str] = field(default_factory=list)  # user_ids that used this code
```

### Extended: SettingsState

```python
@dataclass
class SettingsState:
    channels: Dict[str, ChannelSettings] = field(default_factory=dict)
    users: Dict[str, UserSettings] = field(default_factory=dict)       # NEW
    bind_codes: List[BindCode] = field(default_factory=list)           # NEW
```

### settings.json Example

```json
{
  "channels": {
    "C0123456789": {
      "enabled": true,
      "custom_cwd": "/home/user/project",
      "routing": { "agent_name": "opencode" },
      "show_message_types": [],
      "require_mention": null
    }
  },
  "users": {
    "U0E0FM3QT": {
      "display_name": "cyh",
      "is_admin": true,
      "bound_at": "2026-03-13T10:05:00Z",
      "enabled": true,
      "custom_cwd": null,
      "routing": { "agent_name": "opencode" },
      "show_message_types": [],
      "require_mention": null
    }
  },
  "bind_codes": [
    {
      "code": "vr-a3x9k2",
      "type": "one_time",
      "created_at": "2026-03-13T10:00:00Z",
      "expires_at": null,
      "is_active": false,
      "used_by": ["U0E0FM3QT"]
    },
    {
      "code": "vr-open2026",
      "type": "expiring",
      "created_at": "2026-03-13T10:00:00Z",
      "expires_at": "2026-03-20T10:00:00Z",
      "is_active": true,
      "used_by": ["U12345678"]
    }
  ]
}
```

## API Endpoints (New)

### User Management

| Method | Path | Description |
|--------|------|-------------|
| `GET /users` | — | List all bound users with settings |
| `POST /users` | — | Save all user settings (bulk, same pattern as `/settings`) |
| `POST /users/<user_id>/admin` | `{"is_admin": true}` | Toggle admin flag for a user |
| `DELETE /users/<user_id>` | — | Remove a bound user |

### Bind Code Management

| Method | Path | Description |
|--------|------|-------------|
| `GET /bind-codes` | — | List all bind codes (with usage info) |
| `POST /bind-codes` | `{"type": "one_time"}` or `{"type": "expiring", "expires_at": "..."}` | Create a new bind code |
| `DELETE /bind-codes/<code>` | — | Deactivate/delete a bind code |

### Setup Wizard Extension

| Method | Path | Description |
|--------|------|-------------|
| `GET /setup/first-bind-code` | — | Get or generate the initial bind code (shown on wizard Summary page) |

## IM Command: /bind

### Flow

```
User DMs bot: /bind vr-a3x9k2
  → Bot checks: is this a DM? (Slack: D-prefix, Discord: DMChannel, Lark: p2p)
  → Bot looks up code in settings.bind_codes
  → Validates: is_active? not expired? correct type?
  → If valid:
      - Fetch user info (display_name) via platform API
      - Create UserSettings entry in settings.users
      - If one_time: mark code is_active=false
      - Add user_id to code's used_by list
      - Reply: "Bind successful! You can now use the bot in DM."
      - If this is the first user AND no admins exist: auto-set is_admin=true
  → If invalid:
      - Reply: "Invalid or expired bind code."
```

### DM Message Filtering

In each IM client's message handler, before processing a DM message:

```python
# Pseudocode for all three platforms
if is_dm_message(context):
    user_id = context.user_id
    if not settings_store.is_bound_user(user_id):
        # Check if message is /bind command
        if message.startswith("/bind "):
            await handle_bind_command(context, message)
            return
        # Otherwise reject
        await send_ephemeral(context, "Please use /bind <code> to activate DM access.")
        return
    # Bound user — continue normal processing with user-specific settings
```

## Frontend Changes

### Navigation

AppShell sidebar adds a new nav item:
- **Users** (`Users` icon from lucide-react) → route `/users`

### New Route

| Route | Component | Description |
|-------|-----------|-------------|
| `/users` | `UserList isPage` | User management page |

### UserList Component

Reuse the ChannelList component pattern. The UserList and ChannelList should share as much structure as possible.

**Option A (Recommended): Extract a shared `EntityList` component**

```
EntityList
  ├── props: entities[], entityType ("channel" | "user"), onSave(), ...
  ├── Per-entity: enable toggle, settings (CWD, routing, message types)
  ├── User-specific: is_admin toggle, bound_at display
  └── Channel-specific: require_mention setting
```

Both `/channels` and `/users` pages use `EntityList` with different `entityType` and data sources.

**Option B: Copy ChannelList and modify**

Simpler but duplicates code. Less maintainable.

### Bind Code Management Section

On the `/users` page, above or below the user list, add a "Bind Codes" section:

```
┌─────────────────────────────────────────────────┐
│ Bind Codes                     [+ New Code]     │
├─────────────────────────────────────────────────┤
│ vr-a3x9k2   One-time    Used by cyh    [Copy]  │
│ vr-open2026  Expires 3/20  1 user used  [Copy]  │
│ vr-tmp123    One-time    Active         [Copy]  │
└─────────────────────────────────────────────────┘
```

"+ New Code" opens an inline form:
- Type: One-time / Expiring (radio)
- If Expiring: date picker for expiration
- Generate → shows code with copy button

### Wizard Summary Page Extension

On the Summary page (final wizard step), after "Launch Service":
- Show the auto-generated first bind code
- Instructions: "Send `/bind <code>` to the bot in a DM to become the first admin."

## Implementation Plan

### Phase 1: Data Model & API (Backend)

- [ ] Extend `config/v2_settings.py`:
  - Add `UserSettings` dataclass
  - Add `BindCode` dataclass
  - Extend `SettingsState` with `users` and `bind_codes`
  - Update `SettingsStore._load()` and `.save()` to handle new fields
  - Add helper methods: `get_user()`, `update_user()`, `remove_user()`, `is_bound_user()`, `is_admin()`, `get_admins()`
  - Add bind code methods: `create_bind_code()`, `validate_bind_code()`, `use_bind_code()`, `deactivate_bind_code()`, `get_bind_codes()`
- [ ] Extend `vibe/api.py`:
  - Add `get_users()`, `save_users()`, `toggle_admin()`, `remove_user()` functions
  - Add `get_bind_codes()`, `create_bind_code()`, `delete_bind_code()` functions
  - Add `get_first_bind_code()` for wizard
- [ ] Extend `vibe/ui_server.py`:
  - Register new endpoints: `/users`, `/users/<id>/admin`, `/bind-codes`, `/setup/first-bind-code`

### Phase 2: IM Command & DM Filtering

- [ ] Add `/bind` command handler in `core/handlers/command_handlers.py`:
  - Parse bind code from message
  - Validate code via `SettingsStore`
  - Create user entry with platform user info
  - Auto-admin logic for first user
- [ ] Add DM authorization check in each IM client:
  - `modules/im/slack.py`: In message handler, check `channel_id.startswith("D")` → verify bound
  - `modules/im/discord.py`: In `on_message`, check `is_dm` → verify bound
  - `modules/im/feishu.py`: In message handler, check `is_p2p` → verify bound
- [ ] For unbound DM users: reply with bind instruction, then return (don't process)
- [ ] For `/bind` command: process even if unbound (it's the binding action itself)

### Phase 3: Admin Permission Check

- [ ] Add permission check in `core/handlers/message_handler.py` `handle_callback_query()`:
  - Define protected callback_data prefixes: `cmd_settings`, `cmd_routing`, `cmd_change_cwd`, `vibe_update_now`
  - Before routing to handler, check `settings_store.is_admin(context.user_id)`
  - If not admin: send ephemeral/private rejection message, return
- [ ] Update Discord's existing update button permission check to use the new unified mechanism
- [ ] Add i18n keys for permission error messages

### Phase 4: Frontend — User List & Bind Codes

- [ ] Refactor `ChannelList.tsx` into a shared `EntityList` component (or add `entityType` prop)
- [ ] Create `/users` route and `UserList` page component
- [ ] Add bind code management UI section
- [ ] Add "Users" nav item to `AppShell.tsx`
- [ ] Add `ApiContext` methods: `getUsers()`, `saveUsers()`, `toggleAdmin()`, `removeUser()`, `getBindCodes()`, `createBindCode()`, `deleteBindCode()`
- [ ] Add i18n keys (en.json + zh.json) for all new UI strings
- [ ] Extend wizard Summary step to show first bind code

### Phase 5: Update Notification Reroute

- [ ] Modify `core/update_checker.py`:
  - Replace Slack workspace owner lookup with `settings_store.get_admins()`
  - Send update DM to each admin user via `im_client.send_dm(user_id, message)`
- [ ] Add `send_dm(user_id, message)` to `BaseIMClient` interface if not present
- [ ] Implement `send_dm()` in all three IM clients:
  - Slack: `conversations_open(users=user_id)` → `chat_postMessage(channel=dm_channel_id)`
  - Discord: `user.create_dm()` → `dm_channel.send()`
  - Lark: `POST /im/v1/messages?receive_id_type=open_id` with `receive_id=user_open_id`

## Testing

- Manual: Start bot → generate bind code in UI → DM bot with `/bind <code>` → verify DM works
- Manual: Set user as admin in UI → verify only admin can click Settings/Routing buttons
- Manual: Non-admin clicks Settings → verify rejection message appears
- Manual: Verify one-time code becomes inactive after use
- Manual: Verify expiring code rejects after expiration
- Manual: Verify first bound user auto-becomes admin
- Manual: Verify update notification goes to admins, not workspace owners

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| User loses the first bind code | Web UI can always generate new codes; the setup person has localhost access |
| All admins are removed | Prevent removing the last admin in both UI and API |
| Bot restart clears bind state | All state is persisted to `settings.json` on disk |
| Platform user_id format differences | IDs are opaque strings, no format assumptions; one platform active at a time |
| Existing deployments have no admins | Migration: if `users` key is missing in settings.json, all management buttons remain open (backward compatible). First `/bind` creates the admin. Or: show a migration prompt in web UI. |

## Migration Strategy

For existing Vibe Remote deployments upgrading to this version:

1. `settings.json` without `users`/`bind_codes` keys → treated as empty (no users, no bind codes).
2. **DM behavior**: If no users are configured, DM messages are rejected with bind instructions (same as new installs).
3. **Channel behavior**: If no admins exist, management buttons remain unrestricted (backward compatible). Once the first admin is created, permission enforcement activates.
4. Web UI shows a banner: "No admins configured. Create a bind code and bind a user to enable permission control."
