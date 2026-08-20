# Show Page 独立权限体系：MVP 需求与实现方案

状态：产品决策已拍板（卡点 1/2/3 见 §2.5）；实现交接文档。范围扩展：组织级分享上限策略（企业统一策略）纳入本计划，见 §17（跨 repo，依赖 §16 的 L1–L3 落地后实施）。

目标：修正当前把 Show Page 的“组织内访问”误实现为通用 Resource ACL 的设计，交付一套完全由 Show Page 自己拥有、按入口分别生效的页面权限体系。

## 1. 最终结论

Show Page 有两条彼此独立的页面级权限轴：

| 权限轴 | 页面入口 | 负责什么 | 本次是否重做 |
| --- | --- | --- | --- |
| Link access（链接访问） | `/p/<share_id>/` | 通过分享链接访问页面 | 否，继续使用 #1498/#1501 的本地 `ShowAccess` |
| Organization access（组织内访问） | `/show/<session_id>/` | 登录后的组织成员访问页面 | 是，改为独立的 Show Page ACL |

二者都属于 Show Page 权限体系，但作用入口不同，不能互相授予权限：

- Link access 允许访问 `/p`，不代表可以访问 `/show`。
- Organization access 允许访问 `/show`，不代表可以绕过 `/p` 的 Private/Limited 规则。
- `/show` 的授权判定不得调用 Avibe 实例的 Resource ACL。
- Show Page 也不得继续作为 `resource_access_policies.resource_kind = "show_page"` 参与通用 Resource ACL 同步。

本次只实现 MVP 核心行为，不处理 owner 离开组织后的转移、回收或自动改权。

实现基线：

- #1498/#1501 的本地 Link access（`ShowAccess`、`/p`、exact-email Limited 流程）视为已有能力，不能回退。
- #1516 已完成的当前 Instance/Group 认证链路视为已有能力；本任务消费可信的 Organization membership、role 和 group claims，不重新引入 Cloud Management OAuth。
- #1522 中把 Show Page Organization access 定义成通用 Resource ACL 的部分被本需求取代；不能以 #1522 的 Resource API、Cloud sync 或 ACK 作为实现目标。

## 2. 已确认的产品规则

### 2.1 Organization access 三档

图 2 的三档是“组织内登录访问”，中文和英文显示如下：

| 内部值 | 中文文案 | 英文文案 | 含义 |
| --- | --- | --- | --- |
| `private` | 私有 | Private | 只有页面 owner，以及组织 owner/admin 例外可访问 |
| `scope` | 指定群组 | Selected groups | 只有同一 Organization 中属于至少一个所选群组的成员可访问 |
| `organization` | 组织成员 | Organization members | 同一 Organization 的成员均可访问 |

UI 顺序固定为从紧到松：

```text
私有 | 指定群组 | 组织成员
Private | Selected groups | Organization members
```

不要再使用 `Workspace access`、`Workspace` 等术语。section 的英文使用 `Organization access`，中文使用“组织内访问”。代码内部可以暂时保留兼容性的 `workspace*` key 或组件名，但所有新增和最终用户可见文案必须改成 Organization 语义；清理时应删除无意义的 workspace 命名残留。

### 2.2 访问和管理角色

以下授权优先级均以 §2.5 的 instance 准入为先决条件（未准入者一律拒绝，不进入优先级判定）。页面 owner 是页面上的稳定身份（`owner_user_id`），不随当前 Organization role 变化而改变；页面 owner 在仍具备 instance 准入时始终可访问/管理自己的页面。

授权优先级如下（与 §4.1 伪代码一致，页面 owner 优先于组织角色判定）：

1. 当前用户是页面 owner：允许访问，并可管理该页面自己的 Organization access（页面 owner 是稳定身份，不要求仍为组织活跃成员；owner 离开组织的转移不在 MVP 内）。
2. 当前用户是该页面所属 Organization 的 `owner` 或 `admin`：自动允许访问，并自动拥有该页面 Organization access 的管理权限。
3. 其他用户按页面的 `access_level` 判定。

明确禁止以下隐式绕过：

- `Instance Owner` 身份本身不自动绕过 Show Page ACL。
- Avibe 实例的 Resource ACL 不得覆盖、收紧或放宽 Show Page ACL。
- 具有 Organization 管理权限不应自动获得其他实例的页面访问权限；必须先属于该页面的 Organization，且使用当前已认证的实例/session 上下文。

这里的“自动管理”是产品要求，不只是“能读”：Organization owner/admin 可以读取和修改该 Organization 内所有 Show Page 的 Organization access；普通成员只能按页面 ACL 访问，不能管理。

### 2.3 Personal Avibe

Personal Avibe 没有 Organization，因此：

- 不显示“组织内访问”区块。
- 不加载 Organization groups。
- 不调用 Organization access 读写 API。
- `/show` 的默认有效规则是 `private`：只有页面 owner 可访问。
- 不能通过伪造 `organization` 或 `scope` 请求把 Personal 页面变成组织访问。

### 2.4 认证前提

“不受 Resource ACL 控制”不等于“无需登录”。本 MVP 仍使用现有可信 OIDC/session 认证，访问者必须先获得服务端验证过的身份、Organization membership、Organization role 和 group claims。

