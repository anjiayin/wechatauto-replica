# 第 13 批交接（2026-11-13）

> 本文是 `HANDOVER-2026-11-13.md`（第 8~12 批）之后的增量交接。
> 基线 `HEAD=89fd98a`（第 8~12 批已提交、工作区干净），测试基线 **891 passed**。
> 本批 7 项需求**全部完成**，改动 **未提交**。

## 一、需求与结论

### 1. 商机列表操作列换行 → 「来源（会话 / 发送人）」合并列

- 现象：批次 11 新增「发送人」独立列后表格变宽，操作列三个按钮换行。
- 做法（对齐分析日志的「来源会话」列）：删除独立「发送人」列，`SourceCell`
  一列显示：
  - 群消息：两行 —— 第一行群名，第二行发送人昵称（不写 wxid）；
  - 私聊：一行微信昵称；
  - 悬浮 tooltip 显示 `sender_wxid` / `session_username` 等详情，并带复制按钮。
- 表头 `来源（会话 / 发送人）`；`colSpan` 10→9；操作列加 `whiteSpace:'nowrap'`。
- 跳转微信用 `session_display || session_name`（显示名可能是「昵称@企业名」，
  发送链路已能剥离后缀，见需求 3）。

### 2. 企业微信用户显示「昵称@企业简称」

- 根因：`contact.nick_name` 是**全局昵称**，不含企业信息；企业简称在
  `contact.db` 的 `openim_wording` 表（`local_type=5/6/7` 推断企业微信身份）。
- 做法：`wechat_service.openim_corp_map()` 整表缓存（TTL 600s，失败走 30s 短
  缓存避免频繁打库），`display_name_with_corp(username)` 拼「昵称@企业简称」。
  所有对用户可见的会话名统一走它：监听回调 `session_name`、图片任务
  `session_name`、两处 `_aggregator.add`、`build_log_displays.session_display`、
  商机列表/详情的 `session_display`/`sender_display`。
- 降级：取不到企业名时原样返回昵称，绝不显示 `xxx@openim` 这种 ID。

### 3. 回复「安佳胤@我的车车」发到了同名普通用户（**本批最严重**）

- 根因链：
  1. 显示名变成「安佳胤@我的车车」，`sender.open_chat()` 直接拿它当搜索词；
     `uia_driver._resolve_search_keyword` 只剥离 `@openim` 结尾的**真实 ID**，
     不管这种「昵称@企业名」的**展示名**，所以搜索必然搜不到；
  2. 搜不到后侧栏/搜索兜底把同名会话「安佳胤」点开；
  3. 因为没有「打开的是不是同一个人」的校验，消息发给了同名的普通好友；
  4. 还白等 8 秒数据库确认，合计 49.5s（组件 4.0 · 前置窗口 1.8 · 打开会话
     28.2 · 输入发送 15.6）。
- 修复（三层，逐层独立生效）：
  - **搜索词剥离后缀**：`sender._chat_name()` 去掉 `@企业名`，只用人名搜；
  - **身份消歧优先**：企业微信会话走 `guia.open_chat_prefer_openim(name, corp)`
    → `uia.open_chat(prefer_openim=True)`，优先挑搜索结果里属于「企业微信联系人/
    企业微信」分区、或副标题含企业名的条目；
  - **打开后验身份**：聊天标题栏的企业名标签控件
    `...big_title_line_h_view.current_chat_openim_name`（`OPENIM_NAME_AID`）
    存在才算对上；不一致 **直接拒发**，报文写「该联系人是企业微信身份，若微信里
    存在同名普通好友，可能被误开；已取消发送以免发错人」。
  - 兜底：需求 5 的默认人工确认。
- `jump_to_session` 同样剥离后缀并走消歧。

### 4. 设置页修改启动端口

- 新增 `GET/PUT /api/settings/server`（`ServerSettingsOut{host, port,
  running_host, running_port, restart_required, default_port, address}`），读写
  settings 表的 `run.host`/`run.port`。`_running_address(request)` 从 ASGI
  `scope["server"]` 取**真实监听端口**，避免把「已保存值」当成「正在跑的值」。
