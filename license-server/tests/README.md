# license-server 回归测试（QA 独立复验）

只读 `app/` 源码、直连测试库做断言，**不使用**服务端自己的函数验自己的签名：
客户端验签一律在 `../kefu-system` 目录下起子进程调用真正的
`app.license.payload_bytes / verify_license / extract_error`。

## 运行

```bash
cd license-server
# 服务没起也没关系：conftest 会自动 `python run.py serve --port 8800` 并在结束时收掉
LS_PBKDF2_ITERATIONS=100000 python -m pytest tests -q
```

`LS_PBKDF2_ITERATIONS=100000` 只是把 PBKDF2 轮数降到允许下限以加快用例，
不影响任何业务逻辑（服务端与测试进程同时生效，避免触发密码哈希在线升级）。

指定其它地址：`LS_TEST_BASE=http://host:port pytest tests -q`。

## 分组

| 文件 | 分组 | 覆盖 |
|---|---|---|
| `test_a_contract.py` | A 契约一致性 | 激活签发 / 客户端验签 / `payload_bytes` 逐字节一致 / 心跳重签 / 吊销与到期 `200+ok=false` / 7 个错误码 / 查询接口 |
| `test_b_rules.py` | B 业务规则 | 幂等不叠加、换码叠加、吊销后重发、一机一证（应用层 + 1062）、缩期与设到期日、恢复冲突、并发兑换同一码 |
| `test_c_security.py` | C 鉴权与安全 | 未登录 401、CSRF、超长入参、SQL 注入、LIKE 通配符、强制改密闸门、登录锁定与账号枚举、会话哈希、信息泄漏、限流绕过 |
| `test_d_stats.py` | D 统计口径 | dashboard 全量计数手算比对、趋势 30 天补 0、按手机号聚合与客户去重 |
| `test_e_ops.py` | E 前端与运维 | 页面 200、`/` 跳转、XSS（innerHTML）、`init-db` 幂等、`check` 自检、私钥缺失中文退出、CLI 造号 |

## 已修复的缺陷（回归用例保留）

1. LIKE 通配符未转义（`q='%'`/`q='_'` 命中全表）→ 2026-11-14 修复：`app/repo/__init__.py`
   新增 `like_pattern()`，5 处 LIKE 统一转义（用例 `test_c04_*`）
2. `machines.active_7d` 把"已过期但仍占 current_key"的机器计入 → 2026-11-14 修复：
   `app/repo/stats.py` 的 `active_7d` 增加 `expires_at > now` 条件，设计文档 §6.3 SQL 同步
   （用例 `test_d04_*`）

## 注意

- 用例会 **TRUNCATE** 业务表并重置 `admin_user`，**只能对测试库跑**（`.env` 里的
  `LS_DB_*` 指向的库）。
- 每个用例都用独立 `X-Forwarded-For`（`LS_TRUST_PROXY=1`），避免进程内滑动窗口
  限流把用例互相打死。
