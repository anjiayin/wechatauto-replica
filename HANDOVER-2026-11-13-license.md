# 电脑授权（license）交接（2026-11-13）

> 需求：项目交付给客户使用时加授权，按电脑绑定；首次启动根据机器生成不变
> 的机器码，用它请求授权服务器确认有效期；暂用模拟后台，并提供接口文档，
> 后续照文档开发正式后台。
>
> 基线 `HEAD=b99f988`（第 13 批已提交、工作区干净）。改动**未提交**。
> 测试：**1027 passed**（393s，新增授权用例 44 个）。

---

## 一、交付物清单

### 客户端（kefu-system）

| 文件 | 作用 |
|---|---|
| `app/license.py`（新，~900 行） | 机器码、Ed25519 验签、状态机、离线宽限、时钟回拨检测、激活/心跳、进程级单例 |
| `app/api/router_license.py`（新） | `/api/license/{status,machine-code,activate,refresh}` |
| `app/api/license_guard.py`（新） | 拦截中间件：不可用时 403 拦业务接口，白名单放行授权/状态/静态资源 |
| `app/api/deps.py` | 新增 license 单例注入 + `configure_runtime()` / `ensure_runtime_started()` |
| `app/main.py` | 启动时校验授权；未授权**不启动**抓取/分析/发送；激活成功后回调拉起；后台心跳线程 |
| `app/config.py` | `APP_VERSION`、`LICENSE_FILE`、5 个 `license.*` 配置键（默认关闭） |
| `app/schemas.py` | `LicenseStatusOut` / `LicenseActivateIn` |
| `run.py` | 新增 `--license` 参数（交付启动脚本用） |
| `frontend/src/pages/LicensePanel.tsx`（新） | 激活遮罩、顶栏状态 Chip、激活对话框（机器码复制/验证授权码/刷新） |
| `frontend/src/App.tsx` | 接入授权 Chip、横幅、遮罩、首次未授权自动弹激活框 |

### 模拟授权服务器（可独立拷走）

| 文件 | 作用 |
|---|---|
| `tools/mock_license_server.py`（新） | 完整参考实现：activate/heartbeat/query + 管理端（发码/列表/延期/吊销）+ 简易管理页 |
| `tools/gen_license_keys.py`（新） | Ed25519 密钥对生成（打印客户端要内置的 base64 公钥） |
| `tools/license_keys/dev_{private,public}.pem` | **联调用**密钥对（正式环境必须换） |

### 文档

- `docs/授权服务接口文档.md`（新）：写给后台开发，含 3 个必实现接口的完整
  契约、签名序列化规则（跨语言注意点）、错误码表、建表 SQL、安全部署要求、
  客户端要改的几处、本地联调步骤、FAQ。

---

## 二、设计要点（为什么这么做）

1. **机器码只用「永远可读」的标识**：注册表 `MachineGuid` + 系统盘卷序列号
   （Win32 `GetVolumeInformationW` 直读）。**刻意不用 WMI 的主板 UUID /
   CPU ID**——本机实测 WMI 仓库已损坏（`Get-CimInstance Win32_BaseBoard`
   报「无效类」），而 WMI 被 Windows 更新打坏并不罕见：一旦它坏了，所有客户
   机器码同时漂移、授权集体失效。稳定优先，WMI 只在「查看详细信息」里展示。
   现算 < 10ms，**不缓存到磁盘**（缓存文件一改就能伪造机器码）。

2. **非对称签名（Ed25519）而不是 HMAC**：客户端只内置公钥，客户把程序目录
   翻遍也拿不到伪造许可证的能力。

3. **离线宽限**：许可证里签了绝对到期日 `expires_at`；本地另记 `last_ok_at`
   （最近一次成功联网）。断网时 `now - last_ok_at <= grace_days`（默认 7 天）
   照常可用，超过则锁死要求联网确认。避免"客户断网就停工"。

4. **防时钟回拨**：本地记历史最大时间戳 `max_seen_ts`（只在启动/联网成功时
   落盘，普通状态轮询不写盘）；当前时间比它早 1 天以上判 `clock_rollback`。

5. **到期只锁功能，绝不动数据**：`ok=False` 时拦截 `/api/*` 业务接口，微信
   抓取/分析/发送不启动；数据库、图片/语音缓存一律不删不改，续费后原样恢复。