- 前端「数据与计费」页新增「启动端口」区块：输入框 + 保存 + 恢复默认 +
  当前监听/保存值 + 「需重启生效」Alert。保存后 `restart_required` 如实反映。
- 越界（<1 或 >65535）返回 422。与批次 12 的 `run.py --port/--save` 不冲突：
  优先级仍是 **命令行 > 环境变量 > 已保存配置 > 默认 8765**。

### 5. 商机详情「发送回复」默认人工确认

- `ReplyRequest.manual_confirm` 默认 **True**；`SendTask.manual_confirm` 同。
- 人工确认 = `sender._fill_only()`：只把文字粘贴到输入框（`guia.input_only`），
  **不按 Enter、不写 replied、不置状态**，广播 `send_result{ok, manual: true}`。
- 为什么必须这样：无 UIA 时 OCR 兜底路径会顺手回车，所以 `input_only` 无 UIA
  直接明确失败，绝不落到 OCR 分支。
- 人工确认模式下丢弃 `at`/`quote`（@ 和引用只能靠键盘动作，与「只填不发」冲突），
  文案里告知用户。
- 前端：详情页 `Switch` 默认开，按钮文案随模式切换（「填入微信输入框」/
  「发送回复」，进行中「填入中…」/「发送中…」）。

### 6. 商机详情页「跳转微信会话」

- 详情页操作行新增「跳转微信会话」按钮（原右上角图标保留，两个都留）。
- 后端复用 `jump_to_session`（已带企业微信消歧）。

### 7. 「图片未就绪」反复重试 —— 两条原因：原图按需下载 + **XOR 密钥反推失手**

- 用户问题：为什么有些图能显示、有些下载不到？微信什么情况下就绪？
  用户补充的关键事实：**很多从没点开过的图也能正常识别**。
- 实测（只读探查该群 62 张图片消息，未改动微信数据）：

  | 项 | 数量 | 说明 |
  | --- | --- | --- |
  | 只有 `_t.dat` | 57 | 群图常态，**缩略图本身就是完整小 JPEG**（3~18KB，长边常 210） |
  | 有原图 `.dat` | 5 | 其中 4 张是半截（尾部缺 EOI），1 张完整 |
  | 有 `_h.dat`（高清原图） | 1 | `21507`：`.dat` 半截、`_h.dat` 完整 1320x1372 —— 这张正是靠 `_h` 救回的 |
  | 能正常识别 | 43 | **42 张靠缩略图**、1 张靠 `_h.dat` —— 这就是「没点开也能识别」的原因 |
  | 报「未就绪」 | 18 | 全都被判「半截/花屏」 |

- **第一版结论（错）**：以为这 18 张是「缩略图只下载了一半」。逐张核对文件
  size/mtime 后发现不对：mtime 就是消息到达那一刻，之后**再没变过**，
  文件大小也和完整缩略图一个量级 —— 它们并不是「还在下载」。
- **真实原因（实测）**：单字节 XOR 密钥**按账号固定**（本机 0xF9，批次 10 已
  记录），但 `_key_from_tail` 依赖「明文以 `FF D9` 结尾」；尾部带填充/噪声的
  文件会推出**假密钥**（该账号 40 张缩略图里 35 张推出 0xF9，5 张推出
  `0x11/0x2A/0xDB/0xCA`）。用假密钥解密：AES 首块（含 JPEG 头）正常、之后
  熵编码数据全乱码 → `is_complete_image` 判半截、`image_looks_broken` 判花屏。
  拿 0xF9 重解那 18 张：**18/18 全部可用**（210x467 等，不是占位图）。
- **修复**（`wechatauto/media.py`，第二轮兜底）：
  - `_learn_account_xor_key()`：取账号内最新 16 张缩略图各自反推密钥**投票**，
    得票最高者再用 3 张真实文件**实测**（`is_usable_image`）通过才采信；
  - 逐文件反推全部失手时，用账号密钥再解一轮（`_decode_file`）——命中会打
    `账号级 XOR 密钥兜底命中`；
  - 成功缓存 1 小时、失败缓存 60s（探测要扫 attach 目录，实测约 9s，只在
    失败路径触发一次）；显式传 `xor_key` 的调用方**不走**兜底（只认那一把）；
  - 效果：该群「未就绪」从 18 降到 0（实测 18/18 取回）。
