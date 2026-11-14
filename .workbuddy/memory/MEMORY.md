# 项目长期记忆 · wechatauto-replica / kefu-system

## 项目
- `kefu-system/`：微信客服/商机监测系统，FastAPI + 原生 sqlite3 + React18/TS/MUI5，Windows。
- `wechatauto/`：微信 4.x UIA/数据库驱动（uiautomation + OCR + .dat 解密）。
- 用户偏好：简体中文；重视真实数据/实机验证，不接受只凭推断的结论。

## 当前交接入口
- **2026-11-13 第 13 批：`D:/Git/wechatauto-replica/HANDOVER-2026-11-13-batch13.md`**（7 项全完成，未提交）；前序 `HANDOVER-2026-11-13.md`（第 8~12 批，已提交）。
- 基线 `HEAD=89fd98a`；测试基线 **1115 passed**（2026-11-14 第 23 节后，全量约 10 分钟）。

## 铁律 / 常踩的坑
- pytest 必须 `--basetemp=D:/tmp/...`（本机 safe-delete 守卫会拦临时目录清理）；全量约 4~5 分钟。
- UIA 实机测试只允许「文件传输助手」，**禁止向真实联系人/群发消息**。
- 微信只读探查用 `WeChatDB(workdir=r'D:\wechatauto_cache')`；真实微信库在 E 盘 `E:\xwechat_files\...`，`auto_detect_db_dir()` 返回的 `E:xwechat_files` 缺反斜杠，必须 `os.path.abspath()`。
- 时间口径一律交给 SQLite `datetime('now','localtime',...)`；Python 拼 `T` 分隔时间字符串会因 TEXT memcmp 误删整日数据。
- `voice_texts` / `ai_usage` / `voicecache` 不参与界面「立即清理」（`_PURGE_TABLES` 只有 analysis_logs + opportunities）。
- `kefu-system/data/` 已 gitignore（ASR 模型 229MB、语音/图片缓存）；`tools/silk_v3_decoder.exe` 已被 git 跟踪。

## 领域结论（已实测，别再推翻）
- **「回复时好时坏 / 界面卡死」先查窗口状态与热激活配额，别查会话名、网络、AI**（2026-11-14 23:39~23:51 实测）：①最小化窗口 `IsWindowVisible` 仍 True，但 `GetWindowRect` 是 `(-32000,-32000,-31840,-31972)` 即 160×28 伪矩形；被绑成 `main_hwnd` 后布局/输入框/侧栏全错（日志「校准=1108 vs 当前=280」），3 轮重试耗 60~80s。②`_hot_activate_accessibility` 曾把 gate 已为 1（什么都没写）也返回 True 并计入限流，日志只见 3 次真实 0→1 却报「已热激活 5 次」→ 300s 内强制 OCR 兜底；配额重置后立刻流畅，这就是「时好时坏」的时钟。③sender 每次发送两次 `bring_to_front` 翻转 Z 序 + rebind 清 `_uia` 触发预热巡检再打补丁，形成自激循环；补丁写太频会把 Qt 输入系统写挂（界面卡死）。现已修：guia 淘汰 IsIconic/伪矩形 + `_locate_main_window` 自动恢复最小化窗、ensure_visible 缓存校验「微信仍在前台」；uia_driver 三态只计真实写入 + 按 PID 去重 + 等待期间每 6s 重试；sender 置前统一交给 ensure_visible。排查入口 `kefu-system/wechatauto_logs/app_YYYYMMDD.log` 的「热激活」「重绑 hwnd=」「自动最小化遮挡窗口」。
- 微信语音**异步落库**：消息行先到、`media_*.db VoiceInfo.voice_data` 后到；转写失败须进 `voice_retry.py` 退避队列（5/15/45/120/300s），失败直接丢弃会漏商机。
- 微信 4.x 本地库**不存群名片**；`contact.nick_name` 是全局昵称。群昵称只能从 session.db `last_sender_display_name` 增量学习（`group_cards` 表 + 最后发送者守卫）。
- 微信 4.x 输入框 Accessibility Name 可能拼上 UI 提示语（如「文件传输助手长按鼠标 语音输入」）；会话校验必须用 `same_chat` 宽松匹配，严格相等会误判失败并落入 OCR 兜底（曾导致发送全链路 56.6s）。
- 企业微信联系人 `local_type=5/6/7`、username 形如 `xxx@openim`；企业简称在 `contact.db` 的 `openim_wording` 表。
- .dat 图片 V2 格式：`[6B magic][4B aes_size][4B xor_size][1B pad][AES 密文][raw 明文][XOR 密文]`；key 由 cfg_dword + wxid 派生（详见 media.py）。
- **图片「未就绪」有两个原因，别只归咎微信**：①原图 `<md5>.dat` 确实按需下载（点开过/后台下过才有；`_h.dat` = "查看原图"高清版）；②**缩略图 `_t.dat` 本身就是完整小 JPEG**，多数图不点开也能识别（实测某群 62 张：42 张靠缩略图、1 张靠 `_h.dat` 成功，18 张报未就绪）。这 18 张真因不是没下载完，而是**单字节 XOR 密钥按账号固定（本机 0xF9），逐文件尾字节反推在"尾部带填充"的文件上会推出假密钥**（40 张样本 35 对 5 错）→ JPEG 头正常、之后全乱码 → 误判半截/花屏。已在 `media.py` 加第二轮：账号密钥投票+实测兜底，实测 18/18 救回。
- **企业微信显示名不能直接当搜索词**：「昵称@企业简称」是展示名，`_resolve_search_keyword` 只剥 `@openim` 真实 ID。发消息必须 `_chat_name()` 剥后缀 + 打开后验标题栏企业名标签，不一致拒发（否则会发给同名普通好友，曾实操发错）。
- **「给自己发消息」是普通会话，username = 本账号 wxid**（2026-11-13 实测，本机 `wxid_hu7azjb674f721`），**不是 filehelper**；消息正常落库、`get_new_messages` 能读。但系统只轮询 `monitor_targets` → 不在监控列表就**一条日志都不会有**（用户曾因此以为抓取坏了）。现已标 `is_self`、self/filehelper 豁免「过滤自己发送的消息」（这两类会话 sender_id 恒为 2，开关默认开启会让通道全废）、加入监控时显示名补「（我自己）」。
- **企业微信同名联系人只能靠「逐个点开验标签」识别**（2026-11-13 实机点开验证）：搜「安佳胤」返回『最常使用/安佳胤』(标签 `@我的车车`=企业微信) 与『联系人/安佳胤』(标签空=普通好友) 两条，两者 `Name` 和 `AutomationId`（`search_item_<昵称>`）**完全相同**，分区里也**没有**「企业微信」字样——`_is_openim_result()` 靠分区/副标题判身份在本机**全部失效**。且旧「最常使用 aid 去重」恰好把企业微信那条删掉。现用 `_collect_results(keep_freq=True)` 保两条 + `_open_matching_openim()` 逐个点开读 `openim_label()`（只切会话不输入）+ `_corp_in_label()` 双向包含。改这块前先实机点开看一眼，别信分区名。
- **标题栏企业名标签 AID 会随微信版本整段搬家**（2026-11-13 实测踩坑）：微信 4.1 起从
  `top_content_h_view...current_chat_openim_name` 搬到
  `content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.openim_name_view.current_chat_openim_name`。
  写死旧路径的后果不是「报错」，而是 `is_openim_chat()` 恒 False → **所有企业微信联系人被当同名好友拒发**。
  现用 `OPENIM_NAME_AIDS` 新旧两套 + `openim_label()` 三态（@企业名/''/None）+ `title_h_view` 子树扫「@ 开头文本」兜底。改微信版本后先跑一次 `openim_label()` 自检。
