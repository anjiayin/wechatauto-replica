# 项目长期记忆 · wechatauto-replica

## 项目与偏好
- `kefu-system/`：微信客服/商机监测系统，FastAPI + 原生 sqlite3 + React18/TS/MUI5，Windows。
- `wechatauto/`：微信 4.x UIA/数据库驱动（uiautomation + OCR + .dat 解密）。
- 用户：简体中文；重视真实数据/实机验证，不接受只凭推断的结论。
- 交接文件：`.workbuddy/memory/2026-11-15.md`（UIA 第 2~7 轮）、`HANDOVER-*.md`；打包方案 `打包与自动更新技术方案.md`（2026-11-15 定稿，实现未开工）。
- **2026-11-15 19:33 状态**：分支 `main`、**工作区干净（0 改动）**、`HEAD=670c6ce`「再次优化消息发送，托盘化也能正常发送」，UIA 各批与版本号统一均已提交。测试基线 1234 passed。前端改后必须 `cd kefu-system/frontend && npm run build`（dist 被 gitignore）+ 浏览器 Ctrl+F5；后端改后重启 8766。
- 版本唯一真源 `version.txt`（当前 **1.2.5**，2026-11-18 23:33 已发布 OSS，线上 sequence=125）；发版只跑 `python kefu-system/tools/version_sync.py --bump X.Y.Z`，vite 构建期硬校验 + `tests/test_version_consistency.py` 守门，禁止手改派生的 `1.0.0` 之类字面量。发布命令模板/线上地址见 `.workbuddy/memory/2026-11-18.md`（注意线上 sequence 可能比日志记的大，发版前先 `curl .../manifest.json` 读真实 sequence 再 +1；1.2.4 实际发过两次、sequence 到 124）。

## 铁律
- pytest 只用 `/d/my_conda_envs/wechatauto/python.exe -m pytest`（PATH 里的 python 没装 pytest），必须 `--basetemp=D:/tmp/pt-新目录名`（safe-delete 守卫会拦临时目录清理）；全量约 9 分钟。
- UIA 实机测试只允许「文件传输助手」，**禁止向真实联系人/群发消息**。
- 微信只读探查 `WeChatDB(workdir=r'D:\wechatauto_cache')`；`auto_detect_db_dir()` 返回 `E:xwechat_files` 缺反斜杠，必须 `os.path.abspath()`。
- 时间口径交给 SQLite `datetime('now','localtime',...)`；Python 拼 `T` 分隔字符串会因 TEXT memcmp 误删整日数据。
- `voice_texts`/`ai_usage`/`voicecache` 不参与界面「立即清理」（`_PURGE_TABLES` 只有 analysis_logs + opportunities）。
- `kefu-system/data/` 已 gitignore（ASR 模型 229MB、语音/图片缓存）；`tools/silk_v3_decoder.exe` 已被 git 跟踪。