- 顺带把「为什么/怎么办」讲清楚（用户可见）：
  - 日志带真实原因（`原图未下载 / 原图与缩略图均不完整 / 解密失败 …`）；
  - `analysis_logs` 新增 `image_local_id`，日志能精确指回是哪张图；
  - 新增「重新识别」按钮（`POST /api/images/retry`）：存量被误判的图重启后
    点一下即可补识别（用户不必再去微信点开）；
  - `ImageRetryQueue.on_give_up`：退避用尽时落一条**终态**日志，不再让用户
    无限等待。
- **仍需知道**：真正的「原图」确实按需下载（`<md5>.dat` 只有点开过/后台下过
  才有，`_h.dat` 是"查看原图"后的高清版）。所以：
  - 缩略图能读出内容时就已经够识别（本机 43/62 走这条）；
  - 只有缩略图都解不出、且没有 `_h.dat`/原图时，才真的需要用户在微信点开。

### 8. 补丁：企业微信又发不出去 + 微信到前台后空转 25 秒（19:0x 实机反馈）

现场：回复「安佳胤@我的车车」提示
「无法定位会话：安佳胤@我的车车（该联系人是企业微信身份…已取消发送以免发错人）
（组件初始化 2.6s · 前置窗口 1.4s · 合计 33.8s）」，但**微信其实已经打开到
正确会话，只是没往输入框写字**；用户还观察到「微信到前台 → 开始往搜索框打字」
中间有很长空档。

日志（`kefu-system/wechatauto_logs/app_20261113.log` 19:01:01~19:01:35）逐段对上：

```
19:01:01 WinInput 初始化                     ← 组件初始化 2.6s
19:01:04 WeChatGUI 初始化成功，最小化遮挡窗 3 个  ← 前置窗口 1.4s
19:01:29 已热激活微信 UIA 树：PID=13188        ← 中间 25 秒在等控件树物化
19:01:30 已启用 UIA 驱动
19:01:35 同名会话身份不符：安佳胤 打开后不是企业微信会话，已放弃  ← 实际是误判
```

两条独立根因：

1. **标题区企业名标签的 AID 失效（致命）**。代码写死微信 4.0 的
   `top_content_h_view.top_spacing_v_view.top_left_info_v_view.big_title_line_h_view.current_chat_openim_name`；
   微信 4.1 起整段搬到
   `content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.openim_name_view.current_chat_openim_name`。
   旧路径 `Exists()` 恒 False → `is_openim_chat()` 永远 False →
   **所有企业微信联系人一律被判「同名普通好友」拒发**。
   本机只读实测（当前会话正是该企业微信联系人）：

   ```
   current_chat() = '安佳胤'
   openim_label() = '@我的车车'     ← 修复后
   is_openim_chat() = True
   ```

2. **`ensure_window()` 看到窗口就返回 True，但控件树可能还没物化**。微信 UIA 树
   被回收后只剩 Qt 外壳（`MMUIRenderSubWindowHW` + `QWidget` 两个子控件），此时
   任何 `EditControl`/`TextControl` 查找都超时 → 搜索框 `None` → 报「无法定位
   会话」。而真正把树等回来的 25 秒压在用户点「发送」的那一刻。

修复：

- `uia_driver.py`：`OPENIM_NAME_AIDS` 存**新旧两套路径**逐个试；新增
  `openim_label() -> '@企业名' / '' / None` 三态（`''` = 明确不是企业微信，
  `None` = 树没物化、不作判断）；AID 再变也不怕——兜底在 `title_h_view`
  子树里找「以 `@` 开头的文本」；新增 `is_ready()`（以 `MainView` 为锚点判断
  控件树是否物化）；`ensure_window(require_ready=True)` 树没好时**显式等待并
  打日志**，不再假成功；新增 `warm_tree()`（只让树就绪，**不激活窗口**）。