6. **激活后自动拉起，不用重启**：未授权时 runtime 线程不启动，激活成功由
   `deps.ensure_runtime_started()` 回调现场拉起（幂等）。

7. **中间件 fail-open**：授权模块自身异常时放行（宁可少一单，不让客户开不了工）。

8. **同机重复激活 = 续期叠加**：带新激活码再激活时，有效期从「当前到期日」
   与今天中较晚者起叠加天数（客户买第二次直接续，不用先等旧证过期）。

---

## 三、状态机（`app/license.py`）

| state | 含义 | ok |
|---|---|---|
| `disabled` | 未启用授权（开发/自用） | ✅ |
| `active` | 正常，近期确认过 | ✅ |
| `offline_grace` | 连不上服务器，宽限期内 | ✅ |
| `unlicensed` | 从未激活 | ❌ |
| `expired` | 超过 `expires_at` | ❌ |
| `offline_expired` | 离线超过 `grace_days` | ❌ |
| `invalid` | 验签失败 / 机器码不符 / 缺到期日 | ❌ |
| `revoked` | 服务器吊销（下次确认即本地锁死） | ❌ |
| `clock_rollback` | 系统时间被调早 | ❌ |

---

## 四、实测记录（均为真实 HTTP / 真实文件）

1. **机器码**：`BBCF-BCA8-F127-5747-05D8-46B3-E8D8-CAF0`，采集耗时 0.000s，
   两次一致；成分 `machine_guid` + `volume_serial`（本机 WMI 不可用，已绕开）。
2. **激活**：管理端发 30 天码 → 客户端激活 → `active`，到期 2026-12-13，
   剩 29 天，客户名正确。
3. **续期**：管理端 +365 天 → 客户端心跳 → 剩 394 天（叠加正确）。
4. **吊销**：管理端吊销 → 客户端心跳 → `revoked`，本地锁死；再激活被拒
   （`code=revoked`）。
5. **篡改**：手改 license.json 的 `expires_at` → `invalid`（签名拦住）。
6. **离线**：把 `last_ok_at` 改到 30 天前 → `offline_expired`；3 天前 →
   `offline_grace`（仍可用）。
7. **拦截**：未授权时 `/api/opportunity-types` → 403（`code=license_required`，
   带 `license_state`）；`/api/license/*`、`/api/status`、`/` 均放行。
8. **激活后**：同一接口恢复 200，且运行时回调被调用一次。

---

## 五、给客户启用授权（三种方式，任选）

```bat
:: 1) 命令行参数（推荐的交付启动脚本）
python run.py --license

:: 2) 环境变量
set KEFU_LICENSE_ENABLED=1
python run.py

:: 3) 写进配置库（一次生效，长期有效）
::    设置页暂未提供开关，用 sqlite 直接改：
::    INSERT INTO settings(key,value) VALUES('license.enabled','1')
::    ON CONFLICT(key) DO UPDATE SET value='1';
```

授权服务器地址（默认 `http://127.0.0.1:8799`）：

```bat
set KEFU_LICENSE_SERVER=https://license.yourdomain.com
```

优先级：环境变量 > 数据库设置 > 默认值。

**默认关闭**是刻意设计的：开发/自用/跑测试都不受影响。交付包记得带
`--license`，否则授权形同虚设。

---

## 六、正式上线前必做（4 件）

1. **换密钥对**：`python tools/gen_license_keys.py --out tools/license_keys --key-id prod-2026`
   → 把打印的 base64 公钥粘进 `app/license.py` 的 `PUBLIC_KEYS`
   → 私钥只放授权服务器（**不要**进客户端包/仓库）。
2. **改服务器地址**：`app/config.py` 的 `DEFAULT_LICENSE_SERVER_URL` 或部署时
   设 `KEFU_LICENSE_SERVER`（必须 HTTPS）。
3. **开发正式后台**：按 `docs/授权服务接口文档.md` 实现
   `/api/v1/license/activate`、`/heartbeat`（+ 可选 `/query`）与管理端。
   参考实现直接看 `tools/mock_license_server.py`。
4. **管理端加鉴权**：mock 的 `/admin/*` 无鉴权，正式必须登录+审计。

---

## 七、未做 / 已知边界

- **未做设置页开关**：启用授权靠 `--license` / 环境变量 / 配置库（避免客户
  在界面上误关把自己锁死）。
