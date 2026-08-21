# Show Page 分享与访问体系（单轴模型）

状态：产品决策已拍板（2026-08-20，见 §2）；实现交接文档。**取代此前「两轴（Link/Organization access 分离）」草稿**——该草稿未落地实现即作废。

## 1. 最终结论

Show Page 只有**一条分享轴**，只控制 `/p` 的只读访问：

| 模式 | 谁能通过 `/p` 只读查看 |
| --- | --- |
| `private`（默认） | 无人（无分享链接） |
| `limited` | 白名单命中者：邮箱 / 组织分组 / 「本组织」开关，**全部只读** |
| `public` | 任何人（匿名只读） |

- **`/show` 工作台 = instance 角色**（owner/editor/viewer 都能进），与分享名单完全无关。
- **删除**「Organization access」独立区块，**删除** Resource ACL 里的 `show_page`（注册/evaluator/Cloud sync/ACK/其 `/show` gate/其 API/其 UI）。
- **org 的 show page 默认 `private`**（默认没有分享链接，不是默认 editor 打不开工作台）。
- 能力边界：`/p` 永远只读（无 HMR、无注释、无 Agent）；`/show` 工作台由 instance 角色决定。

## 2. 已确认的产品规则

### 2.1 有限访问白名单 = 异构条目

`limited` 白名单是一个**异构条目集合**，每类条目都是「只读查看」授权，三类平级：

| 条目 kind | 值 | 语义 |
| --- | --- | --- |
| `email` | 规范化邮箱 | 该邮箱可读 |
| `group` | 组织分组 ID | 该分组成员可读（须属本组织）|
| `organization` | 页面的 organization_id | 本组织成员均可读（「本组织」开关，至多一条）|

- 三类条目之间是 **OR** 关系（命中任意一类即通过）。
- 不再有独立的「组织内访问」三档区块；「指定群组」= group 条目，「组织成员」= organization 条目。
- **Personal Avibe 无组织**：只能有 `email` 条目；group/organization 选项在 UI 隐藏、后端拒绝。

### 2.2 能力面（拍板选 A）

- 白名单里的**所有人**（邮箱/分组/本组织）通过 `/p` 进来都是**只读查看**，不获得 HMR、注释、Agent、工作台。
- `/show` 工作台只由 instance 角色决定（owner/editor/viewer），与分享名单无关。instance 用户打开 `/p` 链接仍按现有行为重定向到 `/show` 工作台。
- 一个人可以同时是「外部客人」和「instance 用户」；二者能力独立，分享名单永远不授予或升级 instance 角色。

### 2.3 输入框交互（combobox）

`limited` 白名单的输入框：

- 点击/聚焦自动下拉；可搜索组织内邮箱 + 组织分组，直接选择；可半输入搜索后选择；也可任意填写邮箱。
- 顶部一个「本组织」开关（勾选即本组织成员可读），与白名单齐平。
- Personal Avibe 不显示「本组织」开关和分组搜索，只有邮箱输入。

### 2.4 默认值

- org 与 Personal 的 show page 默认 `private`（无分享链接）。
- `/show` 工作台可进入性不随 `private` 改变（仍由 instance 角色决定）。

## 3. 授权判定契约

### 3.1 `/p` 准入（分享轴）

```text
admit_p(page, visitor_assertion):
    if page.mode == public: allow(anonymous_readonly)
    if page.mode == private: deny(no_share)          # 不提供 /p
    if page.mode == limited:
        if "organization" in entries and visitor.asserts_active_member(page.organization_id):
            allow(readonly)
        if any g in entries(group) and g in visitor.asserts_group_ids:
            allow(readonly)
        if visitor.verified_email in entries(email):
            allow(readonly)
        deny
```

- `visitor_assertion` 是 backend 签名、本地校验通过的身份断言（§4）。
- `private` 保留稳定 `share_id`，但路由不可进入（沿用 #1501 既有语义）。

### 3.2 `/show` 准入（instance 角色，与分享无关）

```text
admit_show(page, subject):
    if not subject.has_role("viewer"): deny          # owner/editor/viewer
    if subject.instance_access_source == "show_page_email": deny
    allow(workbench)
```

- 不再调用 `can_use_resource("show_page", ...)`，不读取 Resource ACL。
- `show_page_email` 作为 instance 授权来源必须彻底移除（#1498 既定，非本计划的回归目标但一并确认）。

### 3.3 能力

- `/p` 所有受众：只读，无 HMR / 注释 / 事件写 / 媒体写 / Agent / 工作台。
- `/show`：按 instance 角色（viewer 只读、editor/owner 可协作/HMR/注释）。