- `guia.py`：`open_chat_prefer_openim` 改用 `openim_label()` + 企业简称比对
  （复用已打开的同类会话前也要校验，否则会沿用上一次的错误会话）；新增
  `last_open_error()` 返回 `tree_not_ready / identity_mismatch / corp_mismatch`；
  新增 `warmup()`；顺手修掉 `calibrate_layout` 里 `name 'threading' is not
  defined`（漏 `import threading`，导致布局校准每次都失败）。
- `sender.py`：启动时后台预热控件树（`start(prewarm=True)`，`main.py` 已传），
  用户第一次发送不再干等 25 秒；打开失败按原因给不同文案——树没就绪说
  「微信控件树正在唤醒（约 15~30 秒）」而不是「无法定位会话」，身份不符才说
  「微信里打开的是同名普通好友…可先手动打开该企业微信会话再试」。

### 9. 补丁 2：重启后回复企业微信「只把微信调到前台就没有下文」（19:40 实机反馈）

现场：重启服务后回复企业微信联系人，前端一直停在「正在操作微信窗口（68.0s）」，
微信只被调到前台，搜索框里一个字都没输入，最后也没有任何失败提示。

只读实测（不发任何消息）定位到**两个独立问题**：

#### 9.1 搜索候选被「aid 去重」删掉了企业微信那条（真正的发送失败原因）

实机点开验证（2026-11-13 19:43，只切会话不输入）搜「安佳胤」的真实结果：

| 分区 | 标题栏 `@企业名` 标签 | 实际身份 |
| --- | --- | --- |
| 最常使用 | `@我的车车` | **企业微信（目标）** |
| 联系人 | 空 | 普通好友 |

两条的 `Name`（都是「安佳胤」）与 `AutomationId`（都是 `search_item_安佳胤`）
**完全相同**，分区里也**没有**「企业微信联系人」这种标注。而旧实现有两处会把
企业微信那条丢掉：

1. `_collect_results` 的「最常使用/最近使用 与普通分区 aid 重复则去重」——
   正好把企业微信那条删掉，只剩普通好友；
2. `prefer_openim` 依赖 `_is_openim_result()`（分区名 / 副标题含「企业微信」/
   副标题含企业名），而这两条一个都不满足，等于形同虚设。

结果：点开的一定是普通好友 → `openim_label() == ''` → 判 `identity_mismatch`
→ 拒发。用户看到「发不出去 / 只在前台转」。

**修复**（`uia_driver.py`）：

- `_collect_results(keep_freq=False)`：企业微信场景传 `True`，不走 aid 去重，
  两条候选都留着；
- 新增 `_open_matching_openim(results, corp)`：**逐个点开候选、读标题栏
  `@企业名` 标签**，读到与本企业匹配的那条才算命中（「最常使用」优先，实测
  多半第一条就中）。点开只切会话，**不输入、不发送**，点错也不会误发；
- 新增 `_corp_in_label(label, corp)`：剥掉 `@` 后双向包含匹配，容忍
  「我的车车(北京)」这类后缀差异；
- `open_chat`：`prefer_openim and openim_hint` 时走上面这条路，即使**只有
  1 条候选也要验标签**（1 条恰好是普通好友的情况就是本次 bug）。

**实测**：`open_chat_prefer_openim("安佳胤", "我的车车")` 现在 **0.90s 返回
True**，`current_chat='安佳胤'`、`openim_label='@我的车车'`。

#### 9.2 68 秒无响应：启动预热与发送线程并发抢微信 UIA 线程

日志（`wechatauto_logs/app_20261113.log`）在

```
19:38:31 WinInput 初始化            ← 发送线程创建 WeChatGUI
19:38:38 WeChatGUI 初始化成功
19:38:41 自动最小化遮挡窗口 4 个      ← ensure_visible 完成，之后 58 秒全静默
```

之后再无任何日志（既没有「控件树预热完成」，也没有失败/超时）。原因是
`main.py` 里的 `sender.start(prewarm=True)` 起了第二个后台线程，用**独立的**
`WeChatUIA()` 每 0.4 秒 `_find_main()` + `is_ready()`（即 `ControlFromHandle` +
`Exists`），和发送线程抢同一个微信进程的 UIA provider（微信侧是单线程），
两边互相排队；预热线程自己也卡在 `_find_main()` 里，所以连「预热完成/未完成」
那行都没打出来。

