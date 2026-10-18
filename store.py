"""身份库：openid ↔ 人 的持久映射。

设计要点
--------
- **账号 ≠ 人**：同一个人在不同 scope（私聊 / 各群）可能是不同 openid，
  所以 ``accounts`` 一行只代表「某个 scope 里的一个 openid」，多行可指向同一个
  ``identity``。
- **主名 + 历史**：QQ 每次只给当前昵称，改名后旧名就消失。这里把见过的每个名字
  都写进 ``name_history``，主名按「来源等级」升级，不因改名而抖动。
- **可撤销**：合并是破坏性操作，写入 ``op_log`` 快照，支持 ``/撤销`` 回滚。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_openid TEXT PRIMARY KEY,
    name         TEXT,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_name TEXT NOT NULL,
    source       INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    created_at   REAL NOT NULL,
    last_seen    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    scope       TEXT NOT NULL,
    openid      TEXT NOT NULL,
    identity_id INTEGER NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    UNIQUE(platform, scope, openid)
);
CREATE INDEX IF NOT EXISTS idx_accounts_openid ON accounts(openid);
CREATE INDEX IF NOT EXISTS idx_accounts_identity ON accounts(identity_id);
CREATE TABLE IF NOT EXISTS name_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id INTEGER NOT NULL,
    name        TEXT NOT NULL,
    source      INTEGER NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    UNIQUE(identity_id, name)
);
CREATE INDEX IF NOT EXISTS idx_names_identity ON name_history(identity_id);
CREATE TABLE IF NOT EXISTS op_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    operator TEXT NOT NULL,
    action   TEXT NOT NULL,
    payload  TEXT NOT NULL,
    undone   INTEGER NOT NULL DEFAULT 0
);
"""

# 名字来源等级：数字越大越可信，只有更高的等级才能替换主名。
SOURCE_PLACEHOLDER = 0  # 占位名，如「群友-6844E3」
SOURCE_NICKNAME = 1  # 平台昵称（群名片）
SOURCE_LLM = 2  # 从对话里抽取
SOURCE_SELF = 3  # 用户自报「我是某某」
SOURCE_ADMIN = 4  # 管理员指定

SOURCE_LABEL = {
    SOURCE_PLACEHOLDER: "占位",
    SOURCE_NICKNAME: "昵称",
    SOURCE_LLM: "抽取",
    SOURCE_SELF: "自报",
    SOURCE_ADMIN: "指定",
}


def valid_name(raw: str | None, openid: str) -> str | None:
    """过滤掉不能当人名的脏值。

    QQ 官方号在昵称为空时会把 openid 本身填进来，历史上实际出现过把 openid
    （形如 ``0123456789ABCDEF...`` 的长 hex 串）当成名字的情况，所以必须过滤。
    """
    if not raw:
        return None
    name = str(raw).strip()
    if not name or len(name) > 24:
        return None
    # 等于 openid、或等于/以 openid 前缀开头 → 是 ID 冒充名字
    head = openid[:8]
    if name == openid or (head and (name == head or name.startswith(head))):
        return None
    # 消息组件残留
    if "[At:" in name or "<" in name or "\n" in name:
        return None
    # 长 hex 串：多半是 ID 变体
    compact = name.replace(" ", "")
    if len(compact) >= 16 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return None
    return name


def is_openid_like(token: str) -> bool:
    """判断一个 token 是不是 openid 前缀（群/成员 openid 都是大写 hex）。"""
    t = (token or "").strip()
    return len(t) >= 8 and all(c in "0123456789abcdefABCDEF" for c in t)