- **多机授权不支持**：当前 1 机 1 证。要做需要 payload 里 `machine_code` 改
  数组 + 客户端校验逻辑调整（后续版本）。
- **整盘 Ghost 到多台机器**能绕过（MachineGuid + 卷序列号都一样）。只能靠
  服务端策略（并发限制/异常 IP 告警）发现，客户端指纹永远可能被绕过。
- **重装系统会变机器码**（MachineGuid 变）→ 需重新授权。管理端按客户名查找
  重新发码即可。
- 客户改系统时间只能挡住"调早 1 天以上"这种粗暴操作；真正的权威是服务器心跳。

---

## 八、联调怎么跑（本机已验证）

```bash
# 终端 A：模拟授权服务器（管理台 http://127.0.0.1:8799/admin）
cd kefu-system
python tools/mock_license_server.py --port 8799

# 终端 B：带授权启动客户端
cd kefu-system
python run.py --license --port 8766

# 浏览器打开 http://127.0.0.1:8766
#   → 全屏遮罩显示机器码（复制）
#   → 管理台「生成激活码」
#   → 粘贴 → 激活 → 遮罩消失，抓取/分析/发送自动启动（无需重启）
```

命令行快速造码：`python tools/mock_license_server.py --issue-code 365`

---

## 九、补丁：吊销过的机器无法重新授权（2026-11-13 21:50 现场）

**现象**：客户机返回「激活失败：该机器码的授权已被吊销」，换后台任意一个
未使用的激活码都不行。

**根因**（`tools/mock_license_server.py` 的 `activate`）：已吊销记录的判断
放在**使用激活码之前**，只要这台机有一条 `revoked_at` 记录就直接 400，
不管新码是否可用 → 供应商无法重新授权同一台机（返修/退款/重新购买全卡死）。
另外 `find_license` 按插入顺序返回，旧吊销记录会一直盖住后来的新记录。

**修复**（规则写进 `docs/授权服务接口文档.md` 5.1）：
- 已吊销 + **未使用**的激活码 → 新建许可证（新 `license_id`，`reissued_from`
  记录原许可证号），旧记录保留作历史；
- 已吊销 + **该机器用过的旧码** → 仍报 `revoked`，提示重新发码（防止客户拿
  吊销前的码自助解封）；
- `find_license` 改为**最新优先**；新增 `Store.active_for_machine()`；
- 管理台新增「恢复」（撤销误吊销，接口 `POST /admin/api/licenses/{id}/restore`，
  若该机器已有生效授权则 409 `already_active`，因为一台机两条生效记录会让
  "按机器码查"结果不可预测）；
- 客户端 `_ERROR_HINTS["revoked"]` 文案改为「旧激活码不能重复使用，请让供应商
  重新发一个激活码」。

**测试**：`tests/test_license_server_rules.py` 8 例（新码激活、跨机 code_used、
同机叠加、吊销心跳、吊销后旧码拒绝、吊销后新码放行、恢复、已有生效授权时拒绝恢复）。

---

## 十、澄清：「管理台已吊销 / 客户端正常」不是矛盾

同一台机器在管理台里**会有多条记录**（吊销重发、换发都会留档），列表只看
时间容易误判——本例旧证 `LIC-20261113-0F0E` 到期 2027-12-13 且已吊销，
而客户端实际持有的是 `LIC-20261113-7924`（2026-12-13，30 天）。

为此加了 `annotate_licenses()`（仅管理接口返回时计算，不落盘）：

| role | 含义 | 操作按钮 |
|---|---|---|
| `active` | 该机器**当前生效**的许可证（唯一） | 延期 / 吊销 |
| `superseded` | 同机更早的、被新证取代的 | 无（灰显） |
| `revoked` | 已吊销；`can_restore=false` 表示该机已有新证，不能复活 | 仅 `can_restore` 时给「恢复」 |

客户端侧新增 `LicenseStatus.last_error`：联网确认失败（含服务端 404 无记录）
会带到 `/api/license/status` 与界面告警。**注意语义**：本地签名许可证仍是
唯一依据，`last_error` 只提示"两侧可能不一致"，不自动锁死（避免服务器数据库
回滚导致全体客户误锁）。

排查口诀：管理台看 `role=active` 那行；客户端看「许可证号」是否等于它。