**修复**：

- `main.py` 不再传 `prewarm=True`（默认不预热）。第一次发送时
  `ensure_window(require_ready=True)` 会显式等待并打日志，更可诊断；
- `_prewarm()` 保留但**必须持 `_gui_lock`**（0.2s 拿不到就放弃），预热永远只是
  可选优化，不许影响正常发送；超时从 40s 收到 20s。

**实测**：同一台机器、同一微信状态下，`WeChatGUI()` 构造 + 打开会话合计
< 2s；`open_chat("文件传输助手")` 4.29s（含搜索 + 点击 + 校验）。

### 10. 补丁 3：给自己发消息完全收不到（20:05 实机反馈）

现场：给自己的微信（昵称「安佳胤」）发消息，PC 端和手机端都发了，**分析日志
里一条都没有**。

只读探查（未改动微信数据）拿到结论：

- 微信 4.x 的「给自己发消息」是一个**普通会话**，username 就是当前登录账号的
  wxid（本机 `wxid_hu7azjb674f721`），**不是**文件传输助手；
- 该会话里的消息**确实落库**：`local_id` 1~5（`111` / `我要桶机油` 17:33 /
  `我要机油` 19:58 / 语音 20:00），`get_new_messages(since_seq=…)` 能正常读出；
- 而系统只轮询 `monitor_targets` 里注册过的会话——这个会话从未被加入监控列表，
  所以一个字节都没读过，日志自然为空。

修复（三处，都是「让这条通道可用且认得出」）：

1. 会话候选标记 `is_self`（`_build_contact_list` + `SessionItemOut` + 前端
   Chip「我自己」+ 搜到时的说明），否则它和同名好友在列表里一模一样；
2. `_is_self_chat()`：**自己↔自己**的会话（self 会话 / 文件传输助手）不受
   「过滤自己发送的消息」影响——这两类会话里每条消息 `sender_id` 都是 2，
   开关默认开启会让整条通道失效；别人会话里我发的消息仍按原逻辑过滤；
3. `create_target` 给自己会话的显示名补「（我自己）」（用 `getattr` 探测，
   测试/降级场景的服务替身不受影响）。

实测（只读，未发送任何消息）：
`Listener(watermark={self: 1794562383000}).add_listener(self, cb)` 启动 2 秒内
回收到 `local_id=4`（文本）与 `local_id=5`（语音）两条。

## 二、改动文件

