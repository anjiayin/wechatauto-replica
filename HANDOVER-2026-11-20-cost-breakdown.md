# 群消息总结批次交接（费用分项 + 反馈修复，2026-11-20 晚）

> 本文档供新会话接续使用。当前分支 `feat/summary`，工作区干净，
> 领先 `origin/main`（1.2.9）**11 个提交**，**未合并、未发版**。
> 本日完成：群消息总结 P0 落地 + 两轮反馈修复 + AI 费用分项（usage_kind）+ 一次启动崩溃修复。

## 1. 提交链（自底向上）

```
5bf9294  feat: 群消息总结 P0（数据链/预算/服务/API/前端）        ← 用户手动 commit
619d9ad  fix: 设置页配置统一走 summary/settings，删 P0 schedules 端点
ae6a055  test: QA 独立验证 35 用例 + 成员显示名回退与统计行修复
3ffcd55  docs: 工作日志补记实现收尾与启动说明
e5ff8e6  fix: 总结反馈批次——AI失败留痕/中文参数错误降级/预算双向联动/同名群区分
3ef2cf6  docs: 工作日志补记反馈批次修复
92ea4ff  docs: 恢复工作日志历史内容并追加反馈批次记录
cd8d20b  费用汇总+分项（商机/总结/图片识别）+ 端口保存按钮对齐   ← 本会话主体
2bcf62e  日志：费用分项批次全量测试通过（1771 passed）
b57636a  修复老库启动报 no such column: usage_kind               ← 用户实测报修后的热修
e177132  日志：usage_kind 启动崩溃修复记录
```

## 2. 本日三大成果

### 2.1 群消息总结 P0（5bf9294 → ae6a055）

- PRD/技术方案：`kefu-system/docs/PRD-群消息总结-2026-11-20.md`、`技术方案-群消息总结-2026-11-20.md`。
- 用户拍板的 7 条产品决策见 `.workbuddy/memory/2026-11-20.md` 设计阶段小节，**均已落实**。
- 设置页唯一通路：`GET/PUT /api/summary/settings` 白名单 7 键（预算 5 + 定时 2），非法值 422；`/api/summary/schedules` 已删（404）。
- 预算：`app/billing.py` 共享换算（`estimate_cost`），预算预检与账单同公式防漂移；金额↔token 双口径；超限 `block|truncate_recent`。
- 截断标注「已截断：仅统计最近 N 条」四处一致（结果区/txt/html/语料首行）。
- QA 独立验证 `test_summary_qa.py` 35 用例通过。
- **T05（P1：定时调度器启线程/历史搜索/提示词模板）未做**，后续按需。

### 2.2 反馈批次（e5ff8e6）

1. AI 失败 WARNING 留痕（summary.generate + router_summary.generate，只记元信息不打印语料）；
2. GLM 网关中文「参数错误」降级：`_is_unsupported_param_error` 加中文关键词 → `_create` 走无参提示词兜底自动重试；
3. 设置页金额↔token 双向联动（基准 输入:输出 = 10000:2000 + 当前单价；未配单价不联动并提示）；
4. 同名群区分：群下拉 renderOption / 设置页 MenuItem 加 username 灰字辅助行。

### 2.3 AI 费用汇总分项 + vision 入账（cd8d20b + b57636a）

用户原始诉求：顶栏和设置页的费用估算应包含商机分析 + 群消息总结两类消耗并有汇总/分项说明。
排查发现三件事：①此前无分项展示；②vision（图片识别）token 完全不入账；③借此补全。

## 3. usage_kind 技术设计（接手者必读）

**三类取值**：`opportunity`（商机分析）/ `summary`（群消息总结）/ `vision`（图片识别）。
`ai_usage` 表新增 `usage_kind TEXT NOT NULL DEFAULT 'opportunity'`；`insert_ai_usage` 对未知 kind 白名单防御归为 opportunity。

**写入路径**：
- 商机分析：`insert_analysis_log` 事务镜像写 `ai_usage`（硬编码 `'opportunity'`）；
- 群消息总结：`summary.py` 直写 `insert_ai_usage(..., usage_kind="summary")`；
- 图片识别：`wechat_service._save_image_analysis` 加 `prompt_tokens/completion_tokens` 可选参，落库后 `insert_ai_usage(usage_kind="vision")`（try/except 吞异常不影响识别主流程）；**4 个调用点**（worker×2 + 补识别×2）全部透传 `result.get("prompt_tokens")`。

**存量回填（近似，总费用恒精确）**：
- summary 按「`group_summaries` 同分钟（`substr(created_at,1,16)`）+ pt/ct 完全相同」匹配；
- vision 按「`image_analysis` 同分钟 + 同 model」匹配；
- 其余保持 opportunity。真实库已验证：3209 opportunity + 5 summary，0 vision 误标。

**API（追加字段，向后兼容）**：
- `StatusOut.today_cost_breakdown: dict`、`BillingOut.breakdown: dict`（旧前端忽略不影响契约）；
- `db.usage_by_date(date, usage_kind=None)` 可选过滤；`db.usage_breakdown_by_date(date)` 返回三类分项。

**前端**：App.tsx 顶栏 Tooltip 列「合计 + 三分项」；SettingsPage 账单卡片「其中：」分项行（calls==0 且 cost==0 的类不显示）。

## 4. 启动崩溃教训（铁律，用户实测踩过）

**现象**：cd8d20b 后用户 `python run.py` 启动即崩 `sqlite3.OperationalError: no such column: usage_kind`。

