"""本地 MCP 服务：SQLite 只读查询。

安全策略是两层：
1. 连接用 `file:...?mode=ro` 打开，物理只读；
2. 再装一个 authorizer，只放行 SELECT / READ / FUNCTION 与几种只读 PRAGMA。
写操作、ATTACH、DROP 等一律 `not authorized`，即使 SQL 里藏了也执行不了。

数据库路径按顺序取：
- 环境变量 MCP_SQLITE_DB（绝对或相对路径）
- 容器里的 askdb 样例库 ../askdb/data/sample/shop.db
- 项目内 data/sample/shop.db

运行：
    python -m agent_engine.mcp_servers.sqlite
    MCP_SQLITE_DB=/path/to.db python -m agent_engine.mcp_servers.sqlite --transport sse --port 8032
"""
import os
import sqlite3
import time
import urllib.parse
from pathlib import Path

from agent_engine.mcp_server import run_cli

from .common import PROJECT_ROOT, make_toolbox

SERVER_NAME = "local-sqlite"
SERVER_VERSION = "1.0.0"
SERVER_INSTRUCTIONS = (
    "对本地 SQLite 数据库做只读查询：列出表、查看表结构、执行 SELECT。"
    "写操作与 ATTACH/PRAGMA 等会被拒绝。"
)

REGISTRY, tool = make_toolbox()

QUERY_TIMEOUT = 8.0
MAX_ROWS = 200
MAX_CELL_CHARS = 500

_DB_CANDIDATES = [
    PROJECT_ROOT.parent / "askdb" / "data" / "sample" / "shop.db",
    PROJECT_ROOT / "data" / "sample" / "shop.db",
    PROJECT_ROOT / "sample.db",
]

_ALLOWED_PRAGMAS = {
    "table_info", "table_xinfo", "index_list", "index_info",
    "foreign_key_list", "database_list",
}


def db_path() -> Path:
    """解析数据库路径；不存在时给出可操作的错误。"""
    configured = os.environ.get("MCP_SQLITE_DB", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        path = next((p for p in _DB_CANDIDATES if p.exists()), None)
        if path is None:
            raise FileNotFoundError(
                "未配置数据库：请设置环境变量 MCP_SQLITE_DB 指向一个 .db 文件。"
                f"已尝试：{', '.join(str(p) for p in _DB_CANDIDATES)}"
            )
    if not path.exists():
        raise FileNotFoundError(f"数据库文件不存在：{path}")
    return path


def _connect() -> sqlite3.Connection:
    path = db_path()
    uri = "file:" + urllib.parse.quote(str(path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=QUERY_TIMEOUT)

    def authorizer(action, arg1, arg2, dbname, source):
        allow = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        }
        for name in ("SQLITE_RECURSIVE", "SQLITE_TRANSACTION"):
            value = getattr(sqlite3, name, None)
            if value is not None:
                allow.add(value)
        if action in allow:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _ALLOWED_PRAGMAS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)

    deadline = time.monotonic() + QUERY_TIMEOUT
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    return conn


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@tool
def list_tables() -> str:
    """列出数据库里的表与视图（不含 sqlite_ 内部表）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()

    if not rows:
        return "（数据库里没有表）"
    return "\n".join(f"{name}  ({kind})" for name, kind in rows)


@tool
def describe_table(table: str) -> str:
    """查看一张表的建表 SQL 与列定义。table 为表名。"""
    name = str(table or "").strip()
    if not name:
        return "table 不能为空"

    with _connect() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return f"表不存在：{name}"
        columns = conn.execute(f"PRAGMA table_info({_quote_ident(name)})").fetchall()

    lines = [f"表：{name}", "", "建表 SQL：", row[0] or "（无）", "", "列："]
    for cid, col_name, col_type, notnull, default, pk in columns:
        flags = []
        if notnull:
            flags.append("NOT NULL")
        if pk:
            flags.append("PK")
        if default is not None:
            flags.append(f"default={default}")
        suffix = ("  " + " ".join(flags)) if flags else ""
        lines.append(f"  {cid}. {col_name} {col_type or ''}{suffix}".rstrip())
    return "\n".join(lines)


@tool
def query(sql: str, limit: int = 20) -> str:
    """执行只读查询（SELECT / WITH），返回列名与前 N 行。limit 默认 20，最多 200。"""
    statement = str(sql or "").strip()
    if not statement:
        return "sql 不能为空"

    limit = max(1, min(int(limit), MAX_ROWS))

    with _connect() as conn:
        cursor = conn.execute(statement)
        if cursor.description is None:
            return "这条语句没有返回结果（本服务只支持 SELECT / WITH）"
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(limit + 1)

    truncated = len(rows) > limit
    rows = rows[:limit]

    def cell(value) -> str:
        if value is None:
            return "NULL"
        text = str(value).replace("\n", " ⏎ ")
        return text[:MAX_CELL_CHARS] + "…" if len(text) > MAX_CELL_CHARS else text

    lines = [" | ".join(columns), "-" * 40]
    for row in rows:
        lines.append(" | ".join(cell(v) for v in row))
    if truncated:
        lines.append(f"…（仅显示前 {limit} 行）")
    if not rows:
        lines.append("（没有数据行）")
    return "\n".join(lines)


def main() -> None:
    run_cli(
        SERVER_NAME,
        SERVER_VERSION,
        SERVER_INSTRUCTIONS,
        REGISTRY,
        description="SQLite 只读查询 MCP 服务",
    )


if __name__ == "__main__":
    main()