class IdentityStore:
    """身份库的读写入口。一个进程一个实例即可（SQLite 同线程使用）。"""

    def __init__(self, path: str | Path, placeholder_prefix: str = "群友-") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.placeholder_prefix = placeholder_prefix
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """向旧库补齐新增字段，保证升级不丢数据。"""
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(identities)")
        }
        if "note" not in columns:
            self.conn.execute("ALTER TABLE identities ADD COLUMN note TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def set_note(self, identity_id: int, note: str | None) -> bool:
        """设置/清空管理员手写备注。返回是否影响到了行。"""
        cur = self.conn.execute(
            "UPDATE identities SET note=? WHERE id=?", (note or None, identity_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ 群
    def touch_group(self, group_openid: str, name: str | None = None) -> None:
        """登记一次群出现；仅当已有记录没有名字时才用传入名字补齐。"""
        now = time.time()
        row = self.conn.execute(
            "SELECT name FROM groups WHERE group_openid=?", (group_openid,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO groups(group_openid,name,first_seen,last_seen)"
                " VALUES(?,?,?,?)",
                (group_openid, name, now, now),
            )
        elif name and not row["name"]:
            self.conn.execute(
                "UPDATE groups SET name=?, last_seen=? WHERE group_openid=?",
                (name, now, group_openid),
            )
        else:
            self.conn.execute(
                "UPDATE groups SET last_seen=? WHERE group_openid=?", (now, group_openid)
            )
        self.conn.commit()

    def set_group_name(self, group_openid: str, name: str) -> None:
        """管理员给群起中文名，供私聊里寻址。"""
        self.touch_group(group_openid)
        self.conn.execute(
            "UPDATE groups SET name=? WHERE group_openid=?", (name, group_openid)
        )
        self.conn.commit()

    def list_groups(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM groups ORDER BY last_seen DESC").fetchall()
        )

    def find_group(self, token: str) -> sqlite3.Row | None:
        """按中文名精确匹配，或按群 openid 前缀匹配。"""
        token = (token or "").strip()
        if not token:
            return None
        rows = self.list_groups()
        for row in rows:
            if row["name"] and row["name"] == token:
                return row
        low = token.lower()
        for row in rows:
            if row["group_openid"].lower().startswith(low):
                return row
        return None

    # -------------------------------------------------------------- 身份
    def _create_identity(self, name: str, source: int, now: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO identities(primary_name,source,created_at,last_seen)"
            " VALUES(?,?,?,?)",
            (name, source, now, now),
        )
        identity_id = int(cur.lastrowid)
        # 占位名（群友-xxxxxx）只作为显示兜底，不写进名字历史，
        # 否则它会被当成一个「别名」污染后续展示。
        if source != SOURCE_PLACEHOLDER:
            self.conn.execute(
                "INSERT OR IGNORE INTO name_history"
                "(identity_id,name,source,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (identity_id, name, source, now, now),
            )
        return identity_id

    def record_name(self, identity_id: int, name: str, source: int) -> bool:
        """记录一个名字；返回主名是否被升级。"""
        now = time.time()
        row = self.conn.execute(
            "SELECT id FROM name_history WHERE identity_id=? AND name=?",
            (identity_id, name),
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE name_history SET last_seen=?, source=MAX(source,?) WHERE id=?",
                (now, source, row["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO name_history(identity_id,name,source,first_seen,last_seen)"
                " VALUES(?,?,?,?,?)",
                (identity_id, name, source, now, now),
            )
        ident = self.conn.execute(
            "SELECT primary_name,source FROM identities WHERE id=?", (identity_id,)
        ).fetchone()
        upgraded = False
        if ident is not None and source > ident["source"]:
            self.conn.execute(
                "UPDATE identities SET primary_name=?, source=?, last_seen=? WHERE id=?",
                (name, source, now, identity_id),
            )
            upgraded = True
        else:
            self.conn.execute(
                "UPDATE identities SET last_seen=? WHERE id=?", (now, identity_id)
            )
        self.conn.commit()
        return upgraded

    def touch_account(
        self,
        platform: str,
        scope: str,
        openid: str,
        nickname: str | None = None,
    ) -> tuple[int, bool]:
        """登记一次出现，返回 (identity_id, 是否新建账号)。

        新账号先尝试按 openid 跨 scope 自动归并：同一个 openid 已经在别的 scope
        出现过（实测私聊 user_openid 与群 member_openid 可能同值），就挂到同一个
        人身上；否则新建身份。
        """
        now = time.time()
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE platform=? AND scope=? AND openid=?",
            (platform, scope, openid),
        ).fetchone()
        created = False
        if row is not None:
            self.conn.execute(
                "UPDATE accounts SET last_seen=? WHERE id=?", (now, row["id"])
            )
            identity_id = int(row["identity_id"])
        else:
            other = self.conn.execute(
                "SELECT identity_id FROM accounts WHERE platform=? AND openid=?"
                " ORDER BY id LIMIT 1",
                (platform, openid),
            ).fetchone()
            if other is not None:
                identity_id = int(other["identity_id"])
            else:
                identity_id = self._create_identity(
                    f"{self.placeholder_prefix}{openid[:6]}",
                    SOURCE_PLACEHOLDER,
                    now,
                )
            self.conn.execute(
                "INSERT INTO accounts"
                "(platform,scope,openid,identity_id,first_seen,last_seen)"
                " VALUES(?,?,?,?,?,?)",
                (platform, scope, openid, identity_id, now, now),
            )
            created = True
        self.conn.commit()

        name = valid_name(nickname, openid)
        if name:
            self.record_name(identity_id, name, SOURCE_NICKNAME)
        return identity_id, created

    def get_identity(self, identity_id: int) -> dict[str, Any] | None:
        ident = self.conn.execute(
            "SELECT * FROM identities WHERE id=?", (identity_id,)
        ).fetchone()
        if ident is None:
            return None
        names = self.conn.execute(
            "SELECT name,source FROM name_history WHERE identity_id=?"
            " ORDER BY source DESC, last_seen DESC",
            (identity_id,),
        ).fetchall()
        accounts = self.conn.execute(
            "SELECT scope,openid,last_seen FROM accounts WHERE identity_id=?"
            " ORDER BY last_seen DESC",
            (identity_id,),
        ).fetchall()
        return {
            "id": int(ident["id"]),
            "primary_name": ident["primary_name"],
            "source": int(ident["source"]),
            "note": ident["note"],
            "created_at": ident["created_at"],
            "last_seen": ident["last_seen"],
            "names": [(row["name"], int(row["source"])) for row in names],
            "aliases": [row["name"] for row in names if row["name"] != ident["primary_name"]],
            "accounts": [
                (row["scope"], row["openid"], row["last_seen"]) for row in accounts
            ],
        }

    def identity_of(self, platform: str, scope: str, openid: str) -> int | None:
        row = self.conn.execute(
            "SELECT identity_id FROM accounts WHERE platform=? AND scope=? AND openid=?",
            (platform, scope, openid),
        ).fetchone()
        return int(row["identity_id"]) if row else None

    def forget(self, identity_id: int) -> dict[str, Any]:
        """彻底删除一个身份及其全部账号与名字历史（数据删除权）。"""
        ident = self.get_identity(identity_id)
        if ident is None:
            raise ValueError("身份不存在")
        accounts = self.conn.execute(
            "DELETE FROM accounts WHERE identity_id=?", (identity_id,)
        ).rowcount
        names = self.conn.execute(
            "DELETE FROM name_history WHERE identity_id=?", (identity_id,)
        ).rowcount
        self.conn.execute("DELETE FROM identities WHERE id=?", (identity_id,))
        self.conn.commit()
        return {
            "id": identity_id,
            "primary_name": ident["primary_name"],
            "accounts": accounts,
            "names": names,
        }

    def resolve(self, token: str, scope: str | None = None) -> list[int]:
        """把「名字 / openid 前缀 / #编号」解析成候选身份 id 列表。"""
        token = (token or "").strip()
        if not token:
            return []
        if token.startswith("#") and token[1:].isdigit():
            identity_id = int(token[1:])
            exists = self.conn.execute(
                "SELECT 1 FROM identities WHERE id=?", (identity_id,)
            ).fetchone()
            return [identity_id] if exists else []
        if is_openid_like(token):
            sql = "SELECT DISTINCT identity_id FROM accounts WHERE LOWER(openid) LIKE ?"
            args: list[Any] = [token.lower() + "%"]
            if scope:
                sql += " AND scope=?"
                args.append(scope)
            sql += " ORDER BY identity_id"
            return [int(row["identity_id"]) for row in self.conn.execute(sql, args)]
        sql = (
            "SELECT DISTINCT i.id FROM identities i"
            " JOIN name_history n ON n.identity_id=i.id WHERE n.name=?"
        )
        args = [token]
        if scope:
            sql += (
                " AND EXISTS (SELECT 1 FROM accounts a"
                " WHERE a.identity_id=i.id AND a.scope=?)"
            )
            args.append(scope)
        sql += " ORDER BY i.id"
        return [int(row["id"]) for row in self.conn.execute(sql, args)]

    # -------------------------------------------------------------- 合并
    def merge(self, keep_id: int, drop_id: int, operator: str) -> dict[str, Any]:
        """把 drop 并进 keep：迁移账号与名字历史，写撤销快照。"""
        if keep_id == drop_id:
            raise ValueError("不能合并到自己")
        keep = self.get_identity(keep_id)
        drop = self.get_identity(drop_id)
        if keep is None or drop is None:
            raise ValueError("身份不存在")
        now = time.time()

        account_ids = [
            int(row["id"])
            for row in self.conn.execute(
                "SELECT id FROM accounts WHERE identity_id=?", (drop_id,)
            )
        ]
        drop_names = [
            {
                "name": row["name"],
                "source": int(row["source"]),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            }
            for row in self.conn.execute(
                "SELECT name,source,first_seen,last_seen FROM name_history"
                " WHERE identity_id=?",
                (drop_id,),
            )
        ]

        added_names: list[str] = []
        for item in drop_names:
            exists = self.conn.execute(
                "SELECT id FROM name_history WHERE identity_id=? AND name=?",
                (keep_id, item["name"]),
            ).fetchone()
            if exists is not None:
                self.conn.execute(
                    "UPDATE name_history SET last_seen=MAX(last_seen,?),"
                    " source=MAX(source,?) WHERE id=?",
                    (item["last_seen"], item["source"], exists["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO name_history"
                    "(identity_id,name,source,first_seen,last_seen)"
                    " VALUES(?,?,?,?,?)",
                    (
                        keep_id,
                        item["name"],
                        item["source"],
                        item["first_seen"],
                        item["last_seen"],
                    ),
                )
                added_names.append(item["name"])

        if drop["source"] > keep["source"]:
            self.conn.execute(
                "UPDATE identities SET primary_name=?, source=?, last_seen=?"
                " WHERE id=?",
                (drop["primary_name"], drop["source"], now, keep_id),
            )
        else:
            self.conn.execute(
                "UPDATE identities SET last_seen=? WHERE id=?", (now, keep_id)
            )

        self.conn.execute(
            "UPDATE accounts SET identity_id=? WHERE identity_id=?", (keep_id, drop_id)
        )
        self.conn.execute("DELETE FROM name_history WHERE identity_id=?", (drop_id,))
        self.conn.execute("DELETE FROM identities WHERE id=?", (drop_id,))

        payload = {
            "keep": keep_id,
            "keep_before": {
                "primary_name": keep["primary_name"],
                "source": keep["source"],
            },
            "drop": {
                "id": drop_id,
                "primary_name": drop["primary_name"],
                "source": drop["source"],
                "created_at": drop["created_at"],
                "last_seen": drop["last_seen"],
                "names": drop_names,
            },
            "accounts": account_ids,
            "added_names": added_names,
        }
        cur = self.conn.execute(
            "INSERT INTO op_log(ts,operator,action,payload,undone) VALUES(?,?,?,?,0)",
            (now, operator, "merge", json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()
        return {"op_id": int(cur.lastrowid), **payload}

    def undo_last(self, operator: str) -> dict[str, Any] | None:
        """回滚最近一次未撤销的合并。"""
        row = self.conn.execute(
            "SELECT * FROM op_log WHERE action='merge' AND undone=0"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        drop = payload["drop"]

        self.conn.execute(
            "INSERT INTO identities(id,primary_name,source,created_at,last_seen)"
            " VALUES(?,?,?,?,?)",
            (
                drop["id"],
                drop["primary_name"],
                drop["source"],
                drop["created_at"],
                drop["last_seen"],
            ),
        )
        for item in drop["names"]:
            self.conn.execute(
                "INSERT OR REPLACE INTO name_history"
                "(identity_id,name,source,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (
                    drop["id"],
                    item["name"],
                    item["source"],
                    item["first_seen"],
                    item["last_seen"],
                ),
            )
        for account_id in payload["accounts"]:
            self.conn.execute(
                "UPDATE accounts SET identity_id=? WHERE id=?",
                (drop["id"], account_id),
            )
        for name in payload.get("added_names", []):
            self.conn.execute(
                "DELETE FROM name_history WHERE identity_id=? AND name=?",
                (payload["keep"], name),
            )
        before = payload.get("keep_before") or {}
        if before:
            self.conn.execute(
                "UPDATE identities SET primary_name=?, source=? WHERE id=?",
                (before["primary_name"], before["source"], payload["keep"]),
            )

        self.conn.execute("UPDATE op_log SET undone=1 WHERE id=?", (row["id"],))
        self.conn.commit()
        return payload

    # -------------------------------------------------------------- 名册
    def roster(self, scope: str, since: float, limit: int) -> list[dict[str, Any]]:
        """取某会话最近活跃的成员，按 identity id 排序以保证前缀稳定。"""
        rows = self.conn.execute(
            "SELECT a.openid, i.id, i.primary_name, i.note FROM accounts a"
            " JOIN identities i ON i.id=a.identity_id"
            " WHERE a.scope=? AND a.last_seen>=?"
            " ORDER BY i.id LIMIT ?",
            (scope, since, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            aliases = [
                item["name"]
                for item in self.conn.execute(
                    "SELECT name FROM name_history WHERE identity_id=? AND name<>?"
                    " ORDER BY source DESC, last_seen DESC LIMIT 2",
                    (row["id"], row["primary_name"]),
                )
            ]
            result.append(
                {
                    "identity_id": int(row["id"]),
                    "openid": row["openid"],
                    "name": row["primary_name"],
                    "note": row["note"],
                    "aliases": aliases,
                }
            )
        return result

    def stats(self) -> dict[str, int]:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "identities": one("SELECT COUNT(*) FROM identities"),
            "accounts": one("SELECT COUNT(*) FROM accounts"),
            "names": one("SELECT COUNT(*) FROM name_history"),
            "groups": one("SELECT COUNT(*) FROM groups"),
        }
