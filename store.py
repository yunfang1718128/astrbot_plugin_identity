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
    alias        TEXT,
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

# 各平台适配器在拿不到昵称时的兜底值，均不能当人名。
# aiocqhttp 在 sender 既无 card 也无 nickname 时会填 "N/A"，
# 其请求/通知事件则直接把昵称填成 QQ 号（已由 openid 比对拦住）。
PLACEHOLDER_NAMES = {"n/a", "na", "null", "none", "unknown", "未知", "-", "无"}

# 别名（管理员设的短名）长度上限：短名就是为了省 token 和好打字。
ALIAS_MAX_CHARS = 12


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
    # 平台兜底占位值
    if name.strip().lower() in PLACEHOLDER_NAMES:
        return None
    # 长 hex 串：多半是 ID 变体
    compact = name.replace(" ", "")
    if len(compact) >= 16 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return None
    return name


def valid_alias(raw: str | None, max_chars: int = ALIAS_MAX_CHARS) -> str | None:
    """校验管理员别名：短、单行、不像 ID、不是平台兜底值。

    别名是显示名，不写进 ``name_history``，所以这里不做 openid 比对
    （别名本来就不该长得像 openid，见下方 ``is_openid_like``）。
    """
    if not raw:
        return None
    alias = str(raw).strip()
    if not alias or len(alias) > max_chars:
        return None
    if "\n" in alias or "\r" in alias or "[At:" in alias or "<" in alias:
        return None
    if alias.lower() in PLACEHOLDER_NAMES:
        return None
    if is_openid_like(alias):
        return None
    return alias


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
        if "alias" not in columns:
            self.conn.execute("ALTER TABLE identities ADD COLUMN alias TEXT")
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

    def set_alias(self, identity_id: int, alias: str | None) -> bool:
        """设置/清空管理员别名。别名只改显示名，不写进名字历史。"""
        cur = self.conn.execute(
            "UPDATE identities SET alias=? WHERE id=?", (alias or None, identity_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def alias_owner(self, alias: str, exclude_id: int | None = None) -> int | None:
        """别名是否已被别人占用；占用则返回那个身份的 id。"""
        token = (alias or "").strip()
        if not token:
            return None
        row = self.conn.execute(
            "SELECT id FROM identities WHERE alias=?", (token,)
        ).fetchone()
        if row is None:
            return None
        owner = int(row["id"])
        if exclude_id is not None and owner == exclude_id:
            return None
        return owner

    def list_aliases(self, limit: int = 30) -> list[dict[str, Any]]:
        """已设别名的身份，按 id 排序（查看用）。"""
        rows = self.conn.execute(
            "SELECT id,alias,primary_name FROM identities"
            " WHERE alias IS NOT NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "alias": row["alias"],
                "primary_name": row["primary_name"],
            }
            for row in rows
        ]

    def count_identities(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
        )

    def page_identities(self, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        """分页列出身份（/名单 用），按最近活跃排序。"""
        rows = self.conn.execute(
            "SELECT i.id, i.primary_name, i.alias, i.note, i.last_seen,"
            " (SELECT COUNT(*) FROM accounts a WHERE a.identity_id=i.id) AS n_accounts"
            " FROM identities i ORDER BY i.last_seen DESC, i.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "primary_name": row["primary_name"],
                "alias": row["alias"],
                "display_name": row["alias"] or row["primary_name"],
                "note": row["note"],
                "last_seen": row["last_seen"],
                "accounts": int(row["n_accounts"]),
            }
            for row in rows
        ]

    def name_history(self, identity_id: int) -> list[dict[str, Any]]:
        """某人的全部名字及来源/时间（/曾用名 用）。"""
        rows = self.conn.execute(
            "SELECT name,source,first_seen,last_seen FROM name_history"
            " WHERE identity_id=? ORDER BY first_seen, id",
            (identity_id,),
        ).fetchall()
        return [
            {
                "name": row["name"],
                "source": int(row["source"]),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            }
            for row in rows
        ]

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
            "alias": ident["alias"],
            "display_name": ident["alias"] or ident["primary_name"],
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
            "alias": ident["alias"],
            "display_name": ident["display_name"],
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
        # 名字与别名等价解析：/谁、/备注、/合并、/忘记 都能直接打短别名。
        sql = (
            "SELECT DISTINCT i.id FROM identities i"
            " WHERE (i.alias=? OR EXISTS (SELECT 1 FROM name_history n"
            "   WHERE n.identity_id=i.id AND n.name=?))"
        )
        args: list[Any] = [token, token]
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
        # 别名跟着并过去：自己没设过才被合并方覆盖，避免管理员设的短名丢失。
        if not keep["alias"] and drop["alias"]:
            self.conn.execute(
                "UPDATE identities SET alias=? WHERE id=?", (drop["alias"], keep_id)
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
                "alias": keep["alias"],
            },
            "drop": {
                "id": drop_id,
                "primary_name": drop["primary_name"],
                "source": drop["source"],
                "alias": drop["alias"],
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
            "INSERT INTO identities(id,primary_name,alias,source,created_at,last_seen)"
            " VALUES(?,?,?,?,?,?)",
            (
                drop["id"],
                drop["primary_name"],
                drop.get("alias"),
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
                "UPDATE identities SET primary_name=?, source=?, alias=? WHERE id=?",
                (
                    before["primary_name"],
                    before["source"],
                    before.get("alias"),
                    payload["keep"],
                ),
            )

        self.conn.execute("UPDATE op_log SET undone=1 WHERE id=?", (row["id"],))
        self.conn.commit()
        return payload

    # -------------------------------------------------------------- 名册
    def roster(self, scope: str, since: float, limit: int) -> list[dict[str, Any]]:
        """取某会话最近活跃的成员，按 identity id 排序以保证前缀稳定。

        ``name`` 是注入/展示用的名字：管理员设了别名就用别名（省 token、好认），
        没设就用主名；``real_name`` 保留主名供卡片展示。
        """
        rows = self.conn.execute(
            "SELECT a.openid, i.id, i.primary_name, i.alias, i.note FROM accounts a"
            " JOIN identities i ON i.id=a.identity_id"
            " WHERE a.scope=? AND a.last_seen>=?"
            " ORDER BY i.id LIMIT ?",
            (scope, since, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            display = row["alias"] or row["primary_name"]
            # 有别名时不再罗列历史昵称：那正是管理员想甩掉的长文本。
            aliases = (
                []
                if row["alias"]
                else [
                    item["name"]
                    for item in self.conn.execute(
                        "SELECT name FROM name_history WHERE identity_id=? AND name<>?"
                        " ORDER BY source DESC, last_seen DESC LIMIT 2",
                        (row["id"], row["primary_name"]),
                    )
                ]
            )
            result.append(
                {
                    "identity_id": int(row["id"]),
                    "openid": row["openid"],
                    "name": display,
                    "real_name": row["primary_name"],
                    "alias": row["alias"],
                    "note": row["note"],
                    "aliases": aliases,
                }
            )
        return result

    # ----------------------------------------------------------- 导入导出
    def export_data(self) -> dict[str, Any]:
        """把整个身份库导出成可序列化的 dict（/导出 用）。

        不导出内部自增 id 与 op_log：导入端重建 id，合并历史在那里无意义。
        """
        identities: list[dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT id,primary_name,alias,source,note,created_at,last_seen"
            " FROM identities ORDER BY id"
        ):
            identities.append(
                {
                    "primary_name": row["primary_name"],
                    "alias": row["alias"],
                    "source": int(row["source"]),
                    "note": row["note"],
                    "created_at": row["created_at"],
                    "last_seen": row["last_seen"],
                    "accounts": [
                        {
                            "platform": a["platform"],
                            "scope": a["scope"],
                            "openid": a["openid"],
                        }
                        for a in self.conn.execute(
                            "SELECT platform,scope,openid FROM accounts"
                            " WHERE identity_id=? ORDER BY id",
                            (row["id"],),
                        )
                    ],
                    "names": [
                        {"name": n["name"], "source": int(n["source"])}
                        for n in self.conn.execute(
                            "SELECT name,source FROM name_history"
                            " WHERE identity_id=? ORDER BY id",
                            (row["id"],),
                        )
                    ],
                }
            )
        return {
            "schema": 1,
            "plugin": "astrbot_plugin_identity",
            "exported_at": time.time(),
            "stats": self.stats(),
            "identities": identities,
        }

    def import_data(self, payload: dict[str, Any]) -> dict[str, int]:
        """合并导入：已有账号不重建，缺的补齐；不覆盖本机已设的别名/备注。"""
        result = {"identities": 0, "accounts": 0, "names": 0, "skipped": 0}
        items = payload.get("identities")
        if not isinstance(items, list):
            raise ValueError("文件格式不对：缺少 identities 数组")
        now = time.time()
        for item in items:
            if not isinstance(item, dict):
                result["skipped"] += 1
                continue
            name = str(item.get("primary_name") or "").strip()
            accounts = [
                acc
                for acc in (item.get("accounts") or [])
                if isinstance(acc, dict)
                and acc.get("platform")
                and acc.get("scope")
                and acc.get("openid")
            ]
            if not name or not accounts:
                result["skipped"] += 1
                continue

            # 先看这个人在本机是否已存在（任一账号命中即认为是同一个人）
            identity_id: int | None = None
            for acc in accounts:
                existing = self.conn.execute(
                    "SELECT identity_id FROM accounts"
                    " WHERE platform=? AND scope=? AND openid=?",
                    (acc["platform"], acc["scope"], acc["openid"]),
                ).fetchone()
                if existing is not None:
                    identity_id = int(existing["identity_id"])
                    break
            if identity_id is None:
                identity_id = self._create_identity(
                    name, int(item.get("source") or SOURCE_NICKNAME), now
                )
                result["identities"] += 1

            current = self.conn.execute(
                "SELECT alias,note FROM identities WHERE id=?", (identity_id,)
            ).fetchone()
            alias = item.get("alias")
            if alias and current is not None and not current["alias"]:
                if self.alias_owner(str(alias), exclude_id=identity_id) is None:
                    self.set_alias(identity_id, str(alias))
            note = item.get("note")
            if note and current is not None and not current["note"]:
                self.set_note(identity_id, str(note)[:60])

            for acc in accounts:
                exists = self.conn.execute(
                    "SELECT 1 FROM accounts"
                    " WHERE platform=? AND scope=? AND openid=?",
                    (acc["platform"], acc["scope"], acc["openid"]),
                ).fetchone()
                if exists is not None:
                    continue
                self.conn.execute(
                    "INSERT INTO accounts"
                    "(platform,scope,openid,identity_id,first_seen,last_seen)"
                    " VALUES(?,?,?,?,?,?)",
                    (acc["platform"], acc["scope"], acc["openid"], identity_id, now, now),
                )
                result["accounts"] += 1
            self.conn.commit()

            for name_item in item.get("names") or []:
                if not isinstance(name_item, dict):
                    continue
                raw = str(name_item.get("name") or "").strip()
                if not raw:
                    continue
                before = self.conn.execute(
                    "SELECT 1 FROM name_history WHERE identity_id=? AND name=?",
                    (identity_id, raw),
                ).fetchone()
                if before is None:
                    result["names"] += 1
                self.record_name(identity_id, raw, int(name_item.get("source") or 0))
        return result

    def stats(self) -> dict[str, int]:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "identities": one("SELECT COUNT(*) FROM identities"),
            "accounts": one("SELECT COUNT(*) FROM accounts"),
            "names": one("SELECT COUNT(*) FROM name_history"),
            "groups": one("SELECT COUNT(*) FROM groups"),
            "aliases": one("SELECT COUNT(*) FROM identities WHERE alias IS NOT NULL"),
        }
