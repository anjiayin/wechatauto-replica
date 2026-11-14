-- ===========================================================================
-- 授权服务后台建表脚本（MySQL 5.6 兼容，全部幂等：CREATE TABLE IF NOT EXISTS）
--
-- 硬约束（照抄设计文档 §3.3，勿改）：
--   * 无 JSON 列（用 TEXT 存 JSON 字符串）、无窗口函数、无 CTE；
--   * 全部 utf8mb4 索引列 <= 191 字符（Antelope + 767 字节上限）；
--   * 时间列不使用 DEFAULT CURRENT_TIMESTAMP，一律由 Python 写入东八区 naive 时间；
--   * TEXT 列不设默认值（5.6 不允许）。
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. 管理员账号
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_user (
  id                  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  username            VARCHAR(32)  NOT NULL                COMMENT '登录名（小写，唯一）',
  password_hash       VARCHAR(255) NOT NULL                COMMENT 'pbkdf2_sha256$迭代次数$base64(salt)$base64(dk)',
  display_name        VARCHAR(64)  NOT NULL DEFAULT ''      COMMENT '显示名',
  role                VARCHAR(16)  NOT NULL DEFAULT 'admin' COMMENT 'admin=全部权限（本版仅一种）',
  status              TINYINT      NOT NULL DEFAULT 1       COMMENT '1=启用 0=停用',
  must_change_password TINYINT     NOT NULL DEFAULT 0       COMMENT '1=首登/重置后必须改密',
  failed_count        INT          NOT NULL DEFAULT 0       COMMENT '连续登录失败次数',
  locked_until        DATETIME     NULL     DEFAULT NULL    COMMENT '锁定到期时间（NULL=未锁）',
  last_login_at       DATETIME     NULL     DEFAULT NULL,
  last_login_ip       VARCHAR(45)  NOT NULL DEFAULT ''      COMMENT '兼容 IPv6',
  password_updated_at DATETIME     NULL     DEFAULT NULL,
  created_at          DATETIME     NOT NULL,
  updated_at          DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='管理端账号';

-- ---------------------------------------------------------------------------
-- 2. 管理端会话（令牌只存 sha256，库泄露也无法冒充）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_session (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  token_hash     CHAR(64)    NOT NULL              COMMENT 'sha256(token) 十六进制，仅存哈希',
  csrf_token     CHAR(32)    NOT NULL              COMMENT 'CSRF 双提交令牌（随机 32 hex）',
  user_id        INT UNSIGNED NOT NULL,
  username       VARCHAR(32) NOT NULL DEFAULT ''   COMMENT '冗余，便于日志免查',
  ip             VARCHAR(45) NOT NULL DEFAULT '',
  user_agent     VARCHAR(255) NOT NULL DEFAULT '',
  created_at     DATETIME    NOT NULL,
  last_active_at DATETIME    NOT NULL,
  expires_at     DATETIME    NOT NULL,
  revoked_at     DATETIME    NULL DEFAULT NULL     COMMENT '登出/改密踢人时写入',
  PRIMARY KEY (id),
  UNIQUE KEY uk_token (token_hash),
  KEY idx_user (user_id),
  KEY idx_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='管理端会话';

-- ---------------------------------------------------------------------------
-- 3. 客户档案（手机号作为业务主键，一客户多台机器）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
  name       VARCHAR(64)  NOT NULL DEFAULT ''   COMMENT '客户名称',
  phone      VARCHAR(32)  NOT NULL              COMMENT '手机号（唯一业务键，不允许空串入档）',
  note       VARCHAR(255) NOT NULL DEFAULT '',
  created_at DATETIME     NOT NULL,
  updated_at DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_phone (phone),
  KEY idx_name (name(32))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='客户档案';

-- ---------------------------------------------------------------------------
-- 4. 激活码
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS license_code (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code           VARCHAR(32)  NOT NULL              COMMENT 'KF-XXXX-XXXX-XXXX-XXXX',
  days           INT          NOT NULL              COMMENT '兑换后的有效天数',
  edition        VARCHAR(32)  NOT NULL DEFAULT 'standard',
  customer_id    INT UNSIGNED NOT NULL DEFAULT 0    COMMENT '客户档案 id，0=无档案（逻辑外键，不建 FK）',
  customer_name  VARCHAR(64)  NOT NULL DEFAULT ''   COMMENT '生成时快照',
  customer_phone VARCHAR(32)  NOT NULL DEFAULT ''   COMMENT '生成时快照',
  batch          VARCHAR(32)  NOT NULL DEFAULT ''   COMMENT '批次号 B20261114-ABCD，便于按批筛选/复制',
  note           VARCHAR(255) NOT NULL DEFAULT '',
  used_machine   VARCHAR(40)  NOT NULL DEFAULT ''   COMMENT '已绑定机器码，空=未使用',
  used_at        DATETIME     NULL DEFAULT NULL,
  voided_at      DATETIME     NULL DEFAULT NULL     COMMENT '非 NULL=已作废（仅未使用的码可作废；作废后不能再激活）',
  created_by     VARCHAR(32)  NOT NULL DEFAULT ''   COMMENT '生成人 username',
  created_at     DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_code (code),
  KEY idx_used_machine (used_machine),
  KEY idx_voided (voided_at),
  KEY idx_phone (customer_phone),
  KEY idx_batch (batch),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='激活码';

-- ---------------------------------------------------------------------------
-- 5. 许可证（历史全留；current_key 保证一机一证生效）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS license (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  license_id     VARCHAR(32)  NOT NULL              COMMENT 'LIC-yyyymmdd-XXXX，业务唯一号',
  machine_code   VARCHAR(40)  NOT NULL              COMMENT '32 位十六进制 + 7 个横杠 = 39',
  customer_id    INT UNSIGNED NOT NULL DEFAULT 0,
  customer_name  VARCHAR(64)  NOT NULL DEFAULT '',
  customer_phone VARCHAR(32)  NOT NULL DEFAULT '',
  edition        VARCHAR(32)  NOT NULL DEFAULT 'standard',
  activation_code VARCHAR(32) NOT NULL DEFAULT ''   COMMENT '本次签发/续期使用的激活码',
  issued_at      DATETIME     NOT NULL              COMMENT '首次签发时间（心跳不修改）',
  expires_at     DATETIME     NOT NULL              COMMENT '到期时间（续期/缩期只改这一列）',
  grace_days     SMALLINT     NOT NULL DEFAULT 7,
  features       VARCHAR(255) NOT NULL DEFAULT 'monitor,reply,ai' COMMENT '逗号分隔（5.6 无 JSON）',
  revoked_at     DATETIME     NULL DEFAULT NULL     COMMENT '非 NULL = 已吊销',
  superseded_at  DATETIME     NULL DEFAULT NULL     COMMENT '非 NULL = 已被更新的证取代（防御性）',
  reissued_from  VARCHAR(32)  NOT NULL DEFAULT ''   COMMENT '吊销后重发时指向旧 license_id',
  last_seen_at   DATETIME     NULL DEFAULT NULL     COMMENT '最近一次心跳（活跃统计口径）',
  app_version    VARCHAR(32)  NOT NULL DEFAULT '',
  os             VARCHAR(64)  NOT NULL DEFAULT '',
  note           VARCHAR(255) NOT NULL DEFAULT '',
  current_key    VARCHAR(40)  NULL DEFAULT NULL     COMMENT '生效时=机器码，吊销/被取代=NULL（NULL 不参与唯一约束）',
  created_at     DATETIME     NOT NULL,
  updated_at     DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_license_id (license_id),
  UNIQUE KEY uk_current_machine (current_key),
  KEY idx_machine_id (machine_code, id),
  KEY idx_phone (customer_phone),
  KEY idx_customer (customer_id),
  KEY idx_expires (expires_at),
  KEY idx_last_seen (last_seen_at),
  KEY idx_created (created_at),
  KEY idx_revoked (revoked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='许可证（一台机器一条生效记录）';

-- ---------------------------------------------------------------------------
-- 6. 审计日志（每次管理端写操作一条，不删不改）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  actor_id    INT UNSIGNED NOT NULL DEFAULT 0    COMMENT '操作人 id（0=系统/匿名）',
  actor       VARCHAR(32)  NOT NULL DEFAULT ''    COMMENT '操作人 username',
  action      VARCHAR(32)  NOT NULL               COMMENT 'login/logout/change_password/create_codes/extend/set_expiry/revoke/restore/bind_customer/create_customer...',
  target_type VARCHAR(16)  NOT NULL DEFAULT ''    COMMENT 'license/code/customer/admin',
  target_id   VARCHAR(64)  NOT NULL DEFAULT ''    COMMENT 'license_id / code / customer id',
  machine_code VARCHAR(40) NOT NULL DEFAULT ''    COMMENT '关联机器码（便于按机器查历史）',
  days_delta  INT          NOT NULL DEFAULT 0     COMMENT '本次增减天数（缩期为负）',
  before_json TEXT         NULL                   COMMENT '变更前（JSON 字符串；5.6 无 JSON 类型）',
  after_json  TEXT         NULL                   COMMENT '变更后',
  ip          VARCHAR(45)  NOT NULL DEFAULT '',
  note        VARCHAR(255) NOT NULL DEFAULT ''    COMMENT '操作备注/吊销原因',
  created_at  DATETIME     NOT NULL,
  PRIMARY KEY (id),
  KEY idx_target (target_type, target_id),
  KEY idx_machine (machine_code),
  KEY idx_created (created_at),
  KEY idx_action (action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='管理端审计日志';