- **微信 UIA 树会被回收，`_find_main()` 仍能拿到只剩 Qt 外壳的窗口**（`MMUIRenderSubWindowHW`+`QWidget` 两子控件）；此时任何 Edit/Text 查找都超时，表现成「无法定位会话」。判定锚点用 `MainView`（`is_ready()`），首次物化实测 15~30 秒。
- **4.1.15.56 UIA 树物化 = 环境变量 + 补丁缺一不可，但补丁必须限流**（2026-11-14 两次实测修正）：进程带 `QT_ACCESSIBILITY=1` 启动后树**仍不随启动物化**，必须再打 `_wake_accessibility()` 热激活补丁（gate byte 0→1）才在约 1s 内物化；不带环境变量时补丁无效。**90 秒内对同一进程补丁 4+ 次会把 Qt 输入系统写挂**——症状：渲染正常、WM_NULL 正常、CPU 0%，但 SendInput/PostMessage 鼠标全被吞、SetWindowPos 被拒，只能重启微信。已加类级限流（同 PID 冷却 20s、上限 5 次、300s 重置、失败不计数），超限走 OCR 兜底。改这块前先看 TestWakeAccessibilityThrottle。
- **微信重启后 sender 会 0 秒报「微信未登录」**：`WeChatGUI` 缓存 `main_hwnd`，重启后 `is_alive()`（只查 IsWindow）恒 False，原代码 `_ensure_gui` 直接判死。已加 `rebind()`（重新评分定位主窗口+刷新 render/pid+清空会话与 UIA 缓存），`sender._ensure_gui` 死句柄先 rebind 再失败。别再往 `_ensure_gui` 加「is_alive False 就报错」的短路。
- **窗口识别必须有进程硬门槛，评分不能替代身份**（2026-11-14 实测事故）：微信退出后，EditPlus 打开的「微信客服开发-记事本.txt」窗口（标题含「微信」+5、尺寸+2=7 分过线）被绑成微信主窗口，自动化点击/键盘全落到 EditPlus 上，用户肉眼可见。修复：guia `_is_wechat_process`（进程名 startswith('weixin')，查询失败返回 None 不否决防误杀）在 `_looks_like_main_window` 与 `_find_main_window` 一票否决。改窗口识别逻辑时，标题/类名只能用于同进程内区分，进程身份必须先行。
- **商机类型能否命中，八成取决于该类型的 `description`，不是 Prompt 模板**（2026-11-13 实测）：`build_system_prompt` 的总纲是「是否表达明确的业务合作意向（采购/委托/询价/求供）」+ balanced「有标的物+意向动作」+ 原则「闲聊一律 hit=false」+ points 只收标的/数量/价格 + direction 只有 buy/sell——非交易型类型（投诉/售后/求助）在这套范式里**天然落空**。只把旧类型描述扩成「命中特征词清单 + 豁免句（如『没有报价/数量/标的物不影响命中』）+ 字段约定（type/points/direction 怎么填）」，实测 7 条投诉全命中、6 条闲聊全不命中、配件交易仍归「汽车配件」，无需改代码。新加类型别再只写「判断是否有 X」。

