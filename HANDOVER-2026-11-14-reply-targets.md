# 群商机「群内回复 / 私信回复」交接（第 15 批）

> 需求：商机详情页的回复功能要区分「群内回复」和「私信回复」。
> 群商机群内回复默认 @ 发送人（可取消，记住用户选择）；
> 对方是好友才能私信（非好友按钮置灰），好友则多一个「私信回复」按钮。

## 1. 功能行为

群商机（`session_username` 以 `@chatroom` 结尾）的回复区现在是：

| 控件 | 说明 |
|---|---|
| `@发送人（张三）` 复选框 | 默认勾选；取消后不再 @。选择存 `localStorage['kefu.reply.atSender']`，下次打开沿用 |
| `@其他群成员（可选）` 下拉 | 原有功能保留，可额外 @ 多人（去重） |
| `群内回复`（主按钮） | 发到群里。手动确认模式下文案是「群内回复（填入输入框）」 |
| `私信回复`（新增） | 发给该条消息的发送人。**对方不是好友时置灰**，悬停显示原因 |
| `跳转微信会话` | 不变（只打开会话，不发消息） |

私信商机（非群）完全不变：发送人就是会话对端，没有「群内/私信」之分。

### 两个必须知道的边界

1. **手动确认模式下 @ 不生效**：微信的 @ 必须输入时从弹层里选人，纯粘贴
   实现不了。选中 @ 后界面会出提示条「手动确认模式只把文字填进输入框，
   @ 不会生效」，并给一个「改为自动发送」按钮；关了手动确认就是真 @。
2. **私信时 @ 与引用一律丢弃**：引用的那条消息在群会话里，私信窗口里不存在。
   后端受理文案会说清楚。

## 2. 后端契约

### `GET /api/opportunities/{id}/reply-targets`

```json
{
  "is_group": true,
  "session_username": "xxx@chatroom",
  "session_name": "加工群",
  "sender_username": "wxid_xxx",
  "sender_name": "张三",
  "sender_is_friend": true,
  "reason": ""
}
```

- `sender_name` 取**当前群昵称**（读群成员表；群里改名后仍能 @ 到人），
  读不到回退库里存的名字；
- `sender_is_friend=false` 时 `reason` 是给用户看的中文原因
  （「对方不是你的微信好友，只能群内回复」/「无法识别该条消息的发送人…」）；
- 微信库不可用时退化为「群内可用、私信不可用」，接口不报错。

### `POST /api/opportunities/{id}/reply`

`ReplyRequest` 新增 `target`（`session` | `sender`，默认 `session`）。

`target=sender` 时后端做三件事：

1. 校验：必须是群商机（否则 400「当前商机不是群聊」）、发送人 wxid 存在
   （否则 400「无法识别该条消息的发送人」）、**发送人是好友**
   （否则 400「不是你的微信好友，不能私信回复」）；
2. 把 `SendTask.session_username/session_name` 换成发送人本人（显示名用
   `display_name_with_corp`，企业微信联系人保持「昵称@企业简称」的搜索口径）；
3. `at` / `quote` 丢弃。

`SendTask` 新增 `target` 字段，只用于日志（`→ 张三（私信）`）、发送结果文案
和落库。

### 落库

`opportunities` 新增 `reply_target TEXT NOT NULL DEFAULT ''`（旧库由
`_migrate_add_columns` 幂等补列，已在真实库副本上验证：74 行数据、迁移后
全部为空串、状态不受影响）。`set_opportunity_replied(opp_id, text, target)`。
详情页据此在「已回复内容」旁标 `私信回复` / `群内回复` 标签——私信只有发送人
看到，群里其他人看不到，这个标签必须显眼。

## 3. 好友判定（为什么不能靠昵称）

`wechatauto/db.py` 新增：

- `get_contact(username)`：单条联系人（`local_type`/`delete_flag`）；
- `is_friend(username)`：口径对齐 `get_contacts(kind="friend")`：
  - `local_type=3`（通讯录好友）→ 可私信；
  - `local_type=1` 且不是群/公众号/小程序/系统号/自己 → 可私信（仅聊天联系人）；
  - `local_type=5/6/7`（企业微信联系人，`@openim`/`@kefu`）→ 可私信；
  - `local_type=0`（非好友交互者）、`delete_flag=1`、群、公众号 → 不可私信。

`WeChatService.is_friend` 包一层：微信库未就绪或读失败一律 `False`
（宁可置灰，也不要给一个点了必失败的按钮）。

### 实机核对（只读，未发任何消息）

真实库里抽了 40 条群商机 + 643 个群的成员：

- `local_type=3` 的群成员 → `is_friend=True`，私信目标解析正确
  （如「南京骏超汽配有限公司」→ `wxid_icvoauwfw7rl21`）；
- `local_type=0` 的群成员（「AA联众汽配」「海盗虾饭」等）→ `False`，
  按钮置灰，原因文案正确；
- 自己（`wxid_hu7azjb674f721`）→ `False`；发送人 wxid 为空的存量商机
  → 提示「无法识别该条消息的发送人，只能群内回复」。

## 4. 改动清单

后端：
- `wechatauto/db.py`：`get_contact` / `is_friend`
- `app/wechat_service.py`：`is_friend`（降级 False）
- `app/schemas.py`：`ReplyRequest.target`、`ReplyTargetOut`、`OpportunityDetailOut.reply_target`
- `app/api/router_opportunities.py`：`GET /{id}/reply-targets`、`_group_sender_name`、`_dm_target`、reply 支持 `target=sender`
- `app/db.py`：DDL + 迁移列 `reply_target`，`set_opportunity_replied(..., target=)`
- `app/sender.py`：`SendTask.target`，日志/文案区分私信

