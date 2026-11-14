# 授权服务后台（license-server）

电脑授权服务的正式后台：客户端用**激活码**换一张 **Ed25519 签名的许可证**，
之后每 6 小时联网确认一次；管理台负责生成激活码、看许可证、延期/缩期/吊销/恢复、
客户档案与审计。

- 技术栈：FastAPI + PyMySQL 裸 SQL + 原生 HTML/JS（无前端构建链）
- 数据库：**MySQL 5.6**（已验证 5.6.38-log）
- 依赖：`fastapi / uvicorn / pydantic v2 / cryptography / PyMySQL`（不装 SQLAlchemy / passlib / jose / dotenv / jinja2）
- 完整设计契约：`docs/design.md`；客户端接口契约：`../kefu-system/docs/授权服务接口文档.md`

---

## 1. 目录结构

```
license-server/
├── run.py                 # 唯一运维入口：serve / init-db / create-admin / issue-code / check
├── requirements.txt
├── .env.example           # 配置模板（不含真实口令）
├── .env                   # 本机实际配置（已在 .gitignore 中，不入库）
├── app/
│   ├── config.py          # env > .env > 默认值
│   ├── errors.py          # 统一错误体 {"error":{"code","message"}}
│   ├── db.py              # 连接池 + 事务 + schema 执行
│   ├── schema.sql         # 6 张表 DDL（幂等）
│   ├── signing.py         # payload_bytes（唯一实现）/ Ed25519 / 时间工具
│   ├── security.py        # PBKDF2、会话令牌、CSRF、Cookie、客户端 IP
│   ├── ratelimit.py       # 进程内滑动窗口限流
│   ├── models.py          # Pydantic 请求模型（含长度上限）
│   ├── boot.py            # 建表 → 首启建号 → 清会话 → 启动自检
│   ├── main.py            # create_app（路由 + 静态挂载）
│   ├── repo/              # 数据访问（customers/codes/licenses/admins/audit/stats）
│   ├── services/          # 业务编排（license_service / admin_service）
│   ├── api/               # deps / client_api / admin_auth / admin_api
│   └── static/            # 管理台（/admin 挂载）
└── docs/design.md
```

---

## 2. 首次初始化

```bash
cd license-server

# 1) 依赖（本机已装齐，通常无需联网）
python -m pip install -r requirements.txt

# 2) 配置：复制模板后填真实值（数据库口令 / 私钥路径）
cp .env.example .env
#   Windows: copy .env.example .env

# 3) 建表（幂等，可重复执行）
python run.py init-db

# 4) 自检签名序列化（会与客户端 payload_bytes 逐字节比对）
python run.py check

# 5) 启动
python run.py serve --host 127.0.0.1 --port 8800
```

启动日志会打印：监听地址、数据库 `user@host:port/db`（不含口令）、`key_id`、
私钥路径、**公钥 base64**、首启管理员初始密码、以及默认口令/Cookie 的安全告警。

打开 <http://127.0.0.1:8800/admin/> 进入管理台（未登录自动跳 `login.html`）。

### 首启管理员

| 情况 | 行为 |
|---|---|
| `LS_BOOTSTRAP_ADMIN_PASSWORD` 有值 | 用它作为初始密码，`must_change_password=0` |
| 留空（默认） | **随机 16 位密码打印到控制台一次**，并置 `must_change_password=1`；改密前除 `me/password/logout` 外接口一律 403 |

手工建号/重置：

```bash
python run.py create-admin --username ops --password Abc12345
python run.py create-admin --username ops --password Abc12345 --must-change   # 强制首登改密
```

---

## 3. 联调客户端

```bat
:: kefu-system 目录下
set KEFU_LICENSE_ENABLED=1
set KEFU_LICENSE_SERVER=http://127.0.0.1:8800
python run.py
```

链路：客户端显示机器码 → 管理台「激活码」页生成码（填客户名+手机号）→ 客户端粘贴 →
`activate` 成功（`key_id=dev`）→ 6 小时后自动心跳。命令行造码也可以：

```bash
python run.py issue-code --days 365 --phone 13800000000 --customer "某某汽配" --count 5
```

> `LS_KEY_ID` 必须与客户端 `app/license.py` 的 `PUBLIC_KEYS` 键一致；
> 换密钥时新旧公钥要在客户端**同时存在**，旧证才能继续验签。

---

## 4. 生产部署

### 4.1 必须覆盖的配置

| 变量 | 说明 |
|---|---|
| `LS_DB_HOST` / `LS_DB_PORT` / `LS_DB_USER` / `LS_DB_PASSWORD` / `LS_DB_NAME` | **生产必须覆盖**；启动日志若提示「正在使用默认数据库口令」说明没覆盖 |
| `LS_PRIVATE_KEY_PATH` | 私钥绝对路径，`chmod 600`，只放服务器，绝不进 git/客户端安装包 |
| `LS_KEY_ID` | 正式密钥标识（如 `prod-2026`），同步到客户端 `PUBLIC_KEYS` |
| `LS_COOKIE_SECURE=1` | HTTPS 下**必须** 1（否则浏览器不发送 Cookie） |
| `LS_TRUST_PROXY=1` | 反代前置时 1；直连公网时必须 0（防伪造 `X-Forwarded-For`） |
| `LS_ENABLE_DOCS=0` | 建议关闭 `/docs` |
| `LS_BOOTSTRAP_ADMIN_PASSWORD` | 不设则随机打印，更安全 |