本次不改变现有 session admission/OIDC 合约，也不承诺一个没有任何合法 instance/session authorization 的用户可以绕过登录直接进入 `/show`。本次改变的是：在用户已经通过现有认证、拿到可信 claims 后，Show Page 最终授权不再调用 `can_use_resource("show_page", ...)`。

如果后续要允许“仅有 Organization membership、没有该 Avibe instance access”的用户进入 `/show`，必须另立 backend/OIDC contract；开发 agent 不得在本任务中自行扩大认证范围。

### 2.5 已拍板的准入判定（2026-08-20）

以下三点已由 owner 拍板，实现必须以本小节为准，不得回退：

1. **“组织成员 / 指定群组”的准入 = 在已获准入的 instance 用户里按组织过滤（方案 1a），不扩大到无 instance access 的组织成员。** 新 evaluator 分两层，均以服务端已验证 claims 为准：
   - **Instance 准入（对所有 `/show` 生效，含 Personal 页面）**：`has_role("viewer")`（`instance_role ∈ {owner, editor, viewer}`）且 `instance_access_source != "show_page_email"`（旧 hosted grant 不得准入）。签名的 exact-page entitlement（`show_page_id`）只属于 `/p` Limited guest 链路，不得作为 `/show` 的准入依据。
   - **Organization membership（仅对 org 页面生效）**：`is_active_organization_member` 为真且 `organization_id == page.organization_id`。
   含义：`/show` 最终授权不再调用 `can_use_resource("show_page", …)`，但仍消费上述独立 instance/session 准入断言；不含这些可信 claims 的请求一律不能进入 `/show`。Personal 页面没有组织层，只有页面 owner 可访问（§2.3/§4.1）。若未来要允许“仅有 Organization membership、没有 instance access”进入 `/show`，另立 backend/OIDC contract，本 MVP 不做。

2. **Instance Owner 不再隐式绕过 Show Page ACL（行为回退已确认）。** 仅页面 owner 与页面所属 Organization 的 owner/admin 自动访问并自动管理。旧 `is_instance_owner → allow` 的 use/manage 两条短路都必须移除。

3. **Organization owner/admin 自动管理已确认。** 管理判定消费 `organization_role ∈ {owner, admin}`（且 `organization_id == page.organization_id`），仅作用于 Organization access，不扩展为 Link access 的 exact-email 管理。

## 3. Link access 与 Organization access 的边界

### 3.1 Link access：继续沿用 #1498

`/p/<share_id>/` 是分享链接表面，继续由本地 `ShowAccess` aggregate 控制：

- `private`：不提供可用分享访问。
- `limited`：使用稳定 `share_id` 和规范化后的 exact email 列表；访问者需要通过现有 limited guest/session 流程。
- `public`：分享链接可公开访问，具体匿名/availability 行为继续遵循 #1498/#1501 的现有约定。

Link access 的字段包括现有 `show_pages.access_mode`、`access_revision`、`share_id` 和 `show_page_authorized_emails`。本任务不把 Organization group 选择写入 exact email，也不把 Organization access 三档映射成 Link access 三档。

### 3.2 Organization access：本任务重做

`/show/<session_id>/` 是登录后的 Show Page Workbench 表面，最终授权只读取新的 Show Page 页面 ACL和当前可信身份：

- `private`、`scope`、`organization` 三档只影响 `/show`。
- 不读取 `show_pages.access_mode` 来决定 `/show` 是否可进入。
- 不读取 `show_page_authorized_emails` 来决定 `/show` 是否可进入。
- 不读取 `resource_access_policies` 或 `resource_access_groups` 来决定 `/show` 是否可进入。

页面是否 offline、Show Runtime 是否可用、资源文件是否允许读取，仍是运行时/可用性/路径安全问题，不得和 Organization access 合并成一个权限字段。

## 4. 授权判定契约

定义：

- `page`: Show Page 页面。
- `page.organization_id`: 页面所属 Organization；Personal 页面为 `null`。
- `page.owner_user_id`: 页面 owner 的稳定用户 ID。
- `subject`: 当前已认证用户，包含 `user_id`、规范化 email、`organization_id`、Organization role 和 group IDs。

### 4.1 页面访问判定（含 Personal）

服务端应在一个独立的 Show Page access service 中实现下面的语义，所有 `/show` 入口复用同一个 evaluator：

```text
authorize_show_page(page, subject, operation):
    # Instance admission (§2.5): applies to every /show, including Personal pages.
    if subject is not authenticated:
        return deny(unauthenticated)
    if not subject.has_role("viewer") or subject.instance_access_source == "show_page_email":
        return deny(not_admitted)

    # Page owner is a stable identity, independent of current Organization role.
    if subject.user_id == page.owner_user_id:
        if operation == manage:
            return allow(manage)
        return allow

    if page.organization_id is null:
        # Personal page: only the owner (handled above).
        return deny

    if subject.organization_id != page.organization_id:
        return deny(wrong_organization)
    if not subject.is_active_organization_member:
        return deny(not_member)

    # Organization owner/admin auto-access and auto-manage (§2.5 decision 3).
    if subject.organization_role in {owner, admin}:
        if operation == manage:
            return allow(manage)
        return allow

    if page.access_level == private:
        return deny(not_member)

    if page.access_level == organization:
        return allow

    if page.access_level == scope:
        if subject.group_ids intersects page.group_ids:
            return allow
        return deny(group_not_selected)

    return deny
```

实现时不要把这段算法散落在 middleware、API handler、Store 和 React 组件中。应有一个服务端单一判定函数，例如：