- **群商机回复分「群内 / 私信」**（第 15 批）：`ReplyRequest.target`（session|sender）；私信只能发给发送人本人，且必须 `is_friend`（`wechatauto/db.py`：local_type 3=好友、1 非群/公众号/小程序/系统号=仅聊天好友、5-7=企业微信，0=非好友交互者→False，delete_flag=1→False）。前端私信按钮非好友置灰。**手动确认模式下 @ 不生效**（微信 @ 要输入时从弹层选人，`input_only` 只粘贴文字），UI 用提示条+「改为自动发送」兜住；私信回复不许带 at/quote（引用的消息在群会话里）。@ 必须用`list_group_members` 的当前群昵称，商机里存的旧名改名后 @ 不到。
- **授权开关 v2 是 fail-closed**：`license.enabled` 任何取值都需要授权，只有等于厂商测试口令（明文或 SHA256 指纹）才关闭校验；`KEFU_LICENSE_MODE=enforce`（env，`run.py --license` 设它）优先级最高、无视口令。切模式只用 `python run.py --license-dev 口令`（必须现场出示，客户拿到程序也关不掉）/ `--license-on`。口令明文不写进 kefu-system/ 任何文件，代码里只有 `DEV_TOKEN_SHA256`。别再把 `0/false/空` 当关闭开关。
- **授权（license）机器码不能用 WMI**（2026-11-13 实测）：本机 WMI 仓库已损坏（`Win32_BaseBoard` 报「无效类」），而 WMI 被系统更新打坏并不罕见——一旦 WMI 参与机器码哈希，坏一次全体客户授权同时失效。现用注册表 `MachineGuid` + Win32 `GetVolumeInformationW` 卷序列号（<10ms、不落盘缓存），WMI 只作排障展示。许可证用 Ed25519 签名（私钥只在服务端，客户端内置公钥表 `PUBLIC_KEYS`），离线宽限 7 天靠 `last_ok_at`，防时钟回拨靠 `max_seen_ts`。接口契约见 `kefu-system/docs/授权服务接口文档.md`，模拟服务器 `tools/mock_license_server.py`（管理台 /admin）。

- **不要并发操作微信 UIA；启动预热必须保留**（2026-11-13/14 实测）：微信 UIA provider 单线程，任何后台 UIA 线程都要持 `_gui_lock`。2026-11-13 的 68s 事故根因是预热线程没持锁；2026-11-14 已重新启用启动预热（main.py `sender.start(prewarm=True)`），`_prewarm` = `_ensure_gui()` + `WeChatGUI.warm_up()`（走 warm_tree，不激活窗口、不点搜索）全程持锁、抢不到即弃。UIA 树首次物化 15~30s 必须在启动时做掉——实测首次发送「打开会话 25.4s」的主体就是它，不要因「怕并发」再关预热。

## 历史批次（已提交，勿重做）
- `c6045c5` AI 账单/清理修复；`036c17e` 企业微信好友搜索；`633f27f` 离线语音转写；`87a6b91` 语音补转写队列 + 发送链路性能修复。
- 详见 `.workbuddy/memory/2026-11-10.md`、`2026-11-11.md`。
- **Ctrl+C 卡在 "Shutting down" 的两个默认值**（2026-11-13 实测）：① uvicorn 的 `timeout_graceful_shutdown` 默认 None = 无限等未完成请求，且在 `lifespan.shutdown()` **之前**——一个卡住的同步请求就让停监听/刷 watermark/关库全不执行，微信轮询照旧（控制台还在刷 poll）；② 解释器退出还要等 **non-daemon 的 AnyIO 线程池 worker**，卡在 `threading._shutdown()`。`run.py` 已修：graceful=5s + `_KefuServer` 挂 20s 看门狗（抓全线程栈→`data/shutdown-dump-*.log`→`os._exit(130)`）+ 收尾 `os._exit`。别再用 `uvicorn.run()` 裸跑。
