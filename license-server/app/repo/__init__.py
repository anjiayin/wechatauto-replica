# -*- coding: utf-8 -*-
"""数据访问层（原生 SQL，全部参数化，MySQL 5.6 兼容）。

每个模块的函数都接受一个**游标**（``pymysql`` 的 ``DictCursor``），
由调用方通过 ``app.db.transaction()`` 控制事务边界；
只读查询也可以直接用 ``app.db.query_one/query_all``。

模块清单：
    customers —— 客户档案（按手机号 upsert）
    codes     —— 激活码（批量生成 / FOR UPDATE 取码 / 标记使用 / 列表）
    licenses  —— 许可证（current_key 维护、延期、吊销、恢复、列表）
    admins    —— 管理员与会话
    audit     —— 审计日志
    stats     —— 统计查询（5.6 兼容写法 + Python 补 0）
"""


def like_pattern(text: str) -> str:
    """把用户输入转成安全的 LIKE 模式（转义 ``\\``、``%``、``_``）。

    MySQL 的 LIKE 默认转义符是反斜杠，因此不需要 ``ESCAPE`` 子句。
    用户输入里的 ``%`` 与 ``_`` 若不转义，会被当成通配符——搜索框输入
    ``%`` 会放大成全表查询（本机库为非严格模式，问题更容易被掩盖）。
    """
    t = str(text or "")
    t = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"