```text
can_access_show_page(page_id, subject_context) -> decision
can_manage_show_page_access(page_id, subject_context) -> decision
```

`decision` 至少应能区分 `allow`、`unauthenticated`、`not_admitted`、`wrong_organization`、`not_member`、`group_not_selected`、`page_not_found` 和 `management_forbidden`，便于 HTTP 层返回稳定错误码和 UI 显示正确状态。

### 4.2 管理判定

MVP 管理者为：

- 页面 owner（Personal 页面仅页面 owner 可管理，无组织角色）；
- 页面所属 Organization 的 owner/admin（仅组织页面）。

管理权限仅针对该页面的 Organization access ACL。它不自动允许：

- 修改其他用户的 Instance role；
- 修改 Project/Resource ACL；
- 修改 Link access 的 exact email（除非该用户同时满足既有 Link access 管理规则）；
- 管理其他 Organization 的页面。

## 5. 当前实现为什么不符合需求

当前代码的链路是：

```text
ShowPage UI
  -> /api/permissions/resources/show_page/...
  -> resource_access_service
  -> Cloud desired policy / local sync / ACK
  -> /show middleware
  -> ShowPageStore.require_access()
```

主要证据：

- `show_page` 被注册为通用 Resource kind：`storage/resource_access_service.py` 的 `RESOURCE_KINDS`。
- 通用 evaluator 在 `storage/resource_access_service.py` 中处理 `show_page` 的 `private/public/scope`。
- `core/show_pages.py` 的 `ShowPageStore.require_access()` 调用 Resource ACL。
- `vibe/ui_server.py` 的 remote `/show` middleware 和 `/show` route 都调用 `_show_page_resource_access_allowed()`/`require_access()`。
- `vibe/api.py` 与 `vibe/ui_server.py` 提供 `/api/show-pages/<session_id>/access`，内部继续调用 `resource_access_service.can_manage_show_page_access()`。
- `ui/src/components/workbench/ShowPageWorkspaceAccessControl.tsx` 直接请求通用 Resource API，并显示 `pending/offline/error/in_sync` 等 Resource sync 状态。

因此当前截图里的“Avibe has not acknowledged the latest policy”属于 Resource ACL 同步状态，不是 Show Page 独立权限状态。这个问题不是某个 group 没同步或某个用户 claims 缺失，而是权限 owner 和判定入口选错了。

## 6. 目标数据模型

Show Page ACL 必须成为独立本地聚合，不复用 `resource_access_policies` 的语义。推荐新增两张表；命名可以按现有 migration 风格调整，但字段语义必须保持一致。

> **前向兼容（§17 组织级上限策略）**：本 MVP 的两张表只承载「owner 的正向授权」。组织级上限策略是 Cloud 权威的**负向收紧**，由 §17 独立投影，不写入 `show_page_access_policies`。但 evaluator（§4.1）与 `can_access_show_page`/`can_manage_show_page_access` 的签名必须**从 L2 起预留一个可选的组织上限输入**（MVP 默认「无上限」，即返回原始判定），避免 §17 落地时破坏判定契约。

### 6.1 页面策略表

```text
show_page_access_policies
  page_id                    PK/FK -> show_pages.session_id
  organization_id            nullable for Personal, exact owning Organization for org page
  owner_user_id              nullable only for legacy-unresolved rows
  owner_email                optional audit/display provenance
  access_level               private | scope | organization
  policy_revision             integer CAS revision
  created_by_user_id          audit field
  updated_by_user_id          audit field
  created_at
  updated_at
```

约束：

- `page_id` 唯一；一页只能有一份 Show Page Organization access policy。
- `access_level = private` 或 `organization` 时，group rows 必须为空。
- `access_level = scope` 时，MVP 要求至少一个 group row。
- 所有 group row 的 `organization_id` 必须等于页面策略的 `organization_id`。
- Personal 页面不能有 `organization_id`，也不能有 group rows。

### 6.2 页面群组绑定表

```text
show_page_access_groups
  page_id                    PK part/FK -> show_page_access_policies.page_id
  group_id                   PK part
  organization_id            exact Organization binding
  created_at
```

不要把 `group_id` 仅存成 JSON 字符串。需要数据库级唯一性、级联删除和跨组织校验，避免一页的 group binding 在换组织或重复保存时产生歧义。

### 6.3 owner provenance

当前 `show_pages` 表没有 page owner 字段；现有 owner provenance 在 `resource_access_policies.owner_user_id/owner_email` 中。迁移时必须先把 owner、Organization binding、access level、group IDs 和审计必要信息复制到新表，再停止运行时读取旧表。

不能直接删除 `resource_access_policies(resource_kind = 'show_page')` 后再尝试推断 owner；那会使现有页面失去明确的 owner，导致 Private 页面无法判定。

## 7. 迁移与兼容策略

这是从错误的 Resource 实现迁移到正确的 Show Page 独立实现，不是另起一套并长期双写。

### 7.1 首次迁移

对已有 Show Page：