> 变量名兼容两套写法（带 `LS_` 前缀优先），例如 `LS_DB_HOST` 与 `DB_HOST`、
> `LS_KEY_ID` 与 `LICENSE_KEY_ID`，任选其一即可。

### 4.2 Nginx HTTPS 反代

```nginx
server {
    listen 443 ssl http2;
    server_name license.example.com;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    client_max_body_size 2m;

    location / {
        proxy_pass         http://127.0.0.1:8800;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

应用侧对应配置：`LS_HOST=127.0.0.1`、`LS_PORT=8800`、`LS_TRUST_PROXY=1`、
`LS_COOKIE_SECURE=1`。健康检查挂 `/api/health`（DB 不通返回 503）。

### 4.3 运行要求

1. **`--workers 1`**：限流器与首启建表都是进程内的；本服务 QPS 极低，单进程足够。
2. **服务器时钟必须准（NTP）**：`expires_at` 判定依赖服务器时间，即使内部统一东八区。
3. **数据库账号最小权限**：`SELECT/INSERT/UPDATE`（`CREATE/INDEX` 仅首启建表需要，之后可收回）。
4. **备份**：`mysqldump --single-transaction wechat_kefu license license_code audit_log customer`。
   授权数据是商务资产，备份优先级等同业务库。
5. **私钥轮换**：改 `LS_KEY_ID` + 换文件 + 客户端加新公钥；旧证仍用旧 `key_id` 验签。

---

## 5. 接口速查

客户端（无需鉴权，路径写死在客户端）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/license/activate` | 激活（限流 10 次/60 秒，键=IP+机器码） |
| POST | `/api/v1/license/heartbeat` | 心跳；**授权失效返回 200 + `ok=false`** |
| GET | `/api/v1/license/query` | 查询（限流 30/60） |
| GET | `/api/health` | DB 探活，不通返回 503 |

管理端（`/admin/api/*`，除 `login` 外全部要会话；写操作要 `X-CSRF-Token`）：
`login/logout/me/password`、`dashboard/codes(+/export)/licenses(+/renew/extend/expiry/revoke/restore)/customers/machines/audit/meta`。

错误体统一：`{"error": {"code": "...", "message": "中文可操作提示"}}`。

---

## 6. 常见故障

| 现象 | 原因与处理 |
|---|---|
| 客户端提示「许可证签名校验失败」 | ① 服务端 `LS_KEY_ID` 与客户端 `PUBLIC_KEYS` 键不一致；② 公钥不配对——用启动日志里的「公钥(base64)」和客户端 `PUBLIC_KEYS` 逐字符比对；③ 有人改过 `payload_bytes`（必须 `sort_keys=True` + `separators=(",",":")` + `ensure_ascii=False`） |
| 客户端提示「授权已到期」但后台显示未到期 | 时区错位。库内必须是**东八区 naive** 时间，payload 必须带 `+08:00`；检查 `python run.py check` 的时间换算行与服务器时钟 |
| 客户端一直「离线运行中」 | 4xx 被客户端当作「服务器暂不可用」。心跳的授权失效必须是 200+`ok=false`；用 `curl -i` 确认状态码，检查反代是否改写了响应 |
| 激活返回 409 `conflict` | 同一机器并发激活，`uk_current_machine` 唯一索引兜住了并发（INV-1）。稍后重试即可；若持续出现说明有并发脚本 |
| 写操作报 1062 Duplicate entry | 唯一索引冲突：`uk_license_id`（许可证号撞了，重试）/ `uk_current_machine`（同机已有生效证）/ `uk_code`（激活码撞了）。业务上先查该机器是否已有生效证 |
| 恢复返回 409 `already_active` | 该机器已有另一条生效证（通常是吊销后重发过）。用消息里给出的那条证；旧证保留作历史即可 |
| 登录返回 423 `account_locked` | 连续 5 次密码错误锁定 15 分钟。手工解锁：`UPDATE admin_user SET locked_until=NULL, failed_count=0 WHERE username='ops';` |
| 写操作返回 403 `csrf_invalid` | 页面令牌失效（会话被重建/开了新标签页）。刷新页面（`GET /admin/api/me` 会取回 CSRF）后重试 |
| 任意 `/admin/api/*` 返回 401 | 会话过期（默认 12 小时，滑动续期上限 7 天）或被改密踢下线。重新登录 |
| 启动即退出 | `LS_PRIVATE_KEY_PATH` 不存在/不是 Ed25519：控制台会打印中文原因并以退出码 2 结束（不静默启动） |
| `/api/health` 返回 503 | 数据库不可达：检查 `LS_DB_*`、网络、账号权限 |
| 长字段被静默截断 | MySQL 非严格模式（`sql_mode=NO_ENGINE_SUBSTITUTION`）不会报错。入参长度已由 Pydantic `max_length` 拦在入口，**新增字段记得同步加** |

---

## 7. 数据维护

授权数据**只增不删**（许可证/激活码不物理删除，吊销只写 `revoked_at`）。
联调后如需清空（**生产严禁执行**）：

```sql
TRUNCATE TABLE admin_session;
TRUNCATE TABLE audit_log;
TRUNCATE TABLE license;
TRUNCATE TABLE license_code;
TRUNCATE TABLE customer;
TRUNCATE TABLE admin_user;
```

然后重新建一个管理员：

```bash
python run.py create-admin --username admin --password '<强口令>' --must-change
```
