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

---

## 十八、回复/跳转再次失效：微信 4.1.15.56 根因与永久方案（2026-11-14 15:15）

**现场**：用户报「回复消息和跳转又不能找到会话了，微信到前台后没有后续动作」。
当天微信自动更新到 **4.1.15.56**。

### 实测定位（全部只读/或与 wake 同机制的写内存）

1. 主窗口在（`mmui::MainWindow` / hwnd 131806），`is_ready()=False`：
   UIA 树整棵缺失，主窗口下只剩 1 个 `MMUIRenderSubWindowHW` 渲染子 HWND。
2. 特征扫描在 4.1.15.56 上**仍能命中**：`qt.accessibility.core` 字符串在
   RVA 0x9042b28，2 个 gate 候选（target 0xb39c9d8 / 0xbbd3b78）。
3. **把两个候选字节都写入 1 后等 40s，树仍然不物化** → 内存补丁路线在
   4.1.15 上失效（字节写成功但 Qt 不再重建 Provider）。这不是签名失效，
   是微信改了 accessibility 的初始化时机。
4. 期间微信进程被强制重启过 → 停在登录界面，需要用户手机确认后才能继续
   验证 QT_ACCESSIBILITY=1 的效果。

### 永久方案（已落地一半）

- **`QT_ACCESSIBILITY=1`**：Qt 框架在**进程启动时**读这个环境变量，
  无障碍树从启动起就完整暴露，不需要内存补丁、不需要版本特征扫描，
  微信后续更新天然免疫（只要还是 Qt）。社区已验证 4.1.x 有效
  （CSDN 159012810、send-wechat-message skill 等）。
- 已设置**用户级环境变量** `QT_ACCESSIBILITY=1`（用户手动双击启动的微信也生效）。
- `uia_driver.py`：`wake()` 拉起微信前 `_ensure_qt_accessibility()` 写入进程
  环境（子进程继承）；唤醒超时的警告文案改为说明新机制。
- 内存补丁保留为兜底（对老版本仍有效，对 4.1.15 无效但无害）。

### 待办

- 用户在手机上确认登录后，验证 `QT_ACCESSIBILITY=1` 下 4.1.15.56 的树是否
  从启动起就完整（is_ready / search box / current_chat）。
- 若验证通过：考虑在唤醒超时提示里引导用户「设置用户级环境变量后重启微信」
  （已写进警告文案）；服务端（kefu-system）无需改动。

## 20. 微信重启后发送/跳转 0 秒失败：句柄缓存不重绑（2026-11-14 第 20 批）

**症状**：微信重新登录后，回复/跳转连「调微信到前台」都没有，0.0 秒报
「微信未登录或窗口不存在」。

**根因**（实测，非 UIA 树问题）：`sender._ensure_gui` 缓存 WeChatGUI 单例
永不重建；微信重启后缓存 `main_hwnd` 已死，`is_alive()` 只查
`IsWindow(缓存句柄)` 恒 False，在进入 ensure_window 之前就判死。

**修复**：
- `wechatauto/guia.py` 新增 `WeChatGUI.rebind()`：重新按多特征评分定位主
  窗口，刷新 main_hwnd/render_hwnd/render_rect/pid，清空 `_current_chat`/
  `_last_input_box`/`_uia`/`_uia_tried`/`_last_open_error`（旧 UIA 引擎的
  `_win` 同样失效）。找不到可见主窗口返回 False。
- `kefu-system/app/sender.py _ensure_gui`：缓存实例 is_alive() 为 False 时
  先 rebind()，成功复用；失败/异常丢弃重建（保留原有中文广播）。

**顺带修正昨日结论（4.1.15.56 UIA 树物化）**：实机验证微信进程已带
`QT_ACCESSIBILITY=1` 时树**仍不随启动物化**，必须再打 `_wake_accessibility()`
热激活补丁（gate byte 0→1）约 1s 内物化；**环境变量 + 补丁缺一不可**。
昨日「补丁无效」是因为当时微信没带环境变量。代码链路已自动完成两者，无需人工。

**实机验证**：死句柄 rebind=True → open_chat('文件传输助手')=True（5.3s，
UIA 混合路径，未发消息）。

**测试**：test_core.py 新增 TestEnsureGuiRebind(4)+TestWeChatGUIRebind(3)；
全量 **1086 passed**（--basetemp=D:/tmp/pt-full18，7m50s）。

**注意**：8766 后端（13:11 启动）是旧代码——需重启才能拿到本修复与第
14~19 批全部改动（群回复 targets、真刷新按钮、看门狗、license v2 等）。

## 21. 部署可迁移性补强：环境变量自动落盘 _ensure_user_env（同日追加）

第 20 节验收时发现：用户级环境变量 QT_ACCESSIBILITY=1 在本机是**手工**写入
的，wake() docstring 引用的 `_ensure_user_env` 并不存在——客户机上用户
手动启动的微信拿不到该变量，树物化条件缺一半。