1. 创建对应的 `show_page_access_policies`。
2. 从现有 `resource_access_policies`/`resource_access_groups` 读取 owner、Organization 和当前 `private/public/scope` 语义，映射为 Show Page 的 `private/organization/scope`。
3. `public -> organization`，`scope -> scope`，`private -> private`；这是 wire 值到页面语义的迁移，不再把 `public` 显示成 internet public。
4. 复制 group rows，并验证它们属于同一 Organization；不合法的绑定 fail closed，不能扩大访问。
5. 保留原有 `policy_revision` 作为新表初始 CAS revision，或采用明确的 `revision + 1` 规则，必须在迁移测试中固定下来。
6. 新表成功落地后，运行时不再读取 `resource_access_policies` 的 `show_page` rows。

### 7.2 异常和未绑定状态

- 找不到旧 policy：创建 `private`，owner 必须来自可信的页面创建 provenance；无法确定 owner 时拒绝普通用户访问，并给页面所属 Organization 的 owner/admin（Personal 页面为 instance owner，作为本地唯一权威）可恢复的诊断状态。
- Organization ID 缺失：不能把页面误判成 Personal 后开放 Personal 行为；保持 `private`/pending，普通用户 fail closed。
- group 跨 Organization：不自动改写、不自动搬迁，保持不可用并记录冲突。
- migration 重跑必须幂等，不能覆盖用户已经在新表中修改的策略。

### 7.3 旧 Resource rows 的处理

MVP 不要求删除通用 Resource ACL 表，也不影响 `agent`、`vault_secret`、`skill`。但 Show Page 完成迁移后必须：

- 停止发布 `show_page` Resource index descriptor。
- 停止拉取、应用、ACK `show_page` Resource ACL intents。
- 停止对 `show_page` 调用 `can_use_resource()` 和 `can_manage_resource_acl()`。
- 停止把 `show_page` 传入 `/api/permissions/resources/...`。
- 删除 Show Page 专用的 Resource sync 状态、Cloud desired revision 和相关 UI。
- 旧 `show_page` Resource rows 可以在一个兼容 migration 中保留或标记为 migrated，待后续独立清理 PR 删除；不能继续作为运行时授权来源。

## 8. 服务端/API 实现契约

### 8.1 独立服务

新增独立模块，例如 `storage/show_page_access_service.py`；也可以将其放在 `core/show_pages.py` 的独立子域，但不得继续依赖 `resource_access_service.py` 的 evaluator。

该服务负责：

- policy/group 的读取和 CAS 写入；
- 当前页面所属 Organization 和 owner 的可信解析；
- `can_access` 与 `can_manage`；
- 同一事务内替换完整 group set；
- 同一 Organization 校验；
- migration/adoption 的幂等入口。

`AuthorizationContext.can_use_show_page()` 当前是 exact-page signed email entitlement 语义，不能直接当作新的 Organization ACL evaluator；开发 agent 不得只做名称替换。

### 8.2 本地 HTTP API

推荐保留 Show Page 语义清晰的同源 API，而不是复用通用 Resource API：

```text
GET  /api/show-pages/{session_id}/organization-access
PUT  /api/show-pages/{session_id}/organization-access
```

读响应至少包含：

```json
{
  "page_id": "ses...",
  "organization_id": "org...",
  "owner_user_id": "user...",
  "access_level": "private|scope|organization",
  "group_ids": ["group..."],
  "policy_revision": 3,
  "can_access": true,
  "can_manage": true,
  "instance_kind": "personal|organization",
  "state": "ready|pending|conflict|unavailable"
}
```

写请求至少包含：

```json
{
  "access_level": "private|scope|organization",
  "group_ids": ["group..."],
  "if_match_revision": 3
}
```

规则：

- Server 从已配对的当前 Avibe/session 解析 instance 和 Organization，不接受浏览器提交的 Organization 作为授权依据。
- `if_match_revision` 必须 CAS；冲突返回当前权威 snapshot，不能部分写入。
- `scope` 没有 group IDs 时返回稳定校验错误，不保存空 scope。
- `private` 和 `organization` 带 group IDs 时拒绝或规范化为空；推荐拒绝，便于暴露客户端 bug。
- mutation 需要 `can_manage_show_page_access`，不能只依赖 UI 是否显示编辑控件。
- 任何响应都必须确认 `page_id`、当前配对 instance 和 Organization 与请求上下文一致。
- API 不再返回 Resource `sync.status`、`last_applied_control_plane_revision` 或 Cloud ACK 字段。

如果现有 `/api/show-pages/{session_id}/access` 已被 #1498 用于 Link access settings，不能让它同时承载 Organization access；应拆成明确的 endpoint 或明确的 response namespace，避免两个 `access` 概念再次混淆。

### 8.3 `/show` route/middleware

所有会返回 `/show` HTML、SPA asset、API、event 或 media 的入口，都必须经过同一个 Show Page ACL gate；不得只修 HTML route 而遗漏 middleware、文件、事件或媒体接口。

目标顺序：

```text
解析 session_id
  -> 取得 Show Page
  -> 建立/读取独立 Show Page ACL
  -> 读取已认证 subject
  -> can_access_show_page
  -> 通过后继续 Runtime/asset/API 处理
```

如果请求尚未完成认证：

- HTML document navigation 可以按现有流程发起登录/重定向；
- asset、XHR、event、media 请求返回稳定的 401/403，不启动浏览器 OAuth；
- 不得通过错误状态伪装成 Resource ACL offline/pending。

如果页面 ACL 变更后用户已经打开 `/show`，后续请求必须重新按当前 policy 判定；不要求主动关闭已渲染的浏览器页面，但不能继续接受新的受保护请求。

### 8.4 `/p` route

