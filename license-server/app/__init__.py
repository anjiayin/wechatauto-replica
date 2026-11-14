# -*- coding: utf-8 -*-
"""授权服务后台（license-server）应用包。

分层：
    config  —— 配置（env > .env > 默认值）
    errors  —— 统一错误体与异常处理器
    db      —— 极简 MySQL 连接池 + 查询封装
    signing —— Ed25519 签名 / 时间工具（payload_bytes 唯一实现处）
    security—— 密码哈希、会话令牌、CSRF、Cookie、客户端 IP
    models  —— Pydantic 请求模型
    repo    —— 数据访问（原生 SQL，MySQL 5.6 兼容）
    services—— 业务编排（客户端授权 / 管理端操作）
    api     —— FastAPI 路由
    static  —— 管理台静态页面（/admin）
"""

__version__ = "1.0.0"