---

## 十一、下线「离线导入许可证」（2026-11-13 22:30）

**决策**：客户机跑本系统必须有网络（微信本身要联网），离线签发没有意义，
去掉这条授权方式，减少一条客户用不上的路径。

**删掉的东西**

| 位置 | 删除内容 |
|---|---|
| `app/api/router_license.py` | `POST /api/license/import` 路由 + `Body`/`LicenseImportIn` 导入 |
| `app/license.py` | `LicenseClient.import_license_file()` |
| `app/schemas.py` | `LicenseImportIn` |
| `app/api/license_guard.py` | 白名单里的 `/api/license/import`（前缀 `/api/license/` 仍放行，未匹配即 404） |
| `frontend/src/pages/LicensePanel.tsx` | 「导入许可证文件」按钮、file input、`importFile` 变更、`UploadFileIcon` |

**保留**：激活码激活、联网刷新、离线宽限（断网 7 天内照常可用——这是"容忍
网络抖动"，与"离线签发"是两件事，不要一起删）。

**测试**：新增 `test_import_endpoint_removed`（路由表无该路径 + 请求 404）；
原先用 import 建状态的 3 个用例改为直接写 `license.json`。全量 **1037 passed**。

## 十二、文案：设置页入口按钮改名

`设置 → 授权` 的按钮原本写「查看授权详情」，但点开就是激活码输入框，与
预期不符 → 改为「**验证授权码**」（未授权时仍显示「输入激活码」）。顶栏 Chip
的 Tooltip 同步改成「点击打开授权窗口」。

---

## 十三、排障：「改了 config.py 的 license.enabled 仍显示开发模式」

**根因**：`DEFAULT_SETTINGS` 只在**建库时**用 `INSERT OR IGNORE` 播种（`db.py`
约 363 行），已有数据库里那行 `license.enabled`（多为 `"0"`）会永久盖住代码
默认值。生效顺序是 **环境变量 `KEFU_LICENSE_ENABLED` > settings 表 > config 默认值**。
本机库里就是 13:27 建库时播种的 `0`，所以把 config.py 改成 `"1"` 后重启无效。

**已做**
1. `LicenseClient.enabled_source()` → `env` / `db` / `default`；`LicenseStatus`
   新增 `enabled_source` 字段，`disabled` 状态的 message 直接写出"开关来自
   XXX"；schemas / types.ts / 设置页同步展示。
2. 设置页「授权」标签在未启用时把启用方式写成三条（`--license` / 环境变量 /
   改 settings 表），并明确提示"改 config.py 只对新建数据库生效"。
3. `config.py` 两处注释补同样的警告（避免下一个人再踩）。
4. 本机数据库 `license.enabled` 已改为 `1`（用户意图），实测
   `enabled=True / state=active / source=db / 剩 29 天`。
5. 测试：`test_status_disabled_by_default` 改为
   `test_status_disabled_when_db_says_off`（显式写 db=0 并断言 source=db），
   新增 `test_enabled_source_env`。全量 **1038 passed**。

**注意**：`DEFAULT_SETTINGS[license.enabled]` 现在是 `"1"`（用户改的）——
意味着**全新数据库默认就要授权**，新环境首次 `python run.py` 会看到激活遮罩。
自用/开发要跳过，把 settings 表该行改回 `0` 即可（`--license` 只有开没有关，
没有 `--no-license` 参数，别去找）。

---

## 十四、加固：默认强制授权 + 测试口令（2026-11-13 23:00）

**问题**（用户提出）：原来 `license.enabled` 是 `1/0` 开关，客户打开数据库或
配置文件把它改成 `0` 就进开发模式了，等于没有授权。

### 新的判定规则（v2，fail-closed）

`app/license.py → LicenseClient.mode()`，按顺序：

1. 环境变量 `KEFU_LICENSE_MODE=enforce`（`1/on/yes/true` 同义）→ **强制授权**，
   后面都不看。交付脚本带 `--license` 就会设它。
2. `license.enabled` 的取值（环境变量 `KEFU_LICENSE_ENABLED` > settings 表 >
   config 默认 `"1"`）**恰好等于测试口令**（明文，或其 SHA256 指纹）→ 测试模式。
3. **其余一切取值**（`1`/`0`/`false`/`off`/空/乱写）→ 需要授权。