`/p/<share_id>/` 继续由 `ShowAccess`/limited guest lease 控制。实现独立 ACL 时不要在 `/p` route 加入 Organization group membership 判断，也不要让 Organization owner/admin 通过 `/show` ACL 绕过 `/p` 的链接规则。

## 9. 前端实现要求

### 9.1 Share popover 结构

Organization Avibe 的 Show Page 分享设置中，两个 section 并列展示，但必须明确是两个入口（一个管 `/p` 只读，一个管 `/show` 工作台）：

```text
Link access（分享链接 · 只读）
  Private | Limited | Fully public
  Limited 时显示 exact email 输入/列表

Organization access（组织内访问 · 完整工具）
  Private | Selected groups | Organization members
  Selected groups 时显示组织群组选择
```

中文文案：

```text
分享链接（只读）
  私有 | 仅限名单 | 任何人可看

组织内访问（完整工具）
  私有 | 指定群组 | 组织成员
```

- **Personal Avibe 只显示 Link access，不显示 Organization access 区块。** 组织区块只在组织实例（或 `pending/conflict` 需诊断状态时）出现，隐藏本身不是错误。
- **组织区块在组织实例上默认折叠**，由「Organization access」标题行展开，以减少对仅使用 Link access 的用户的干扰。折叠/展开不改变两轴模型，也不影响任何判定——它只是组织轴编辑器（不是访问模式本身）的默认收合。页面 owner 与组织 owner/admin 在展开后看到可编辑控件；普通成员看到只读态。
- **两个 section 不合并**：Link access 的 `Fully public` 只授予 `/p` 匿名只读，不等于组织成员停止通过 `/show` 协作；Organization access 与 Link access 保持独立，选 `Fully public` 不得隐藏或禁用 Organization access 区块。Link 邮箱（外部客人）与组织群组（组织成员）是两个不同 authority 的受众，不在 UI 或存储中叠加去重。
- Organization access 的 tab 顺序固定为 `Private -> Selected groups -> Organization members`，中文为“私有 -> 指定群组 -> 组织成员”。不要显示单独的 “Workspace” 选项，也不要把 group selection 设计成与“组织成员”并列的另一种 instance/resource 权限。

### 9.2 UI 状态

UI 需要显示 Show Page ACL 自己的状态：

- loading；
- ready；
- no organization / Personal（区块隐藏而不是错误）；
- pending/conflict/unavailable（保持私有或只读，不能误显示 Personal 并开放）；
- revision conflict（保留用户草稿，刷新权威 policy）；
- no active groups；
- scope 缺少 group 的校验错误；
- no management permission（可读但不可编辑）。

删除或改写这些错误文案：

- “This Avibe has not acknowledged the latest policy.”
- “offline / applying / in sync” Resource policy 状态。

它们描述的是通用 Resource 同步，不是 Show Page 独立 ACL。新的文案应描述页面 ACL 当前不可验证、冲突或等待 Organization binding。

### 9.3 i18n

所有用户可见文案放入 `ui/src/i18n/en.json` 与 `ui/src/i18n/zh.json`，并保持 key parity。至少覆盖：

- `Link access`（分享链接 · 只读）/ `分享链接（只读）`；
- `Organization access`（组织内访问 · 完整工具）/ `组织内访问（完整工具）`；
- `Private` / `私有`；
- `Selected groups` / `指定群组`；
- `Organization members` / `组织成员`；
- 组织群组列表、无群组、至少选择一个群组；
- 组织区块折叠/展开标签；
- 无管理权限、冲突、不可用和保存失败。

### 9.4 明确不做的 UI 合并

（已与 owner 确认，写入以固化，实现不得擅自改回。）

- **不按 Link access 的档位隐藏/禁用 Organization access**：选 `Fully public` 时组织区块仍展示（组织实例上），两个轴独立。
- **不把 Limited 邮箱与组织群组合并去重**：邮箱是外部只读客人、群组是组织工作台成员，authority 与能力都不同，不得在 UI、存储或判定中叠加去重。
- **不改两轴模型**：§1/§2.5 的 Link access 与 Organization access 分离是产品基线。

## 10. 代码清理边界：完成后不能留错误尾巴

完成新链路后，对 Show Page 做一次全局引用审计。以下引用必须归零，除非在兼容 migration 中有明确注释且不再被运行时调用：

- `resource_kind == "show_page"` 的访问 evaluator、管理 evaluator 和 route guard；
- Show Page 对 `resource_access_policies`、`resource_access_groups` 的读写；
- Show Page 对 `/api/permissions/resources/show_page/...` 的请求；
- `show_page` resource index/descriptor、desired policy、ACK/revision sync；
- `_show_page_resource_access_allowed` 作为 `/show` 最终授权函数；
- UI 中把 Organization access 绑定到 resource `public/scope/private` 的映射；
- UI 中的 Resource sync status；
- 以 workspace 命名表达 Organization access 的新文案或新 API。

以下内容继续保留：

- `storage/resource_access_service.py` 对 `agent`、`vault_secret`、`skill` 的通用 Resource ACL；
- `/api/permissions` 中 Instance/Project/其他 Resource 权限管理；
- #1498 的 Link access 本地 aggregate、exact-email 流程和 `/p` route；
- 现有 OIDC/session 认证、group claims 解析和 Instance admission（本任务不扩大其范围）。