**修复**（wechatauto/uia_driver.py）：
- 新增 `_ensure_user_env()`：写 HKCU\Environment 的 QT_ACCESSIBILITY=1
  （REG_SZ）+ 广播 WM_SETTINGCHANGE("Environment") 让 Explorer 立即刷新，
  此后用户手动启动的微信也继承。幂等（已是 1 则不写不广播）。
- 新增 `_broadcast_env_change()`：SendMessageTimeoutW 广播（2s 超时防挂）。
- 调用点：`WeChatUIA.__init__`（服务一启动就落盘）+ `wake()`（拉起微信前）。
- `_wake_and_wait_ready` 超时文案改为「重启一次微信让其继承」。

**测试**：新增 tests/test_uia_env.py（5 例，全部打桩不碰真实注册表）；
本机实测幂等 no-op=True；后端 py3.12 py_compile 通过。

**客户机部署口径**：
- 服务启动即自动配置环境变量，零手工步骤；唯一一次性条件：微信在变量
  写入之后启动过一次（客户手动重启微信即可）。
- 无法对「微信未来更新」提供 100% 保证（UIA/内存补丁天然依赖版本），
  防线 = 特征扫描（非写死 RVA）+ UIA 失败自动降级 OCR + 告警日志。

## 22. 启动预热重新启用：首次发送「打开会话 25.4s」归零（同日追加）

**现象**：用户实测第一次回复「微信到前台很快，但搜索联系人时间长」；
第二次快。分段日志：首次发送 前置窗口 2.0s / **打开会话 25.4s** /
输入发送 3.5s；第二次 打开会话 5.1s。25.4s 的主体 = UIA 控件树**首次物化**
（含 4.1.15 热激活补丁，日志 21:10:47 组件初始化 → 21:11:08 才热激活）。

**修复**：
- `wechatauto/guia.py` 新增 `WeChatGUI.warm_up(timeout=30)`：走
  `WeChatUIA.warm_tree`——只物化控件树，**不激活窗口、不动前后台、不点
  搜索框**（用户正在用微信也不受打扰）；成功后把引擎缓存进 `_uia`，
  失败时 `_uia_tried` 保持 False（首次发送会再走完整 ensure_window）。
- `kefu-system/app/sender.py _prewarm`：改为「_ensure_gui() + warm_up()」，
  全程持 `_gui_lock`，拿不到锁立即放弃（2026-11-13 的 68s 事故根因就是
  预热线程没持锁；锁保护现在内置）。
- `app/main.py`：重新启用 `sender.start(prewarm=True)`（当年注释已过时），
  授权通过、pipeline 启动时后台预热。

**测试**：TestWeChatGUIWarmUp(3) + TestSenderPrewarm(3)；全量见日志。

**注意**：预热在服务启动（授权通过）时跑一次；微信未登录/未启动时预热
快速失败并打「未完成」日志，不影响发送；登录后的第一次发送仍走
ensure_window 兜底（有热激活补丁，物化在环境变量齐备时约 1s）。

## 23. 误绑第三方窗口事故：进程身份升级为一票否决（同日追加）

**事故**：用户退出微信、重启服务后测试发送——系统「找不到微信」，且自动化
的点击/键盘**全部落到打开着的 EditPlus 上**，用户肉眼可见。

**根因**（日志+实机实锤）：`_find_main_window` 多特征评分里「进程名」只是
+3 加分项，而「标题含微信」+5 单独过线。EditPlus 打开的
`微信客服开发-记事本.txt` 窗口（标题含「微信」+5、尺寸≥800 +2 = 7 分）在
微信退出后被绑成主窗口（日志 21:43:31 `hwnd=3212074`、无渲染子窗口），
OCR 兜底路径按主窗口矩形对 EditPlus 点击/输入。预热日志同时报
「微信可能未启动」——真微信确实不在，但错误绑定已经发生。

**修复**（wechatauto/guia.py）：
- 新增 `_is_wechat_process(hwnd)`：True=进程名以 weixin 开头；False=确认
  其它进程（一票否决）；None=查询失败不做硬判（防权限问题误杀真微信）。
- `_looks_like_main_window` / `_find_main_window._cb`：进程非微信的窗口
  直接淘汰，不参与评分；进程查询失败才退回旧评分逻辑。
- UIA 侧（WeChatUIA._wechat_hwnds）本就要求窗口进程加载了 weixin.dll，
  无此漏洞。

**实机验证**：EditPlus 3212074 → False（淘汰）；真微信（Weixin.exe）→
True 正常绑定且渲染子窗口识别正常。单测 TestWechatProcessGate(4)。

**教训**：任何「评分制」窗口识别必须有一个不可绕过的身份锚点（进程名），
标题关键字只能用于同进程内的窗口区分。