## 发送链路 / 托盘（已实测，别推翻）
- **托盘态唤起只能模拟点击托盘图标让 Qt 自己 show()**（19:05 实测 2.62s）：再启动 `Weixin.exe` 不是唤起（新起 PID 后自行退出）；`SW_SHOW` 已证伪。实现 `wechatauto/tray_restore.py`：精确图标名 + 按矩形面积选 40x40 图标（溢出面板有同名 37x22 文本标签）+ UIA 子节点轮询 2s（固定 sleep 0.6 会扑空）+ 单击 0.8s 无效补双击 + V 形重试阈值 ≥1s + 保存/恢复鼠标并收起面板；绝不 ShowWindow/写内存/起新进程。唤起后必须 `guia._after_tray_restore()` 清 UIA/会话/输入框/句柄缓存；后台预热巡检不碰托盘窗口。pytest 由 `tests/conftest.py` autouse 隔离。
- **托盘 = Qt hide()（IsWindowVisible=False），真最小化 = IsIconic=True 且 visible 仍 True**（`GetWindowRect` 是 -32000 的 160×28 伪矩形）。只 SW_RESTORE 真最小化；托盘态绝不 SW_SHOW/写补丁。Qt accessibility 树只在可见窗口物化。
- **UIA 树物化 = `QT_ACCESSIBILITY=1` + 热激活补丁（gate 0→1）缺一不可**；90s 内同进程补丁 4+ 次会把 Qt 输入写挂（渲染正常、WM_NULL 正常，但鼠标全吞、SetWindowPos 被拒，只能重启微信）。已加类级限流（冷却 20s、上限 5 次、300s 重置、只计真实写入、按 PID 去重）。托盘树被回收但 gate 留在 1 → 普通热激活返回 `already` 干等 30s；改用 `_hot_activate_accessibility(rearm=True)` 受控 `1→0→1` 脉冲（**计 2 次写**）+ 同 PID 45s 限流、窗口不可见立即失败。
- **发送链路 fail-closed，别再加截图/OCR 兜底**（用户明确要求）：`guia.OCR_FALLBACK_ENABLED=False` + `_require_uia()` 总闸，send_msg/quote_msg/at_members/input_only/open_chat* 只走 UIA、失败直接判失败（命令行 demo 需 `WECHATAUTO_OCR_FALLBACK=1`）。托盘恢复后伪矩形会让 OCR 抛 `Coordinate 'right' is less than 'left'` 并白等 20~30s。
- **所有失败路径都必须广播 `send_result`**；异步 202 要「受理先于结果」：后端 `ACK_LEAD_SEC=0.35`（`SendTask.enqueued_mono` + Event.wait）+ 前端 `sendSettledRef` 双保险。响应带 `data.chat_verified`，sender 只在 True 时把「已操作发送 + DB 未确认」按 replied 处理；DB 未确认文案有 6 种前缀，别只匹配一种。
- **恢复后主窗句柄会变**（4000292→1378074）：`_rebind_if_window_changed()` 清缓存 + 每次发送 `_confirm_target_chat()` 实时读 `uia.current_chat()`，企业微信不匹配拒绝二次 open_chat（免得开成同名普通好友）。
- **@+引用**：`SenderThread._do_send` 原是互斥 `if...return` → 只发 @ 却报成功；现走 `at_members(quote=True)`（先建引用卡片、再逐个 @、只回车一次，**引用后绝不能 Ctrl+A/Delete**）。`hasattr(gui,'at_members')` 对 None 返回 True，要用 `callable(getattr(...))`。
- **只有引用失败、纯文本/@ 正常 → 查引用路径而非窗口**：微信 @ 原文是 `@昵称\u20055`（U+2005），`" ".join(text.split())` 折成 U+0020 导致恒不匹配 → `wechatauto/text_norm.py` 双向归一化；`find_in_message_list` 向历史滚最多 12 次且失败不复位 → 面板停在历史、OCR 全失败，必须复位；`_find_row_by_text` 把 rect 当控件传、`_right_click_row` 返回 bool 却被解包；「引用最新一条」改 `quote.latest` 意图，执行时右键真正最新。
- **「回复时好时坏/界面卡死」先查窗口状态与热激活配额，别查会话名/网络/AI**：最小化伪矩形被绑成 main_hwnd（日志「校准=1108 vs 当前=280」）、gate 已为 1 也计入限流 → 300s 内强制 OCR 兜底；sender 两次 `bring_to_front` + rebind 清 `_uia` 触发预热巡检再打补丁形成自激循环。排查日志 `kefu-system/wechatauto_logs/app_YYYYMMDD.log` 的「热激活」「重绑 hwnd=」「自动最小化遮挡窗口」。
- **微信重启后 0 秒报「微信未登录」**：`WeChatGUI` 缓存 `main_hwnd`，`is_alive()` 只查 IsWindow；已加 `rebind()`（重新评分定位主窗 + 刷新 render/pid + 清缓存），别再短路。
- **窗口识别必须进程硬门槛**：`_is_wechat_process`（进程名 startswith('weixin')，查询失败返回 None 不否决）在 `_looks_like_main_window`/`_find_main_window` 一票否决；标题/类名只能用于同进程内区分（曾把 EditPlus 的「微信客服开发-记事本.txt」绑成微信主窗，点击全落到记事本）。
- **不要并发操作微信 UIA，启动预热必须保留**：provider 单线程，后台 UIA 线程必须持 `_gui_lock`（68s 事故根因是预热线程没持锁）；`main.py` 用 `sender.start(prewarm=True)`，`_prewarm` = `_ensure_gui()` + `warm_up()`（warm_tree，不激活窗口）全程持锁、抢不到即弃。UIA 首次物化 15~30s 必须在启动时做掉。
- 输入框 Accessibility Name 可能拼 UI 提示语（「文件传输助手长按鼠标 语音输入」），会话校验用 `same_chat` 宽松匹配；「给自己发消息」username = 本账号 wxid（本机 `wxid_hu7azjb674f721`，**不是 filehelper**），不在 `monitor_targets` 就一条日志都没有；微信语音异步落库（消息行先到、VoiceInfo.voice_data 后到），转写失败进 `voice_retry.py` 退避队列（5/15/45/120/300s）。