“没有 UI 调用”不等于可以删除后端兼容路由；对本任务来说，首先要确认 Show Page 运行时不再走 Resource ACL。旧客户端兼容路由、`/api/org/context`、`/api/org/groups`、`/api/resource-policies` 等不属于本任务的 Show Page ACL 运行时链路，除非实际引用审计证明它们是新链路依赖，否则保持独立的兼容清理范围。

## 11. 测试与验收标准

验收应验证不变量，而不是只列几个示例用户。至少覆盖以下不变量。

### 11.1 数据和服务

- 任意 Show Page 的 Organization access 读取和写入都不访问 `resource_access_policies` 的 `show_page` 行。
- Personal 页面永远不能产生可用的 Organization/group policy。
- `scope` 的每个 group 都属于页面所属 Organization，跨组织输入被拒绝。
- 完整 group set 替换具有 CAS 和事务性；冲突不产生部分写入。
- migration 在重复执行后保持同一 policy，且不覆盖已迁移后的用户修改。
- 旧 Link access 字段和 Organization access 字段独立变更，互不改写。

### 11.2 访问判定

- Private 页面只有 page owner 和同 Organization 的 owner/admin 能访问；Instance Owner 身份单独存在时不产生绕过。
- Selected groups 页面只有同 Organization 且 group intersection 非空的成员能访问；不属于 Organization 或没有所选 group 的成员被拒绝。
- Organization members 页面允许同 Organization 的普通 member、owner、admin 访问。
- Organization owner/admin 在三档下都能访问并管理；普通 member 在三档下都不能管理。
- page owner 能访问并管理自己的页面。
- 不同 Organization 的同名/同 email 用户不能通过 email 或 group ID 获得页面访问。
- 没有可信认证 claims 的请求不会被当作 Organization member。
- 仅有 `/p` Limited exact-page entitlement（`show_page_id`）或 `instance_access_source == "show_page_email"` 的请求不能通过 `/show` 准入；`/show` 必须要求真实 instance 准入。
- `/show` 的访问结果不随 Avibe Resource ACL allow/deny 改变。
- `/p` 的访问结果只随 Link access/guest lease 改变，不随 Organization access 改变。

### 11.3 路由和安全

- `/show` HTML、asset、XHR、event、media 等所有受保护入口都使用同一个 ACL decision。
- 页面 ACL 收紧后，后续请求按最新 revision 判定。
- 未认证 HTML 可以走现有登录流程；未认证 asset/XHR 不发起浏览器登录重定向。
- page ID、instance binding、Organization binding 不一致时 fail closed。
- 错误响应不泄漏页面内容、exact email 或 group membership 细节。

### 11.4 UI

- Personal Avibe 看不到 Organization access 区块。
- Organization Avibe 显示两个独立 section；Link access 三档和 Organization access 三档文案、顺序准确。
- Organization access 区块在组织实例上默认折叠，可展开；折叠/展开不影响任何判定或存储。
- Link access 选择 `Fully public` 时，Organization access 区块仍展示（组织实例上），不被隐藏或禁用。
- 邮箱列表与组织群组在 UI 上保持独立，无叠加去重。
- Organization access 的 group selector 仅在 Selected groups 下出现。
- 普通成员看到只读状态，owner/admin/page owner 可编辑。
- UI 不显示 Resource sync/ACK 状态，不请求通用 Resource API。
- EN/ZH key parity、UI build、相关 Vitest/pytest 通过。

## 12. MVP 明确不做

- owner 离开 Organization 后的自动转移、回收、删除或重新分配；
- Organization switching；
- Cloud 端保存每个页面的 guest/group 精确列表（组织级上限策略是「全局总开关」，不是每页名单，二者不同，见 §17）；
- （组织级上限策略：已从「不做」改为「纳入本计划」，作为 §17 的后续 workstream；其依赖 §16 的 L1–L3 独立 ACL 与本地 enforcement 钩子先落地。）
- 把 Show Page ACL 反向应用到 Agent、Vault、Skill 或其他 Resource；
- 为了实现 Show Page ACL 而扩大 OIDC/session 的 instance admission 范围；
- 删除所有通用 Resource ACL 表或通用 Permissions 页面。

## 13. 开发 TODO（按实现顺序）

- [ ] 在本地 SQLite 增加独立的 Show Page ACL policy/group schema 和 migration。
- [ ] 实现 owner/Organization provenance 的一次性迁移与幂等 adoption；对缺失、pending、冲突状态 fail closed。
- [ ] 新增独立 `show_page_access_service`，集中实现 read/apply/CAS、access evaluator 和 management evaluator。
- [ ] 将现有 Show Page Organization access API 从通用 Resource API 切换到 Show Page 专用 API；保留 Link access API 的职责边界。
- [ ] 替换 `/show` middleware、route、asset/event/media guard 中的 Resource ACL 调用。
- [ ] 确认 `/p` route 仍只走 `ShowAccess`/limited guest lease，补充反向回归测试。
- [ ] 重做 Share popover：Personal 隐藏 Organization access；Organization 显示两个独立 section；修正三档文案和顺序；移除 Resource sync 状态。
- [ ] 更新 EN/ZH i18n，并删除 Show Page 专用的 workspace/Resource ACL 错误文案。
- [ ] 全局审计并删除 Show Page 的 Resource descriptor、Cloud intent/apply/ACK、通用 Resource API caller 和运行时 guard；保留其他 Resource ACL。
- [ ] 增加服务、迁移、路由、UI 的不变量测试，运行 Ruff、UI build、`git diff --check` 和相关回归。
- [ ] 在 PR 描述中列出本文件中的行为契约、测试证据层和未覆盖的手工验证。