所以改库为 `0` 只是「写了句废话」，不再解除授权。

### 口令怎么存

- 明文：`cheche123456@`（只写在本文档，**不写进 kefu-system/ 任何文件**）。
- 代码里只有 `DEV_TOKEN_SHA256 = 8528a399…1c7f`（
  `sha256("kefu-license-dev-token-v1|" + 明文)`）；界面/日志用前 8 位
  `8528a399` 做「口令指纹」标识。
- 客户即使拿到源码或反编译，也搜不到明文，只能暴力猜。

### 三条命令（`run.py`）

| 命令 | 作用 |
|---|---|
| `python run.py --license` | 本次启动强制授权（写 `KEFU_LICENSE_MODE=enforce`）；**交付脚本必带** |
| `python run.py --license-dev 口令` | 切测试模式（写指纹到 settings 表）后退出；**口令不对直接 exit 2** |
| `python run.py --license-on` | 切回强制授权（写 `1`）后退出 |

`--license-dev` 必须现场出示口令：客户拿到同一份程序也跑不了这条命令，
否则「本机跑个命令就免授权」等于没堵。已实测：无口令/错口令 exit 2，
正确口令写入指纹，`--license-on` 写回 1。

### 测试模式有多显眼

- 顶栏红色 Chip「测试模式」；
- 页面顶部红色横幅「当前是厂商测试模式…交付客户前请执行
  `python run.py --license-on`」；
- 设置页「授权」标签整块红色 Alert；
- 每次启动 `WARNING ⚠ 授权校验处于【厂商测试模式】…`。

交付件里出现任何一条 = 忘了切回来。

### 改动清单

- `app/license.py`：新增 `TOKEN_SALT` / `DEV_TOKEN_SHA256` / `DEV_TOKEN_PREFIX`
  / `FORCE_ENV` / `token_digest()` / `is_dev_token()` / `force_enforced()`；
  `LicenseClient.mode()`；`enabled()` 改为 `mode() != "dev"`；
  `enabled_source()` 新增 `env-force`；`LicenseStatus.dev_mode`；
  `disabled` 文案改为「厂商测试模式」。
- `app/config.py`：注释改写（默认值仍是 `"1"`）。
- `app/main.py`：测试模式启动打 WARNING。
- `app/schemas.py`：`LicenseStatusOut.dev_mode`。
- `run.py`：`--license` 改写 `KEFU_LICENSE_MODE`；新增 `--license-dev 口令` /
  `--license-on`；`_write_saved_setting()`。
- 前端：`types.ts` 加 `dev_mode`；`LicenseChip` 测试模式红标；
  `App.tsx` 顶部横幅；`LicensePanel` 设置页红色 Alert + 说明改写。
- 文档：`docs/授权服务接口文档.md` 第 10 节新增「授权开关（v2）」表 + 命令 +
  「客户改库为 0 会不会免授权」问答。
- 测试：`test_license_core.py` 新增 4 例（口令/0 不等于关/明文与指纹等价/
  env-force 优先），`test_license_api.py` 改写 1 例 + 新增 1 例，
  新增 `tests/test_license_mode_switch.py`（6 例）。

### 验证

- 真实库副本逐项实测：`1`/`0`/`false`/空 全部 `enabled=True`；
  指纹与明文都得到 `dev=True`；`KEFU_LICENSE_MODE=enforce` 把明文压回强制。
- 命令行实测（真实库，已复原）：无口令 exit 2、错口令 exit 2、正确口令 exit 0
  并写入指纹、`--license-on` 写回 `1`。
- 全量 pytest **1048 passed**；`tsc --noEmit` 0 错误；前端产物含「测试模式」、
  不含口令明文。

### 仍然做不到的（强度要求不高，已知边界）

- 程序是 Python 源码交付，铁了心的人可以直接改 `app/license.py` 里的判断。
  要真正防住得上 PyInstaller/Nuitka 打包 + 代码混淆，或把核心逻辑放服务端。
- `license.json` 仍需 Ed25519 签名，那个才是防伪造许可证的主防线；本次只是
  把「改配置免授权」这条最容易走的路堵死。

---

## 十五、Bug：Ctrl+C 退出卡在 "Shutting down"（2026-11-13 23:40）

**现场**：控制台停了 uvicorn 的 `Shutting down`，但 `[wechatauto] poll #NNN`
还在刷、进程一直不退出，两次。

