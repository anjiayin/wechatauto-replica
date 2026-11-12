# 项目长期记忆 · wechatauto-replica / kefu-system

## 项目
- `kefu-system/`：微信客服/商机监测系统，FastAPI + 原生 sqlite3 + React18/TS/MUI5，Windows。
- `wechatauto/`：微信 4.x UIA/数据库驱动（uiautomation + OCR + .dat 解密）。
- 用户偏好：简体中文；重视真实数据/实机验证，不接受只凭推断的结论。

## 当前交接入口
- **2026-11-12 交接文档：`D:/Git/wechatauto-replica/HANDOVER-2026-11-12.md`**（第五批 9 项需求 8 完成 1 未完成，全部未提交）。
- 基线 `HEAD=87a6b91`；测试基线 475 passed。

## 铁律 / 常踩的坑
- pytest 必须 `--basetemp=D:/tmp/...`（本机 safe-delete 守卫会拦临时目录清理）；全量约 4~5 分钟。
- UIA 实机测试只允许「文件传输助手」，**禁止向真实联系人/群发消息**。
- 微信只读探查用 `WeChatDB(workdir=r'D:\wechatauto_cache')`；真实微信库在 E 盘 `E:\xwechat_files\...`，`auto_detect_db_dir()` 返回的 `E:xwechat_files` 缺反斜杠，必须 `os.path.abspath()`。
- 时间口径一律交给 SQLite `datetime('now','localtime',...)`；Python 拼 `T` 分隔时间字符串会因 TEXT memcmp 误删整日数据。
- `voice_texts` / `ai_usage` / `voicecache` 不参与界面「立即清理」（`_PURGE_TABLES` 只有 analysis_logs + opportunities）。
- `kefu-system/data/` 已 gitignore（ASR 模型 229MB、语音/图片缓存）；`tools/silk_v3_decoder.exe` 已被 git 跟踪。

## 领域结论（已实测，别再推翻）
- 微信语音**异步落库**：消息行先到、`media_*.db VoiceInfo.voice_data` 后到；转写失败须进 `voice_retry.py` 退避队列（5/15/45/120/300s），失败直接丢弃会漏商机。
- 微信 4.x 本地库**不存群名片**；`contact.nick_name` 是全局昵称。群昵称只能从 session.db `last_sender_display_name` 增量学习（`group_cards` 表 + 最后发送者守卫）。
- 微信 4.x 输入框 Accessibility Name 可能拼上 UI 提示语（如「文件传输助手长按鼠标 语音输入」）；会话校验必须用 `same_chat` 宽松匹配，严格相等会误判失败并落入 OCR 兜底（曾导致发送全链路 56.6s）。
- 企业微信联系人 `local_type=5/6/7`、username 形如 `xxx@openim`；企业简称在 `contact.db` 的 `openim_wording` 表。
- .dat 图片 V2 格式：`[6B magic][4B aes_size][4B xor_size][1B pad][AES 密文][raw 明文][XOR 密文]`；key 由 cfg_dword + wxid 派生（详见 media.py）。

## 历史批次（已提交，勿重做）
- `c6045c5` AI 账单/清理修复；`036c17e` 企业微信好友搜索；`633f27f` 离线语音转写；`87a6b91` 语音补转写队列 + 发送链路性能修复。
- 详见 `.workbuddy/memory/2026-11-10.md`、`2026-11-11.md`。
