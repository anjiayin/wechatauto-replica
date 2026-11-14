# license-server 回归测试（QA 独立复验）

只读 `app/` 源码、直连测试库做断言，**不使用**服务端自己的函数验自己的签名：
客户端验签一律在 `../kefu-system` 目录下起子进程调用真正的
`app.license.payload_bytes / verify_license / extract_error`。

## 数据库隔离（铁律）

测试**绝不触碰真实库**（`.env` 里 `LS_DB_NAME` 指向的 `wechat_kefu`）：

- 测试连的是独立测试库 `wechat_kefu_test`（`= 真实库名 + "_test"`，
  `LS_TEST_DB` 环境变量可覆盖），不存在时自动创建并建表；
- 测试服务**总是自起**在专用端口 `8801`（`LS_TEST_PORT` 可改），**绝不复用**
  已在 8800 运行的真实服务——否则测试请求会写到真实库；
- `reset_all()` 内置守卫：目标库名不以 `_test` 结尾直接抛错拒绝执行。

真实库里的激活码 / 许可证 / 客户数据在测试前后保持原样；跑完测试**不需要**
也不允许再对真实库做"恢复交付态"操作。

## 运行

```bash
cd license-server
# 一条命令即可：conftest 自动建测试库 + 自起 8801 服务 + 结束收掉
LS_PBKDF2_ITERATIONS=100000 python -m pytest tests -q
```

`LS_PBKDF2_ITERATIONS=100000` 只是把 PBKDF2 轮数降到允许下限以加快用例，
不影响任何业务逻辑（服务端与测试进程同时生效，避免触发密码哈希在线升级）。

可选环境变量：`LS_TEST_PORT`（默认 8801）、`LS_TEST_BASE`（请求地址，默认由
端口推导，二者永远一致）、`LS_TEST_DB`（测试库名，默认 `wechat_kefu_test`）。

真实库管理员体检（只读）：`python tests/_verify_admin.py`。

## 分组

| 文件 | 分组 | 覆盖 |
|---|---|---|
| `test_a_contract.py` | A 契约一致性 | 激活签发 / 客户端验签 / `payload_bytes` 逐字节一致 / 心跳重签 / 吊销与到期 `200+ok=false` / 7 个错误码 / 查询接口 |
| `test_b_rules.py` | B 业务规则 | 幂等不叠加、换码叠加、吊销后重发、一机一证（应用层 + 1062）、缩期与设到期日、恢复冲突、并发兑换同一码、客户绑定、作废与导出 |
| `test_c_security.py` | C 鉴权与安全 | 未登录 401、CSRF、超长入参、SQL 注入、LIKE 通配符、强制改密闸门、登录锁定与账号枚举、会话哈希、信息泄漏、限流绕过 |
| `test_d_stats.py` | D 统计口径 | dashboard 全量计数手算比对、趋势 30 天补 0、按手机号聚合与客户去重 |
| `test_e_ops.py` | E 前端与运维 | 页面 200、`/` 跳转、XSS（innerHTML）、`init-db` 幂等、`check` 自检、私钥缺失中文退出、CLI 造号 |

## 已修复的缺陷（回归用例保留）

1. LIKE 通配符未转义（`q='%'`/`q='_'` 命中全表）→ `app/repo/__init__.py`
   新增 `like_pattern()`，5 处 LIKE 统一转义（用例 `test_c04_*`）
2. `machines.active_7d` 把"已过期但仍占 current_key"的机器计入 →
   `app/repo/stats.py` 的 `active_7d` 增加 `expires_at > now` 条件，设计文档 §6.3 SQL 同步
   （用例 `test_d04_*`）
3. 测试污染真实库（旧 `reset_all` 直接 TRUNCATE `.env` 指向的库 + 夹具可能
   复用真实服务）→ 引入测试库隔离 + 专用端口自起服务 + 库名守卫

## 历史教训

- `_helpers.ADMIN_PASSWORD` 必须与交付文档口令逐字节一致，否则每次
  `reset_all()` 都会悄悄改掉真实库管理员密码（曾因此导致用户登录不上）。
- `LS_TEST_PORT` 与 `LS_TEST_BASE` 必须同源推导，只设其一曾导致请求打到
  8800 的旧服务上（表现为新接口 405）。