## 14. 参考代码位置

这些位置用于开发 agent 定位现有链路；实现时以当前分支最新代码为准：

- Link access aggregate：`core/show_pages.py`、`storage/models.py` 的 `show_pages` 与 `show_page_authorized_emails`。
- 当前错误的 Resource 注册/evaluator：`storage/resource_access_service.py`。
- 当前 Show Page route/middleware：`vibe/ui_server.py` 的 `/show` 相关函数。
- 当前 Show Page access API：`vibe/api.py` 的 `get_show_page_access` 和 `vibe/ui_server.py` 的 `/api/show-pages/<session_id>/access*`。
- 当前错误的 UI：`ui/src/components/workbench/ShowPageWorkspaceAccessControl.tsx`、`ui/src/features/permissions/api.ts`、`ui/src/i18n/en.json`、`ui/src/i18n/zh.json`。
- 现有错误扩展设计：`docs/plans/2026-08-18-current-instance-permissions.md`；其中 Workspace/Resource ACL 部分不再是本需求的目标架构。
- Link access 原始设计：`docs/plans/show-access-local-settings.md`；其中 `private|limited|public` 和 `/p` 边界继续有效。

## 15. 交接时必须回答的问题

开发 agent 提交实现前，应在 PR 描述或测试报告中明确回答：

1. `/show` 的每一个入口是否都经过同一个独立 Show Page ACL evaluator？
2. 是否还存在任何 `show_page -> resource_access_service` 的运行时授权调用？若存在，为什么不是授权链路？
3. 现有页面的 owner、Organization、group 迁移来源是什么，迁移失败如何 fail closed？
4. Organization owner/admin 的“自动访问”是否同时覆盖“自动管理”？
5. Instance Owner 但非 Organization owner/admin/page owner 时，Private 页面是否仍被拒绝？
6. `/p` Link access 和 `/show` Organization access 是否能独立修改、独立测试、互不授予权限？
7. Personal Avibe 是否完全隐藏 Organization access，并拒绝后端伪造的 org/scope policy？
8. UI 是否已经移除 Resource ACK/sync 状态以及 workspace 术语？

只有这些问题都有代码或测试证据，才算完成本 MVP。

## 16. 交付 lane 拆分（2026-08-20 拍板后）

单一 repo（avibe-app）。接口契约已在 §4.1/§6/§8.2 冻结，各 lane 直接引用，不再 lane 间协商。

### 契约（contracts before parallelism）

- 数据模型：§6 两表（`show_page_access_policies` / `show_page_access_groups`）。
- 判定语义：§4.1 伪代码 + §2.5 三层准入（instance → org membership → access_level）。
- HTTP API：§8.2 `GET/PUT /api/show-pages/{session_id}/organization-access`，请求/响应字段。
- decision 枚举：§4.1（`allow/unauthenticated/not_admitted/wrong_organization/not_member/group_not_selected/page_not_found/management_forbidden`）。

### Lane 边界与依赖

| Lane | 范围 | 主要文件 | 禁止触碰 | 依赖 |
| --- | --- | --- | --- | --- |
| L1 数据层 | schema + 一次性迁移 + 幂等 adoption（§6/§7） | `storage/models.py`、新 migration、迁移测试 | `vibe/ui_server.py`、`vibe/api.py`、UI | — |
| L2 服务层 | 独立 evaluator + CAS 读写（§4/§8.1） | 新 `storage/show_page_access_service.py` + 测试 | ui_server / api / UI（本 lane 不接运行时） | L1 |
| L3 接入层 | HTTP API + `/show` 全入口 gate + `/p` 不变（§8.2/§8.3/§8.4） | `vibe/api.py`、`vibe/ui_server.py`、`core/show_pages.py` 的 `require_access*` | UI | L1+L2 |
| L4 前端 | Share popover 双 section + i18n + 移除 Resource sync UI（§9） | `ShowPageWorkspaceAccessControl.tsx`、`ShowPageShareControl.tsx`、`ShowPageSharingSettings.tsx`、`ui/src/i18n/*.json` | 所有 Python | 契约冻结即可（与 L3 并行） |
| L5 清理 | 摘除 show_page 的 Resource 注册/intent/ACK/sync 与通用 API caller（§10/§7.3） | `storage/resource_access_service.py`、`vibe/api.py`、`vibe/ui_server.py`、`ui/src/features/permissions/api.ts` | 通用 Resource ACL 的 agent/vault/skill 保留 | L3+L4 |

依赖序：`L1 → L2 → L3 → L5`；`L4` 在契约冻结后与 L3 并行（前端只依赖 §8.2 响应，不依赖后端实现细节）。

L4 与 L5 协调点：`ShowPageWorkspaceAccessControl.tsx` 停止调用 `getResourceAccess({resource_kind:'show_page'})` 属 L4；`ui/src/features/permissions/api.ts` 中 show_page 专用 surface 的摘除属 L5，但仅删 show_page 专用项，保留 users/projects 等 Permissions 共用面。

### 合并序与 gate