## 4. Backend 依赖（lane B）— 身份断言扩展

当前 show-identity 断言（`vibe/show_identity.py` 校验的 JWT）只含 `verified_email`、`sub`、`instance_id`、`nonce`、`jti`、`iat`、`exp`。要支持 group/organization 条目，backend 必须在断言中**新增（可选）组织块**：

```text
claims（新增，仅当签名用户是某组织成员时存在）:
  organization_id: str
  organization_member_id: str
  organization_role: owner|admin|member
  group_ids: [str]
```

- backend 仍**不知道**页面白名单，只证明「这个人是谁 + 是不是某组织成员 + 属于哪些分组」。
- 本地校验：签名、audience、nonce、instance_id、TTL 不变；新增字段为可选块，缺失时 group/organization 条目一律 fail closed（视为不命中）。
- 断言不得携带页面授权、页面 revision、或从页面成员派生的 instance 角色。

## 5. 数据模型

扩展本地 `ShowAccess` 聚合（#1501），`private|limited|public` + 稳定 share_id + CAS revision 不变，白名单从「纯邮箱」扩为「异构条目」：

```text
show_page_access_entries
  page_id       FK -> show_pages.session_id (ondelete cascade)
  kind          email | group | organization
  value         str  # email 存规范化邮箱；group 存 group_id；organization 存 organization_id
  organization_id  nullable  # 仅 group/organization 条目：页面所属 organization_id
  created_at
  唯一约束: (page_id, kind, value)
  约束: organization 条目至多一条
  约束: group/organization 条目只在 org 页面存在；Personal 无
```

- 迁移把现有 `show_page_authorized_emails` 的邮箱行搬进 `kind=email` 条目（幂等）。
- `scope` 相关旧 Resource ACL show_page 行**不搬运**（那是错误模型，直接由 §7 清理）。
- CAS 原子性、完整条目集替换、失败不部分写入的语义与 #1501 保持一致。

## 6. `/show` gate 与 Resource ACL show_page 清理

- 从 `storage/resource_access_service.py` 的 `RESOURCE_KINDS` 与 evaluator 移除 `show_page`；停止发布 show_page resource descriptor、停止 Cloud intent/apply/ACK。
- `vibe/ui_server.py` 的 `/show` route/middleware/HMR 最终授权改为 §3.2 的 instance 角色判定。
- `vibe/api.py` 移除 show_page Resource access 读写端点（`/api/permissions/resources/show_page/...`）。
- 前端移除 `ShowPageWorkspaceAccessControl.tsx` 与 show_page Resource API client；移除 Resource sync/ACK 文案。

## 7. lane 拆分

| Lane | repo | 范围 | 依赖 |
| --- | --- | --- | --- |
| B | avibe-backend | show-identity 断言新增组织块（§4）| — |
| A1 | avibe-app | 数据/聚合：异构条目 + 迁移 + CAS（§5）| — |
| A3 | avibe-app | UI：单分享区块 + combobox + 本组织开关 + 移除 org access 组件（§2.3/§6）| — |
| A2 | avibe-app | `/p` 准入：email∨org∨group + 本地断言校验扩展（§3.1/§4）| A1 + B |
| A4 | avibe-app | 清理：删 Resource ACL show_page + `/show` gate → instance 角色（§6）| A2 |

依赖序：**阶段 1（并行）B、A1、A3** → **阶段 2 A2**（需 A1+B）→ **阶段 3 A4**（需 A2，避免与 A2 同时改 `vibe/ui_server.py`）。

## 8. 明确不做 / 未来

- **组织级分享上限策略**（禁止 public、trusted domains、紧急关闭）—— 保留为未来 workstream（§17 旧的负向收紧概念仍适用，映射到这条单轴：`effective = 本地 owner 设置 AND 组织上限`）。本次不做。
- owner 离开组织后的转移/回收；Organization switching。
- 不把分享名单反向应用到 Agent/Vault/Skill 等 Resource。

## 9. 验收不变量

- org 与 Personal 默认 `private`，无分享链接。
- limited 白名单三类条目 OR 判定；邮箱/分组/本组织命中即只读进入 `/p`。
- 分组条目跨组织被拒；Personal 无 group/organization 条目。
- 白名单所有人只读：无 HMR/注释/Agent。
- `/show` 可进入性只由 instance 角色决定，与分享名单、与 Resource ACL 无关。
- 断言缺失组织块时 group/organization 条目 fail closed。
- Resource ACL 不再处理 show_page；`/show` 不读取 `resource_access_policies`。
- `/p` 与 `/show` 互不授予能力。