| 层 | 文件 | 要点 |
| --- | --- | --- |
| 底层 | `wechatauto/uia_driver.py` | `OPENIM_NAME_AID`/`OPENIM_SECTIONS`、`open_chat(prefer_openim, openim_hint)`、`_is_openim_result`、`fill_text`、`is_openim_chat` |
| 底层 | `wechatauto/media.py` | 账号级 XOR 密钥学习 + 第二轮兜底（需求 7 续）、`_decode_file` 抽取 |
| 底层 | `wechatauto/guia.py` | `open_chat_prefer_openim(name, corp)`、`input_only(text)`（只填不发，无 UIA 明确失败）、`is_openim_chat` |
| 后端 | `app/sender.py` | `manual_confirm`、`_chat_name`（剥后缀）、`_is_openim`/`_corp_hint`、`_fill_only`、`_do_send`、消歧打开 + 拒发 |
| 后端 | `app/wechat_service.py` | 企业名缓存与 `display_name_with_corp`、`get_image_bytes_with_reason`、`retry_image`、`_on_image_retry_given_up` |
| 后端 | `app/image_retry.py` | `on_give_up` 回调（重试用尽才触发，`abort` 不触发） |
| 后端 | `app/db.py` | `analysis_logs.image_local_id`（DDL + 迁移 + 查询）、`delete_image_analysis` |
| 后端 | `app/api/router_opportunities.py` | 列表/详情补 `session_display`/`sender_display`、`manual_confirm` 默认 True |
| 后端 | `app/api/router_settings.py` | `GET/PUT /api/settings/server` |
| 后端 | `app/api/router_targets.py` | 「给自己发消息」显示名补「（我自己）」（getattr 探测，兼容服务替身） |
| 后端 | `app/api/router_system.py` | `POST /api/images/retry`（异常绝不 500） |
| 后端 | `app/api/deps.py` | `get_wechat_service_optional()`（未注入返回 None） |
| 前端 | `pages/OpportunityList.tsx` | `SourceCell` 合并列 + tooltip + 复制 |
| 前端 | `pages/OpportunityDetail.tsx` | 手动确认开关、双按钮文案、跳转会话 |
| 前端 | `pages/AnalysisLogs.tsx` | 图片行「重新识别」 |
| 前端 | `pages/SettingsPage.tsx` | 启动端口区块；候选列表「我自己」Chip 与说明 |
| 测试 | `tests/test_batch13.py` | 32 例（新增） |
| 测试 | `tests/test_batch13_media_key.py` | 14 例（新增）：账号密钥投票/实测/缓存/兜底 |
| 测试 | `tests/test_batch13_openim_aid.py` | 17 例（新增）：新旧 AID、子树兜底、三态、就绪门禁、预热不抢焦点、消歧与企业名比对 |
| 测试 | `tests/test_batch13b_openim_search.py` | 14 例（新增）：候选不被去重删掉、逐个点开验标签、别的企业拒收、失败原因码 |
| 测试 | `tests/test_batch13c_self_chat.py` | 10 例（新增）：`is_self` 标记、自我会话豁免过滤、加入监控标注 |
| 测试 | `tests/test_api.py` 等 | 契约字段更新；图片打桩改到 `get_image_bytes_with_reason` |

## 三、测试

- `tests/test_batch13.py`：32 例 —— 企业名显示/缓存/失败回退、路由 helper、列表与
  详情展示名、无微信服务降级、企业微信发送消歧（含「同名被拒发」）、人工确认只填
  不发且状态仍 pending、端口 GET/PUT/越界 422/恢复默认、`image_local_id` 落库、
  `retry_image` 各分支、`/api/images/retry` 不 500、退避用尽回调与 `abort` 不回调。
- 契约变更带动 3 个旧文件调整：`tests/test_batch6_media.py`、
  `tests/test_batch7.py` 的打桩目标从 `get_image_bytes` 改为
  `get_image_bytes_with_reason`（返回 `(data, reason)`），两处日志断言补「真实原因」。
- 前端 `npx tsc --noEmit` 0 错误；`npx vite build` 成功（产物 `index-Cdv5Carp.js`）。

## 四、遗留 / 注意事项

- **服务需重启**：当前运行实例（8765）仍是旧代码，端口设置、手动确认、跳转按钮
  都要重启后才生效；前端产物已构建，重启后强刷页面即可。
- 图片「重新识别」按钮只能补「微信已经下到本地」的图；用户必须先在微信里点开原图，
  这是微信侧行为，程序无法代劳（也**不应该**去代劳：模拟点击会动用户微信）。
- 企业微信同名消歧依赖标题栏的 `@企业名` 标签；已做「新旧 AID + 子树扫描」
  两级兜底。若某版本连标题区都读不到，`openim_label()` 返回 `None`，此时
  退化为「按分区优先打开 + 不校验」，由默认人工确认兜底。
- **不做启动预热**（9.2）：两个线程并发查微信 UIA 树实测能把发送拖到 60s+
  且全程无日志。第一次发送若树未就绪，`ensure_window` 会显式等待并打
  「微信控件树尚未就绪，正在热激活」——慢但看得到在干什么。
- **企业微信同名联系人只能靠逐个点开验标签识别**：搜索结果里两个身份的
  `Name`/`AutomationId` 完全一样（`search_item_<昵称>`），分区名也不标
  「企业微信」。因此企业微信发送会依次点开同名候选（每个约 0.6s），
  读到标题栏 `@企业名` 匹配才停——微信窗口会短暂闪动属正常。
- 历史商机无 `raw_refs` 不回显图片（批次 11 遗留）；发送人候选 500 上限；
  `LIKE` 通配符未转义 —— 均未排期。
