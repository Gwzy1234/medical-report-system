"""
app/database.py
===============================================================================
SQLite 3 数据库连接与数据访问封装（纯标准库，不依赖 ORM）

设计要点：
1. 每次操作打开一个独立连接，避免 FastAPI 多线程下共享连接导致
   "SQLite objects created in a thread can only be used in that same thread"；
2. 连接统一开启外键约束和 busy_timeout，写冲突时自动等待；
3. 所有 SQL 一律使用参数化占位符（?），防止 SQL 注入；
4. 查询结果统一转换为 dict，方便 FastAPI 直接序列化为 JSON；
5. 数据库文件与建表脚本均基于 __file__ 定位，不依赖启动目录。

手动初始化数据库（可选）：
    python -m app.database
===============================================================================
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # 项目根目录
SQL_FILE = BASE_DIR / "sql" / "init_db.sql"                 # 建表脚本

# 默认数据库放在项目根目录；可通过环境变量 MEDICAL_DB_PATH 覆盖（便于测试）
_env_db_path = os.getenv("MEDICAL_DB_PATH", "").strip()
DB_PATH: Path = (
    Path(_env_db_path).expanduser().resolve()
    if _env_db_path
    else BASE_DIR / "medical_report.db"
)


# ---------------------------------------------------------------------------
# 密码工具：按题目要求使用 hashlib.sha256
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """返回密码的 SHA-256 十六进制摘要。"""
    return hashlib.sha256((password or "").encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """恒定时间比较密码摘要，避免简单的时序侧信道。"""
    return hmac.compare_digest(hash_password(password), password_hash or "")


# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------
@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """
    获取数据库连接的上下文管理器。

    用法：
        with get_connection() as conn:
            conn.execute(...)

    正常退出自动 commit，异常退出自动 rollback，最后一定 close。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """执行 sql/init_db.sql，创建表、索引并写入初始提示词。"""
    if not SQL_FILE.exists():
        raise FileNotFoundError(f"数据库初始化脚本不存在：{SQL_FILE}")
    sql = SQL_FILE.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.executescript(sql)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


# ===========================================================================
# 一、用户模块
# ===========================================================================
def create_user(username: str, password: str) -> Dict[str, Any]:
    """
    注册新用户。

    :raises ValueError: 用户名为空、密码为空或用户名已存在
    :return: {"id", "username", "created_at"}
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if not password:
        raise ValueError("密码不能为空")

    password_hash = hash_password(password)
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if exists:
            raise ValueError("用户名已存在")

        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, password_hash),
            )
        except sqlite3.IntegrityError:                      # 并发注册兜底
            raise ValueError("用户名已存在") from None

        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)  # type: ignore[arg-type]


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    校验用户名和密码。

    :return: 校验成功返回 {"id", "username", "created_at"}，失败返回 None
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "created_at": row["created_at"],
    }


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """按主键查询用户（不返回密码摘要）。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """按用户名查询用户（不返回密码摘要）。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE username = ?",
            ((username or "").strip(),),
        ).fetchone()
    return _row_to_dict(row)


# ===========================================================================
# 二、提示词配置模块
# ===========================================================================
def list_prompt_configs(active_only: bool = False) -> List[Dict[str, Any]]:
    """查询全部提示词配置，active_only=True 时只返回启用项。"""
    sql = "SELECT id, category, system_prompt, is_active FROM prompt_configs"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id ASC"
    with get_connection() as conn:
        return _rows_to_dicts(conn.execute(sql).fetchall())


def get_prompt_by_category(
    category: str, active_only: bool = True
) -> Optional[Dict[str, Any]]:
    """
    按检查分类查询 System Prompt。

    :param active_only: 为 True 时只匹配 is_active=1 的配置
    """
    category = (category or "").strip()
    if not category:
        return None

    sql = "SELECT * FROM prompt_configs WHERE category = ?"
    if active_only:
        sql += " AND is_active = 1"

    with get_connection() as conn:
        row = conn.execute(sql, (category,)).fetchone()
    return _row_to_dict(row)


def upsert_prompt_config(
    category: str, system_prompt: str, is_active: int = 1
) -> Dict[str, Any]:
    """
    新增或更新某个分类的提示词（管理员配置用）。

    依赖 SQLite 3.24+ 的 UPSERT 语法；Python 3.9 自带的 SQLite 均满足。
    """
    category = (category or "").strip()
    system_prompt = (system_prompt or "").strip()
    if not category:
        raise ValueError("检查分类不能为空")
    if not system_prompt:
        raise ValueError("System Prompt 不能为空")

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO prompt_configs (category, system_prompt, is_active)
            VALUES (?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET
                system_prompt = excluded.system_prompt,
                is_active     = excluded.is_active
            """,
            (category, system_prompt, 1 if is_active else 0),
        )
        row = conn.execute(
            "SELECT * FROM prompt_configs WHERE category = ?", (category,)
        ).fetchone()
    return dict(row)  # type: ignore[arg-type]