## 领域结论
- 微信 4.x 本地库**不存群名片**，`contact.nick_name` 是全局昵称；只能从 session.db `last_sender_display_name` 增量学习（`group_cards` + 最后发送者守卫）。
- 企业微信联系人 `local_type=5/6/7`、username `xxx@openim`，简称在 `contact.db` 的 `openim_wording`。「昵称@企业简称」是展示名不能直接当搜索词：`_resolve_search_keyword` 只剥 `@openim`，发消息要 `_chat_name()` 剥后缀 + 打开后验标题栏企业名标签，不一致拒发（曾实操发错给同名好友）。同名联系人只能「逐个点开验标签」（`_collect_results(keep_freq=True)` + `_open_matching_openim()` + `_corp_in_label()`），分区/副标题判身份在本机全部失效。标题栏企业名 AID 会随微信版本整段搬家（4.1 起搬到 `...title_h_view.left_v_view...current_chat_openim_name`），现用 `OPENIM_NAME_AIDS` 新旧两套 + `openim_label()` 三态 + 子树扫「@ 开头文本」兜底；换版本先跑一次自检。
- .dat 图片 V2：`[6B magic][4B aes_size][4B xor_size][1B pad][AES 密文][raw 明文][XOR 密文]`，key 由 cfg_dword + wxid 派生。图片「未就绪」两因：原图按需下载（`_h.dat` 是高清版），**缩略图 `_t.dat` 本身就是完整小 JPEG**（62 张里 42 张靠缩略图）；单字节 XOR 密钥按账号固定（本机 0xF9），逐文件尾字节反推在带填充文件上会推出假密钥（40 样本 35 对 5 错）→ JPEG 头正常后全乱码。现用账号密钥投票 + 实测兜底，18/18 救回。
- **商机类型能否命中八成取决于该类型 `description`，不是 Prompt 模板**：`build_system_prompt` 是交易意向范式（采购/委托/询价/求供 + 标的物 + 意向动作），非交易型（投诉/售后/求助）天然落空；扩成「命中特征词清单 + 豁免句（没有报价/数量/标的物不影响命中）+ 字段约定」即可，实测 7 条投诉全命中、6 条闲聊全不命中，无需改代码。
- 群商机回复分「群内 / 私信」：`ReplyRequest.target`（session|sender）；私信只发本人且必须 `is_friend`（`db.py`：local_type 3/1/5-7 好友，0 或 delete_flag=1 → False），不许带 at/quote；手动确认模式 @ 不生效（`input_only` 只粘贴文字）；@ 必须用 `list_group_members` 当前群昵称。
- 授权 v2 fail-closed：`license.enabled` 任何取值都要授权，只有厂商测试口令（明文或 SHA256 指纹）能关；`KEFU_LICENSE_MODE=enforce`（`run.py --license`）优先级最高。切模式用 `run.py --license-dev 口令` / `--license-on`，代码里只有 `DEV_TOKEN_SHA256`。机器码**不能用 WMI**（本机 WMI 仓库损坏，坏了全体客户授权同时失效），现用注册表 `MachineGuid` + `GetVolumeInformationW` 卷序列号；许可证 Ed25519 签名（客户端内置公钥表 `PUBLIC_KEYS`），离线宽限 7 天靠 `last_ok_at`、防时钟回拨靠 `max_seen_ts`。契约见 `kefu-system/docs/授权服务接口文档.md`，模拟服务器 `tools/mock_license_server.py`。
- Ctrl+C 卡 "Shutting down" 的两个默认值：uvicorn `timeout_graceful_shutdown=None` 无限等（且早于 `lifespan.shutdown()`）、解释器退出等 non-daemon AnyIO worker 卡在 `threading._shutdown()`。`run.py` 已修 graceful=5s + `_KefuServer` 20s 看门狗（抓全线程栈 → `data/shutdown-dump-*.log` → `os._exit(130)`），别用裸 `uvicorn.run()`。

