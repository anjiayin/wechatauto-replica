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
| 后端 | `app/api/router_system.py` | `POST /api/images/retry`（异常绝不 500） |
| 后端 | `app/api/deps.py` | `get_wechat_service_optional()`（未注入返回 None） |
| 前端 | `pages/OpportunityList.tsx` | `SourceCell` 合并列 + tooltip + 复制 |
| 前端 | `pages/OpportunityDetail.tsx` | 手动确认开关、双按钮文案、跳转会话 |
| 前端 | `pages/AnalysisLogs.tsx` | 图片行「重新识别」 |
| 前端 | `pages/SettingsPage.tsx` | 启动端口区块 |
| 测试 | `tests/test_batch13.py` | 32 例（新增） |
| 测试 | `tests/test_batch13_media_key.py` | 14 例（新增）：账号密钥投票/实测/缓存/兜底 |
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
- 企业微信同名消歧依赖标题栏 `current_chat_openim_name` 控件；若某版本微信改名，
  `is_openim_chat()` 返回 `None`，此时退化为「按分区优先打开 + 不校验」，
  但默认人工确认仍在兜底。
- 历史商机无 `raw_refs` 不回显图片（批次 11 遗留）；发送人候选 500 上限；
  `LIKE` 通配符未转义 —— 均未排期。
