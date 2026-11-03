"""Change the built-in demo admin password inside the mounted SQLite DB."""
from __future__ import annotations

import getpass
import hashlib
import sqlite3

from app.database import DB_PATH


def main() -> None:
    print(f"数据库: {DB_PATH}")
    pwd = getpass.getpass("输入新的 admin 密码（至少 12 位，输入时不显示）: ")
    if len(pwd) < 12:
        raise SystemExit("密码不足 12 位，未修改")
    repeat = getpass.getpass("再次输入新密码: ")
    if pwd != repeat:
        raise SystemExit("两次密码不一致，未修改")
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = 'admin'",
            (hashlib.sha256(pwd.encode('utf-8')).hexdigest(),),
        )
        if result.rowcount != 1:
            raise SystemExit("没有找到 admin 用户，未修改")
    print("admin 密码已更新。注意：本项目自身仍未实现完整的身份鉴权。")


if __name__ == "__main__":
    main()