## 打包与自动更新（P0 离线包 + P1 核心已实测通过；分支 feat/packaging-p1）
- 定稿：嵌入式 CPython 3.11 x64 + 托盘/默认浏览器 + **阿里云 OSS 签名 manifest（Ed25519）** + 无签名内测包（升级前强制提示关杀软 + 三层兜底）。客户机联网、授权联网（生产地址打包前注入 `KEFU_LICENSE_SERVER`）。版本判定用数字元组，禁止字符串比较（`1.9 < 1.10`）。
- 三层目录 `app\<版本>` / `runtime` / `data`；`data/` 永不覆盖（`secret.key`、`license.state.json` 覆盖 = 全体授权失效）；A/B 切换 + 回滚 + `PRAGMA user_version` 迁移 + 升级前 DB 备份。
- **P0 最小离线包实测通过（2026-11-15 20:00，分支 `po/offline-bundle`）**：新增 `packaging/build_p0.py` + `selftest_p0.py`（仅 2 个文本脚本），产物 `D:/tmp/kefu-p0-bundle` 715.5MB、不进 git。embed CPython 3.12.10 + 本机 cp312 wheel 复制 + 229MB 模型 + 84MB ffmpeg + SILK decoder，**8/8 探针全绿、92 个业务文件 SHA256 DIFF=0（零改动）**；真实中文语音转写成功（模型加载 3.69s / 识别 0.88s）、HEVC→JPEG 成功、`/` 与 `/openapi.json` 均 200（lifespan=off，未碰微信）。关键在于按原两层布局铺资源：`app/config.py` 全用 `BASE_DIR = Path(__file__).resolve().parent.parent`，`voice_asr.model_ready()` 只看文件存在（预置即 READY、不联网）、`media._ffmpeg_exe()` 先 `shutil.which`（PATH 注入即命中内置）、`APP_VERSION` 已支持 `KEFU_VERSION_FILE`/`KEFU_APP_ROOT`。详见 `2026-11-15.md` 第 8 轮。
- **embed 装配坑（P1 会再遇到）**：embed 版不执行 `site-packages/*.pth` → `pywin32.pth` 失效 → `wechatauto/utils/win32.py` 的 `import win32ui` 报 `ModuleNotFoundError`（首次自检 7/8）。必须把 `win32`/`win32/lib`/`pythonwin` 显式写进 `python312._pth` + `os.add_dll_directory(pywin32_system32, sherpa_onnx/lib)`，并排除 `__editable__*.pth`（指向开发机仓库）。`pip install --target` 同样适用，target 目录的 .pth 不会被 site 处理。
- **3.11 依赖**：`sherpa-onnx 2.0.1` / `pywin32 312` / `winsdk 1.0.0b10` 均有 cp311-win_amd64 wheel；`imageio-ffmpeg` 是 py3-none-any（正常）。**`opencv-python 5.0.0.93` 无 cp311 wheel，必须锁 4.10.0.84（cp37-abi3）**；3.11 可行但要锁定版本矩阵，不能照抄本机 3.12 环境。
- **P1 核心已实现（2026-11-17，分支 `feat/packaging-p1`，未 commit）**：新增 `packaging/build_p1.py`（三层构建器）、`selftest_p1.py`、`launcher.py`（托盘+浏览器）、`updater.py`（验签/A-B/关杀软）、`start_p1.bat`；`app/config.py` 加数据分离（`KEFU_DATA_DIR`/`KEFU_ASSETS_DIR`，无 env 回退 P0 兼容）。产物 `D:/tmp/kefu-p1-bundle` 316MB、**0 .py / 4761 .pyc**（全量 legacy pyc，含 site-packages），7/7 自检 + HTTP 200 + junction A/B 切换/回滚 + 托盘起后端均实测通过，单测 23 passed。详见 `2026-11-17.md` 与技术方案附录 B。
- **铁律（P1 实测）**：① embed Python 忽略 PYTHONPATH，`._pth` 写死 `app/current` **目录 junction** 做 A/B（PowerShell `Get-Item.Delete()` 切，别用 Remove-Item；禁从 Bash 调 powershell）；② pywin32 常量是 `winerror.ERROR_ALREADY_EXISTS`，单实例用 `WaitForSingleObject(mutex,0)`；③ 全量 pyc 用 `compileall -b` 删 .py，fastapi/pydantic 的 getsource 有 try/except 无害；④ `--skip-site` 时业务代码每次仍要重编删源（否则残留 .py）；⑤ bat 源 UTF-8 → 转 GBK+CRLF。
- **P1 尚未做**：Inno Setup 安装器、前端更新 UI + `/api/system/update/*` 接口、build_release.py（manifest 生成/上传）、3.11 embed 装配、干净机+杀软实测与签名。仍未改：`wechatauto/param.py` `os.getcwd()`、`uia_driver.py` 硬编码微信路径、`license.py` 卷序列号写死 `C:\`。
- **大文件绝不进 git**（runtime / 229MB 模型 / ffmpeg / staging / build 输出），只提交构建脚本与 .gitignore 规则；资源由构建脚本从本机缓存目录复制。既有 `build_exe.bat` / `wechat-adapter.spec` 只打 adapter，不含后端/前端/ASR。

## 历史批次（已提交，勿重做）
- `c6045c5` AI 账单/清理修复；`036c17e` 企业微信好友搜索；`633f27f` 离线语音转写；`87a6b91` 语音补转写队列 + 发送链路性能修复。详见 `.workbuddy/memory/2026-11-10.md`、`2026-11-11.md`。