**根因**：我把 `idx_usage_kind` 索引放进 `DDL_STATEMENTS`。老库走 DDL 阶段时 ai_usage 还没有 usage_kind 列，`CREATE INDEX` 直接崩。新库（DDL 建表已带列）不崩，所以自测没拦住。

**铁律**：**引用新列的索引/约束必须放在加列迁移之后创建，绝不进 `DDL_STATEMENTS`**。现在索引在 `_migrate_ai_usage_kind()` 内、`ALTER TABLE ADD COLUMN` 之后（幂等）。

**回归测试**：`tests/test_feedback_round2.py::TestUsageKind::test_old_db_without_kind_column_opens_clean`——sqlite3 直建旧版无 usage_kind 的 ai_usage 表 + 存量数据，走完整 `Database()` 初始化链断言不崩、列/索引就位、数据保留。三种库状态（全新/老库+商机/老库混合）均已覆盖。

**发版检查清单建议**：加一项「旧版本库文件启动新版本」冒烟（本次就栽在这）。

## 5. 客户机影响结论（已向用户答复）

| 客户机版本 | 影响 |
|---|---|
| 1.2.9（origin/main，线上） | 无 usage_kind 代码，**不受影响**，照常运行 |
| cd8d20b（含索引 Bug 的 feat/summary） | 老库启动即崩（用户已实测踩中）；新库不崩 |
| b57636a 及之后 | 三种库状态全覆盖验证通过，安全 |

## 6. 环境与操作铁律（本会话踩过的坑）

1. **全量 pytest 必须 `cd kefu-system` 执行**：仓库根 `/d/Git/wechatauto-replica` 另有一套 50 个旧项目测试，在根目录跑会得到「50 passed」假象。正确命令：
   ```
   cd kefu-system && /d/my_conda_envs/wechatauto/python.exe -m pytest --basetemp=D:/tmp/pt-新目录
   ```
   全量约 9~12 分钟（本次 1771 passed + 2 xfailed，11m45s）。basetemp 必须用**全新目录**，复用旧目录会被 safe-delete 守卫拒清理 → fixture 全 ERROR。
2. **路径配置**：`DB_PATH = DATA_DIR / "kefu.sqlite3"`，`DATA_DIR` 由 **`KEFU_DATA_DIR`** 环境变量或 `APP_ROOT/data` 决定。**不存在 `KEFU_DB_PATH`**——脚本里 `Database()` 无参会直接打开真实开发库（本会话误触发一次，恰好完成真实库升级验证；隔离测试请用 `Database(tmp_path)` 显式传参）。
3. **工作日志 append-only**：`.workbuddy/memory/YYYY-MM-DD.md` 追加用 `cat >>`，`cat >` 会覆盖历史（曾发生过，靠 git show 恢复）。
4. 前端改后必须 `cd kefu-system/frontend && npm run build` + 浏览器 Ctrl+F5；后端改后重启 8766。
5. pytest 只用 `/d/my_conda_envs/wechatauto/python.exe -m pytest`（PATH 里的 python 没装 pytest）。

## 7. 待办 / 后续决策

1. **feat/summary 合并回 main 时机**：待用户拍板。合并前建议跑一次全量回归（命令见 §6.1）。
2. **发版流程**：版本唯一真源 `version.txt`，发版只跑 `python kefu-system/tools/version_sync.py --bump X.Y.Z`；发版前先 `curl .../manifest.json` 读线上真实 sequence 再 +1（模板见 `.workbuddy/memory/2026-11-18.md`）。本次发版务必带上 §4 的老库升级冒烟检查。
3. **T05（P1）**：定时调度器真正启线程（P0 只存配置）/ 历史搜索 / 提示词模板——等用户决定是否做。
4. **vision 回填近似性**：老数据中 vision 无法精确回填（当时未入账），今日起才精确；无需处理，知悉即可。
5. 中断遗留测试修复记录：`test_cost_per_k/m` 引用已被 5bf9294 删除的 `_usage_cost`，已改用 `app.billing.estimate_cost`（cd8d20b 内一并修复）。

## 8. 关键文件索引

| 文件 | 内容 |
|---|---|
| `kefu-system/app/db.py` | ai_usage DDL、`_migrate_ai_usage_kind()`、`insert_ai_usage(kind)`、`usage_by_date`、`usage_breakdown_by_date` |
| `kefu-system/app/billing.py` | `estimate_cost` 共享换算（预算与账单同公式） |
| `kefu-system/app/summary.py` | 总结服务，直写 usage_kind="summary" |
| `kefu-system/app/wechat_service.py` | `_save_image_analysis` 落 vision token（4 调用点透传） |
| `kefu-system/app/api/router_summary.py` | `/api/summary/*`（settings 白名单 7 键、generate、422 分支） |
| `kefu-system/app/api/router_system.py` | get_status/get_billing 分项输出 |
| `kefu-system/app/schemas.py` | BillingKindOut / BillingOut.breakdown / StatusOut.today_cost_breakdown |
| `kefu-system/frontend/src/App.tsx` | 顶栏费用 Tooltip 分项 |
| `kefu-system/frontend/src/pages/SettingsPage.tsx` | 账单分项行 + 端口按钮对齐（height 40） |
| `kefu-system/tests/test_feedback_round2.py` | TestUsageKind 7 用例（含老库直启回归） |
| `kefu-system/tests/test_cache_status_api.py` | 分项 API 契约 3 用例 |
| `kefu-system/tests/test_summary_qa.py` | QA 独立 35 用例 + 反馈回归 2 用例 |
| `.workbuddy/memory/2026-11-20.md` | 本日全程工作日志（含设计决策与教训） |