- 每 lane 独立分支/PR（从最新 `master`），按 `pr-delivery-loop` 走 Codex bot review；不自行合并，orchestrator/owner 终审。
- 上游 lane 未 merge 前，下游 lane 以契约为准开发，不共享工作拷贝；合流时再 rebase。
- 每个 PR 必须回答 §15 的 8 个问题中本 lane 覆盖的部分，并列出测试证据层。

## 17. 组织级分享上限策略（企业统一策略）— 纳入本计划

状态：已纳入（2026-08-20 拍板）；跨 repo，依赖 §16 的 L1–L3 落地后实施。本节定义模型与待决卡点，实施前须完成 §17.4 的 owner 拍板。

### 17.1 目标与定位

补上 Google Docs 模型的「后半段」：page owner 管自己页面的正向分享名单（§16 已覆盖），企业管理员在 Cloud 设**组织级统一总开关**，只收紧、永不替 owner 加人。

与 #1498 原始约定一致，也与此前错误模型划清界限：

- 这是**全局上限策略**，不是每页一条 Resource policy；不把 show_page 塞回 `resource_access_policies`。
- 管理员策略**只收紧**本地选择，不得远程添加 guest/群组，不得要求上传每页 exact-email 列表。
- 与 #1516 曾出现的「Cloud 拥有每页策略 + ACK/revision 同步」**不是一回事**——那是本计划 §16 L5 要删除的错误模型。

### 17.2 双权威模型

```text
effective access = 本地 owner 设置（正向授权） AND 组织级上限策略（负向收紧）
```

- **正向授权**：本地 `ShowAccess` + 新 `show_page_access_policies`（本 MVP，§16）。Cloud 不可达不得阻断 owner 的本地写入（#1498 既定原则）。
- **负向收紧**：Cloud 权威。以投影快照形式落地本地，不成为每次访问的实时依赖（否则违反 local-first）。

四类开关（来自 #1498）：

| 开关 | 语义 | 强制点 |
| --- | --- | --- |
| 禁用 Fully public | 组织页面不得选 public | 变更时 + 读取/准入时 |
| 外部邮箱域名禁用 | Limited 名单只允许公司域/白名单域 | 变更时 |
| trusted domains 白名单 | 仅允许配置域 | 变更时 + 读取/准入时 |
| 紧急关闭分享 | 立即停服某实例/组织所有 `/p` | 读取/准入时（kill switch）|

### 17.3 落地形态（复用 #1516 投影模式）

- Cloud 提供组织级分享策略读取端点（avibe-backend）；实例用配对 credential 拉取并持久化本地投影快照，绑定 exact instance/Organization（沿用 #1516 的 offline last-known 模式，凭据不缓存）。
- 本地 enforcement 只消费投影快照，不做每请求 Cloud round-trip。
- 强制的两个点：**变更时约束**（owner 选 public/加外部邮箱被拒）与**读取/准入时收紧**（kill switch / 已公开页面停服）。

### 17.4 待决卡点（实施前必须 owner 拍板，含推荐）

1. **Cloud 不可达时的 fail 方向（最关键）**：投影快照缺失/过期时，负向收紧按哪个方向兜底？
   - (a) **fail-closed**：未知策略 = 最严格（禁 public、禁外部域、禁分享）。安全但首次配对离线时页面无法公开，体感偏严。
   - (b) **fail-open to local**：未知策略 = 无上限，仅按本地设置。体验好，但管理员无法保证收紧在离线时仍生效，已公开页面在离线窗口可能不可撤回。
   - **推荐 (a)**：负向策略的兜底方向天然应趋向「最严格」，且「曾公开即不可撤回」是不可逆风险。
2. **策略粒度**：组织级 vs 实例级 vs 组织级+实例覆盖。推荐：组织级默认 + 实例级可选覆盖（覆盖只能更严）。
3. **强制点确认**：「禁用 public」是只在 owner 变更时拒绝，还是也要让已公开页面立即停服？推荐：变更时约束 + kill-switch 类开关做读取时收紧；「禁用 public」默认只约束未来变更（已公开页面由 emergency disable 统一停服），避免一次开关改动触发全组织页面重算。
4. **投影刷新节奏**：拉取时机（配对时 + 定期 + 变更事件）与离线兜底阈值。推荐沿用 #1516 的 SESSION_AUTHORIZATION_REFRESH 节奏，首次配对必须拉取成功才允许 public 变更（配合 17.4.1 的 fail-closed）。

### 17.5 实施拆解（跨 repo，依赖 L3）

- **B1（avibe-backend）**：组织级分享策略模型 + 读写端点 + 配对实例读取端点（admin 只设开关，不碰每页名单）。
- **B2（avibe-app）**：本地投影快照 schema + 拉取/缓存 + exact instance/org 绑定（复用 #1516 模式）。
- **B3（avibe-app）**：变更时约束接入 L2/L3 的 write 路径（public/外部邮箱被拒，返回稳定错误码）。
- **B4（avibe-app）**：读取/准入时收紧接入 §4.1 evaluator 的预留上限输入（kill switch / 已公开页面停服）。
- **B5（UI）**：Share popover 显示「被组织策略收紧」的只读/禁用态与原因（区分「owner 设置了 public 但被组织禁用」）。

依赖序：`L1→L2→L3` 之后 `B2→B3/B4→B5`；`B1` 与 L 线并行（backend 独立）。B3/B4 只接 evaluator 的预留上限输入，不改变 §4.1 的判定顺序。
