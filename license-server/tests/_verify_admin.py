# -*- coding: utf-8 -*-
"""真实库管理员体检（只读，绝不修改）：验证交付态账号是否可用。

用法：``python tests/_verify_admin.py``
输出全部为布尔值 / 计数（不回显口令，避免终端改写字符串造成误判）。
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql                                   # noqa: E402
from pymysql.cursors import DictCursor           # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from _helpers import ADMIN_PASSWORD, ADMIN_USER, _env_file_value  # noqa: E402
from app import security                         # noqa: E402
from app.config import get_config                # noqa: E402


def main() -> None:
    cfg = get_config()
    real_name = _env_file_value("LS_DB_NAME") or "wechat_kefu"
    conn = pymysql.connect(
        host=cfg.db_host, port=cfg.db_port, user=cfg.db_user,
        password=cfg.db_password, database=real_name, charset=cfg.db_charset,
        autocommit=True, cursorclass=DictCursor,
        connect_timeout=cfg.db_connect_timeout)
    with conn.cursor() as cur:
        cur.execute("SELECT username, password_hash FROM admin_user")
        admins = list(cur.fetchall())
        cur.execute("SELECT COUNT(*) c FROM license")
        lic = int(cur.fetchone()["c"])
        cur.execute("SELECT COUNT(*) c FROM license_code")
        code = int(cur.fetchone()["c"])
        cur.execute("SELECT COUNT(*) c FROM customer")
        cust = int(cur.fetchone()["c"])
    conn.close()

    print("真实库:", real_name, "| admin 行数:", len(admins))
    for row in admins:
        ok = security.verify_password(ADMIN_PASSWORD, row["password_hash"])
        print("账号:", row["username"], "| 密码 == 交付态口令:", ok,
              "| 口令 sha256[:16]:",
              hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()[:16])
    print("业务数据量 license=%d code=%d customer=%d（测试不应改动它们）"
          % (lic, code, cust))
    assert real_name.endswith("kefu"), "意外库名，检查 .env"
    _ = ADMIN_USER


if __name__ == "__main__":
    main()