**实测结论（有活体证据）**：用户那次的进程 **PID 16376 到现在还活着**
（`D:/my_conda_envs/wechatauto/python.exe`，22:49 启动，累计 CPU 2000+ 秒），
8766 早已不监听、但库还在被写、还有 443 的 AI 连接——也就是说 **uvicorn 进了
shutdown，但 lifespan 的 shutdown 回调压根没执行**，所以停监听、刷 watermark、
关库全都没发生，微信监听线程照旧轮询（就是你看到的 poll 日志）。

### 根因（两层，都在 uvicorn 的默认行为上）

1. **`timeout_graceful_shutdown` 默认 `None` = 无限等**。
   uvicorn 0.54 `Server.shutdown()` 的顺序是：
   `log "Shutting down"` → 关监听 socket → 逐个 `connection.shutdown()` →
   `await wait_for(_wait_tasks_to_complete(), timeout=None)` →
   **之后**才 `await lifespan.shutdown()`。
   只要有一个**同步请求卡在 AnyIO 线程池里**（图片解密、UIA 发送、视觉调用…
   任何一个卡住的 endpoint 都算），`server_state.tasks` 永远不空，
   于是永远等下去，lifespan 的关闭逻辑一次都不跑。
   复现脚本 `D:/tmp/uvicorn_ws_shutdown_repro.py`（uvicorn 0.54）实测：
   - 空闲：0.2s 退出
   - 开着 `/ws/events` 长连接：0.2s 退出（WS 不是元凶，uvicorn 会强断）
   - **一个卡住的同步请求：20s 还不返回**（无限等）
   - 同上 + `timeout_graceful_shutdown=5`：5.2s 返回
2. 就算 uvicorn 顺利返回，**解释器退出还要等非 daemon 线程**。
   AnyIO 线程池的 worker 是 non-daemon 的；卡在 `time.sleep`/socket 上的
   worker 会让进程停在 `threading._shutdown()`（栈快照里就是
   `threading.py:1624 in _shutdown → lock.acquire()`）。这一层同样表现为
   "窗口没关、进程还在"。

### 修复（`run.py`）

1. `uvicorn.Config(..., timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC=5)`
   —— 5 秒还没收尾的请求直接 cancel，shutdown 继续往下走。
2. 不再用 `uvicorn.run()`，改成 `_KefuServer(uvicorn.Server)` 子类：
   重写 `handle_exit()`，**先挂看门狗**再交给 uvicorn 处理信号。
3. `os._exit(code)` 收尾：此时 lifespan shutdown 已跑完（监听已停、watermark
   已刷回、库已关），剩下的都是 daemon/第三方（UIA、COM）线程，直接结束，
   不再走 `threading._shutdown()` 去等那些可能永远等不到的 worker。
4. 看门狗 `_start_exit_watchdog()`：Ctrl+C 后 20 秒还没退出 →
   `_dump_all_threads()` 抓**所有线程的 Python 栈**（纯标准库，不需要 py-spy），
   打印到控制台 + 写 `data/shutdown-dump-<时间>.log`，然后 `os._exit(130)`。
   下次再卡，控制台里直接就有卡在哪个函数上。

### 验证

- 单元测试 `tests/test_shutdown_watchdog.py`（8 例）：栈快照含主线程且排第一、
  其它线程可见、落盘文件名、写入失败返回空串、看门狗超时调用 `exit_fn(130)`、
  只启一次、`handle_exit` 先挂看门狗再置 `should_exit`、常量必须有限值。
- 端到端（conda 环境 uvicorn 0.54 + 真正卡住的同步请求）：
  `Ctrl+C → 5.2s 后 run() 返回 → os._exit(0)`，总耗时 9.7s（含 4.5s 前置等待），
  进程立刻结束；线程栈快照里能直接看到卡在 `AnyIO worker thread → block()`。
- 全量 pytest 见下一条。

### 仍要注意

- 卡住的 endpoint 被 cancel 后，它的线程还在跑（Python 杀不掉线程），如果它
  正在写库，可能与 `db.close()` 抢锁并留下一条 sqlite 错误日志。可接受——
  总比进程永远不退、微信监听一直跑（继续调 AI 花钱）好。
- 用户那次的僵尸进程要手工结束：`taskkill /PID <pid> /F`。