def set_prompt_active(category: str, is_active: bool) -> bool:
    """启用 / 停用某个分类的提示词，返回是否命中记录。"""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE prompt_configs SET is_active = ? WHERE category = ?",
            (1 if is_active else 0, (category or "").strip()),
        )
    return cur.rowcount > 0


# ===========================================================================
# 三、报告模板模块
# ===========================================================================
def create_template(
    title: str,
    category: str,
    findings: str = "",
    conclusion: str = "",
    is_ai_generated: int = 0,
    creator_id: Optional[int] = None,
) -> Dict[str, Any]:
    """新建报告模板。"""
    title = (title or "").strip()
    category = (category or "").strip()
    if not title:
        raise ValueError("模板标题不能为空")
    if not category:
        raise ValueError("检查分类不能为空")

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO templates
                (title, category, findings, conclusion, is_ai_generated, creator_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                category,
                findings or "",
                conclusion or "",
                1 if is_ai_generated else 0,
                creator_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)  # type: ignore[arg-type]


def list_templates(
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    creator_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    查询模板列表，支持分类筛选、关键词模糊搜索、创建人筛选。
    """
    where: List[str] = []
    params: List[Any] = []

    if category:
        where.append("category = ?")
        params.append(category.strip())
    if creator_id is not None:
        where.append("creator_id = ?")
        params.append(creator_id)
    if keyword:
        like = f"%{keyword.strip()}%"
        where.append("(title LIKE ? OR findings LIKE ? OR conclusion LIKE ?)")
        params.extend([like, like, like])

    sql = "SELECT * FROM templates"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, limit), max(0, offset)])

    with get_connection() as conn:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())


def get_template(template_id: int) -> Optional[Dict[str, Any]]:
    """按主键查询模板。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    return _row_to_dict(row)


def delete_template(template_id: int) -> bool:
    """删除模板；已引用它的报告会因 ON DELETE SET NULL 保留正文。"""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    return cur.rowcount > 0


# ===========================================================================
# 四、报告模块
# ===========================================================================
_REPORT_DETAIL_SQL = """
    SELECT r.*,
           t.title    AS template_title,
           u.username AS doctor_name
      FROM reports r
      LEFT JOIN templates t ON t.id = r.template_id
      LEFT JOIN users     u ON u.id = r.doctor_id
"""


def create_report(
    patient_name: str,
    findings: str,
    conclusion: str,
    template_id: Optional[int] = None,
    patient_gender: str = "未知",
    patient_age: Optional[int] = None,
    patient_record_no: str = "",
    doctor_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    保存一份患者报告（findings / conclusion 为生成时的内容快照）。

    :raises ValueError: 必填项为空或年龄不合法
    """
    patient_name = (patient_name or "").strip()
    findings = (findings or "").strip()
    conclusion = (conclusion or "").strip()

    if not patient_name:
        raise ValueError("患者姓名不能为空")
    if not findings:
        raise ValueError("影像所见不能为空")
    if not conclusion:
        raise ValueError("诊断意见不能为空")
    if patient_age is not None and not (0 <= int(patient_age) <= 150):
        raise ValueError("患者年龄必须在 0 ~ 150 之间")

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO reports
                (template_id, patient_name, patient_gender, patient_age,
                 patient_record_no, findings, conclusion, doctor_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                patient_name,
                (patient_gender or "未知").strip(),
                int(patient_age) if patient_age is not None else None,
                (patient_record_no or "").strip(),
                findings,
                conclusion,
                doctor_id,
            ),
        )
        row = conn.execute(
            _REPORT_DETAIL_SQL + " WHERE r.id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)  # type: ignore[arg-type]


def list_reports(
    doctor_id: Optional[int] = None,
    keyword: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    查询报告列表（含模板标题、医生姓名）。

    :param keyword: 按患者姓名或病历号模糊搜索
    """
    where: List[str] = []
    params: List[Any] = []

    if doctor_id is not None:
        where.append("r.doctor_id = ?")
        params.append(doctor_id)
    if keyword:
        like = f"%{keyword.strip()}%"
        where.append("(r.patient_name LIKE ? OR r.patient_record_no LIKE ?)")
        params.extend([like, like])

    sql = _REPORT_DETAIL_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, limit), max(0, offset)])

    with get_connection() as conn:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())


def get_report(report_id: int) -> Optional[Dict[str, Any]]:
    """按主键查询报告基础信息（不含关联名称）。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_report_detail(report_id: int) -> Optional[Dict[str, Any]]:
    """
    按主键查询报告详情，附带 template_title、doctor_name，
    供 PDF 导出和详情展示使用。
    """
    with get_connection() as conn:
        row = conn.execute(
            _REPORT_DETAIL_SQL + " WHERE r.id = ?", (report_id,)
        ).fetchone()
    return _row_to_dict(row)


def delete_report(report_id: int) -> bool:
    """删除报告，返回是否命中记录。"""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    return cur.rowcount > 0


# ===========================================================================
# 命令行入口：python -m app.database
# ===========================================================================
if __name__ == "__main__":
    init_db()
    print(f"数据库初始化完成：{DB_PATH}")
    print(f"提示词分类数量：{len(list_prompt_configs(active_only=True))}")