前端：
- `api/types.ts`：`ReplyPayload.target`、`ReplyTarget`、`OpportunityDetail.reply_target`
- `pages/OpportunityDetail.tsx`：@发送人复选（localStorage 记忆）、私信按钮（置灰+原因）、
  已回复内容旁的去向标签、切换商机清空草稿、手动确认 + @ 的提示条

测试：
- `tests/test_api.py::TestReplyTargets`（11 例：去向接口、好友/陌生人、群昵称实时性、
  私信任务字段、非好友 400、非群 400、默认仍是群内、落库标记）
- `tests/test_reply_targets.py`（11 例：`local_type` 判定表 + 服务层降级）

## 5. 验证

- 全量 pytest：见下一条（本批后 1079 例）
- `tsc --noEmit` 0 错误、`npm run build` 成功，产物含「群内回复 / 私信回复 / @发送人」
- 真实库副本迁移通过；真实微信库只读核对 `is_friend` 与 `_dm_target`

## 6. 待办 / 已知边界

- 手动确认模式下 @ 不生效（见 1.1）。要彻底解决需在 `wechatauto/ui/chatbox.py`
  加「插入 @ 提及但不按发送」，UIA 层改动必须实机在**真实群**验证，本次没做。
- 「自己回复自己」的群（发送人是自己）会被判不可私信，符合预期。
- 同一备注名的好友与群名撞车时，`open_chat` 仍可能开错——这是既有风险，
  与本次改动无关（发送前的人工确认就是为它准备的）。

---

## 十六、到期提醒：每天提示 + 当日可关闭（2026-11-14 11:45）

**需求**（用户提出）：授权到期前 7 天开始每天提示剩余天数；用户手动关闭后
当日不再提示。

**现状**：`LicenseNotice` 已有「7 天内到期 / 离线宽限」横幅，但常驻、无法关闭。

**改动**（仅前端 `frontend/src/pages/LicensePanel.tsx`）：

- 横幅加 `×` 关闭按钮（旁边保留「查看」）。关闭后写
  `localStorage['license.notice.dismissed'] = "${当天日期}|${state}:${days_left}"`。
- 隐藏判定是**每次渲染现场比较**：日期翻天、或剩余天数变化（续费/推进）都会
  让记录失配、横幅重新出现——即「当日不再提示，次日恢复」。
- 日期用本地时区手拼（不用 `toISOString()`，那是 UTC，晚上会提前翻日）。
- localStorage 写不进去（隐私模式）时退化为「每次都提示」，宁可多提示。
- 顶栏黄色 Chip（`授权将在…（X 天后）`）不受关闭影响，仍持续可见——横幅是
  主动提醒，Chip 是被动标识，两者互补。
- 后端无需改动：`LicenseClient.status()` 每次轮询都按当天重算 `days_left`。

**验证**：固定时钟模拟 5 个场景全过（当日关→隐 / 次日→显 / 同日续费→显 /
切离线宽限→显 / 次日再关→再隐）；`tsc --noEmit` 0 错误；`npm run build`
成功，产物含新文案。

**已知边界**：关闭记录存在浏览器 localStorage，清浏览器数据会恢复提示
（多提示一次，无害）；到期当天（days_left=0）仍会提示，真正过期后由
`LicenseBlocker` 全屏遮罩接管。

---

## 十七、Bug：后台吊销后客户端「刷新状态」无反应（2026-11-14 11:55）

**现场**：用户在 8799 管理台吊销许可证后，客户端仍显示「已授权」，点设置页
「刷新状态」也不变。

**排查**（全部实测）：
- 8799 服务端正确：`POST /api/v1/license/heartbeat` 返回
  `{"ok":false,"error":{"code":"revoked"}}`（HTTP 200）。
- 客户端逻辑正确：`refresh()` 收到 revoked → 写
  `license.state.json` 的 `revoked_at` → `status()` 锁死为 revoked。
- **真因在前端**：设置页「授权」标签的「刷新状态」按钮只是
  `refetch()`——重新读一遍**本地缓存**的状态（GET /api/license/status），
  根本没连服务器。真正发心搏的是激活框里的「刷新授权」按钮。
  后台周期心搏默认 6 小时一次，所以吊销后客户端最长 6 小时才会自己发现。

**修复**（仅 `frontend/src/pages/LicensePanel.tsx → LicenseSettingsTab`）：
- 「刷新状态」改为 `POST /api/license/refresh`（真实心搏），成功后 refetch
  并用 toast 报结果：正常 → 「已联网确认：授权正常」；revoked/expired 等 →
  「服务器返回：已吊销/…」；网络失败 → 「联网确认失败：…」。
- 按钮等待文案「联网确认中…」。全局 UI（红色 Chip / 全屏遮罩 / 业务接口 403）
  由 5 秒状态轮询自动跟上。

**顺带**：排查期间我用一次真实心搏把本机 8766 的状态刷成了 revoked
（`revoked_at = 2026-11-14 11:51`），用户刷新页面即可看到全屏遮罩。

**验证**：tsc 0 错误、build 成功、产物含「联网确认中」。链路各环节
（服务端 404/revoked 响应、客户端 refresh→revoked 落盘、/api/license/refresh
端到端返回 revoked）均实测通过。
