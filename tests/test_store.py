"""认人本自测：零第三方依赖，直接 ``python tests/test_store.py`` 运行。

覆盖：脏名过滤、跨 scope 自动归并、改名不覆盖主名、来源等级升级、三种解析方式、
合并与撤销、删除、名册生成、管理员别名、名单分页、导出导入。
任何一项失败都会抛 AssertionError 并以非零码退出。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import (  # noqa: E402
    SOURCE_ADMIN,
    SOURCE_SELF,
    IdentityStore,
    is_openid_like,
    valid_alias,
    valid_name,
)

PLATFORM = "qq_official"
GROUP_A = "AAAA0000BBBB1111CCCC2222DDDD3333"
GROUP_B = "EEEE4444FFFF5555AAAA6666BBBB7777"
GROUP_C = "CCCC8888DDDD9999EEEE0000FFFF1111"
USER_A = "0123456789ABCDEF0123456789ABCDEF"
USER_B = "FEDCBA9876543210FEDCBA9876543210"
USER_C1 = "11111111222222223333333344444444"
USER_C2 = "55555555666666667777777788888888"
USER_C3 = "99999999AAAABBBBCCCCDDDDEEEEFFFF"


def check(label: str, got, expect) -> None:
    ok = got == expect
    print(f"  {'OK  ' if ok else 'FAIL'} {label}: {got!r}")
    assert ok, f"{label} 期望 {expect!r}，实得 {got!r}"


def section(title: str) -> None:
    print(f"\n== {title} ==")


def test_dirty_names() -> None:
    section("脏名过滤")
    cases = [
        ("云帆少年", True),
        ("", False),
        (None, False),
        (USER_A, False),  # 昵称 == openid
        (USER_A[:8], False),  # 昵称 == openid 前缀
        (USER_A[:8] + "尾巴", False),
        ("[At:123]", False),
        ("abcdef0123456789ab", False),  # 长 hex 串
        ("N/A", False),  # aiocqhttp 兜底值
        ("null", False),
        ("未知", False),
        ("路人", True),
        ("x" * 40, False),
    ]
    for raw, expect in cases:
        check(f"valid_name({str(raw)[:20]!r})", bool(valid_name(raw, USER_A)), expect)
    check("is_openid_like('0123ABCD')", is_openid_like("0123ABCD"), True)
    check("is_openid_like('云帆少年')", is_openid_like("云帆少年"), False)


def test_learn_and_link(store: IdentityStore) -> int:
    section("群聊学习 + 私聊自动归并")
    store.touch_group(GROUP_A)
    store.set_group_name(GROUP_A, "摸鱼群")
    check("按中文名找群", store.find_group("摸鱼群")["group_openid"], GROUP_A)
    check("按短码找群", store.find_group(GROUP_A[:6])["group_openid"], GROUP_A)
    check("找不存在的群", store.find_group("不存在的群"), None)

    identity_id, created = store.touch_account(PLATFORM, GROUP_A, USER_A, "云帆少年")
    check("首次学习即建新身份", created, True)
    card = store.get_identity(identity_id)
    check("主名取自群昵称", card["primary_name"], "云帆少年")

    same_id, created_again = store.touch_account(PLATFORM, "c2c", USER_A, None)
    check("私聊同 openid 自动归并", same_id, identity_id)
    check("归并后不新建身份", created_again, True)
    check("一个人挂两个账号", len(store.get_identity(identity_id)["accounts"]), 2)
    return identity_id


def test_name_rules(store: IdentityStore, identity_id: int) -> None:
    section("改名不覆盖主名 / 来源等级升级")
    store.record_name(identity_id, "云帆", 1)
    card = store.get_identity(identity_id)
    check("昵称级改名不动主名", card["primary_name"], "云帆少年")
    check("新名字进别名", "云帆" in card["aliases"], True)

    store.record_name(identity_id, "云帆本人", SOURCE_SELF)
    check("自报可升级主名", store.get_identity(identity_id)["primary_name"], "云帆本人")

    store.record_name(identity_id, "云帆同学", 1)
    check("低等级不能回退主名", store.get_identity(identity_id)["primary_name"], "云帆本人")

    store.record_name(identity_id, "云帆大人", SOURCE_ADMIN)
    check("管理员指定最高优先", store.get_identity(identity_id)["primary_name"], "云帆大人")


def test_resolve(store: IdentityStore, identity_id: int) -> None:
    section("三种解析方式")
    check("按名字", store.resolve("云帆大人"), [identity_id])
    check("按 openid 前缀", store.resolve(USER_A[:8]), [identity_id])
    check("按 #编号", store.resolve(f"#{identity_id}"), [identity_id])
    check("按群作用域过滤", store.resolve("云帆大人", scope=GROUP_A), [identity_id])
    check("不存在的名字", store.resolve("查无此人"), [])


def test_merge_undo(store: IdentityStore, identity_id: int) -> None:
    section("跨群合并与撤销")
    other_id, _ = store.touch_account(PLATFORM, GROUP_B, USER_B, "云帆（另一个群）")
    check("另一个群里是独立身份", other_id != identity_id, True)

    keep, drop = min(identity_id, other_id), max(identity_id, other_id)
    result = store.merge(keep, drop, "test:admin")
    merged = store.get_identity(keep)
    check("合并后账号数", len(merged["accounts"]), 3)
    check("旧群名字变成别名", "云帆（另一个群）" in merged["aliases"], True)
    check("被合并的身份已消失", store.get_identity(drop), None)

    payload = store.undo_last("test:admin")
    check("撤销返回快照", payload["keep"], keep)
    restored = store.get_identity(drop)
    check("撤销后被合并身份恢复", restored is not None, True)
    check("撤销后账号数还原", len(store.get_identity(keep)["accounts"]), 2)
    check("没有更多可撤销", store.undo_last("test:admin"), None)


def test_forget_and_roster(store: IdentityStore, identity_id: int) -> None:
    section("删除与名册")
    roster = store.roster(GROUP_A, 0, 20)
    check("名册包含本人", any(r["identity_id"] == identity_id for r in roster), True)

    info = store.forget(identity_id)
    check("删除返回账号数", info["accounts"] >= 1, True)
    check("删除后查不到", store.get_identity(identity_id), None)
    check("删除后名册为空", store.roster(GROUP_A, 0, 20), [])


def test_note(store: IdentityStore, identity_id: int) -> None:
    section("管理员备注")
    check("默认无备注", store.get_identity(identity_id)["note"], None)

    store.set_note(identity_id, "服务器主人，爱考 MC 知识")
    check(
        "写入后读得到",
        store.get_identity(identity_id)["note"],
        "服务器主人，爱考 MC 知识",
    )

    roster = store.roster(GROUP_A, 0, 20)
    check("名册里带备注", roster[0]["note"], "服务器主人，爱考 MC 知识")

    store.set_note(identity_id, None)
    check("清空后为 None", store.get_identity(identity_id)["note"], None)


def test_alias(store: IdentityStore, identity_id: int) -> None:
    section("管理员别名")
    card = store.get_identity(identity_id)
    check("默认无别名", card["alias"], None)
    check("display_name 回落主名", card["display_name"], card["primary_name"])

    check("合法别名", valid_alias("云帆"), "云帆")
    check("超长别名被拒", valid_alias("一二三四五六七八九十一二三"), None)
    check("多行别名被拒", valid_alias("云\n帆"), None)
    check("纯 hex 别名被拒", valid_alias("0123ABCD"), None)
    check("平台兜底值不能当别名", valid_alias("N/A"), None)

    store.set_alias(identity_id, "云帆")
    card = store.get_identity(identity_id)
    check("别名写入", card["alias"], "云帆")
    check("display_name 用别名", card["display_name"], "云帆")
    check("主名不变", card["primary_name"], "云帆大人")

    check("按别名解析", store.resolve("云帆"), [identity_id])
    check("别名占用检测", store.alias_owner("云帆"), identity_id)
    check("排除自己不算占用", store.alias_owner("云帆", exclude_id=identity_id), None)

    roster = store.roster(GROUP_A, 0, 20)
    item = next(r for r in roster if r["identity_id"] == identity_id)
    check("名册用别名", item["name"], "云帆")
    check("名册保留真名", item["real_name"], "云帆大人")
    check("有别名不再附曾用名", item["aliases"], [])

    check("别名列表", [o["alias"] for o in store.list_aliases()], ["云帆"])

    # 昵称继续变不能把别名冲掉（别名是独立字段，不进名字历史）
    store.record_name(identity_id, "又换了个长名字", 1)
    check("改名后别名仍在", store.get_identity(identity_id)["alias"], "云帆")
    names_before = len(store.get_identity(identity_id)["names"])
    store.set_alias(identity_id, "豆豆")
    check("别名不写进名字历史", len(store.get_identity(identity_id)["names"]), names_before)
    store.set_alias(identity_id, "云帆")

    store.set_alias(identity_id, None)
    check("清空别名", store.get_identity(identity_id)["alias"], None)
    store.set_alias(identity_id, "云帆")


def test_alias_merge(store: IdentityStore, identity_id: int) -> None:
    section("合并时别名处理")
    other_id, _ = store.touch_account(PLATFORM, GROUP_B, USER_B, "另一个很长很长的名字")
    store.set_alias(other_id, "老王")
    keep, drop = min(identity_id, other_id), max(identity_id, other_id)
    store.merge(keep, drop, "test:admin")
    # 双方都有别名时，保留 keep 自己的，不被合并方覆盖
    expect_keep_alias = "云帆" if keep == identity_id else "老王"
    check("合并保留自己的别名", store.get_identity(keep)["alias"], expect_keep_alias)

    store.undo_last("test:admin")
    expect_drop_alias = "云帆" if drop == identity_id else "老王"
    check("撤销恢复被合并方别名", store.get_identity(drop)["alias"], expect_drop_alias)
    check("撤销后 keep 别名不变", store.get_identity(keep)["alias"], expect_keep_alias)
    store.forget(other_id)


def test_listing(store: IdentityStore, identity_id: int) -> None:
    section("名单与曾用名")
    total = store.count_identities()
    check("身份总数", total >= 1, True)
    first = store.page_identities(10, 0)
    check("首页不超 10 条", len(first) <= 10, True)
    check("首页条数等于 min(总数,10)", len(first), min(total, 10))
    check("偏移到头为空", store.page_identities(10, total), [])

    card = store.get_identity(identity_id)
    history = store.name_history(identity_id)
    check("曾用名含主名", any(h["name"] == card["primary_name"] for h in history), True)
    check("曾用名带时间", all(h["first_seen"] > 0 for h in history), True)
    check("曾用名拼起来不漏", len(history) >= 1, True)


def test_export_import(store: IdentityStore, identity_id: int) -> None:
    section("导出与导入")
    data = store.export_data()
    check("导出 schema", data["schema"], 1)
    check("导出人数与库一致", len(data["identities"]), store.stats()["identities"])
    check("导出含账号", sum(len(i["accounts"]) for i in data["identities"]), store.stats()["accounts"])
    check("导出不泄内部 id", "id" in data["identities"][0], False)

    with tempfile.TemporaryDirectory() as tmp:
        other = IdentityStore(os.path.join(tmp, "other.db"))
        try:
            result = other.import_data(data)
            check("导入人数", other.stats()["identities"], store.stats()["identities"])
            check("导入账号数", other.stats()["accounts"], store.stats()["accounts"])
            check("导入名字数", other.stats()["names"], store.stats()["names"])
            check("导入返回值一致", result["identities"], store.stats()["identities"])
            check(
                "别名跟着导入",
                other.get_identity(other.resolve(store.get_identity(identity_id)["alias"])[0])["alias"]
                if store.get_identity(identity_id)["alias"]
                else "无别名可导",
                store.get_identity(identity_id)["alias"] or "无别名可导",
            )

            again = other.import_data(data)
            check("重复导入不新增人", again["identities"], 0)
            check("重复导入不新增账号", again["accounts"], 0)
            check("重复导入不新增名字", again["names"], 0)

            try:
                other.import_data({"identities": "不是数组"})
                check("坏格式应报错", "没报错", "ValueError")
            except ValueError:
                check("坏格式报错", True, True)
        finally:
            other.close()


def test_relevant_members(store: IdentityStore, identity_id: int) -> None:
    section("名册瘦身：相关成员选择")
    store.touch_account(PLATFORM, GROUP_C, USER_C1, "甲同学")
    id2, _ = store.touch_account(PLATFORM, GROUP_C, USER_C2, "乙同学")
    id3, _ = store.touch_account(PLATFORM, GROUP_C, USER_C3, "丙同学")
    store.set_alias(id3, "阿丙")

    picked, total = store.relevant_members(GROUP_C, 0, USER_C1, [], "")
    check("只含发言人", [p["openid"] for p in picked], [USER_C1])
    check("总数统计", total, 3)

    picked, _ = store.relevant_members(GROUP_C, 0, None, [USER_C2], "")
    check("按 @ 选人", [p["openid"] for p in picked], [USER_C2])

    picked, _ = store.relevant_members(GROUP_C, 0, USER_C1, [USER_C1], "")
    check("发言人与 @ 重复不叠加", len(picked), 1)

    picked, _ = store.relevant_members(GROUP_C, 0, None, [], "乙同学在吗")
    check("正文点名主名", [p["openid"] for p in picked], [USER_C2])

    picked, _ = store.relevant_members(GROUP_C, 0, None, [], "阿丙呢")
    check("正文点名别名", [p["openid"] for p in picked], [USER_C3])

    picked, _ = store.relevant_members(GROUP_C, 0, USER_C1, [USER_C2], "乙同学和丙同学")
    check("发言人 + @ + 点名去重排序", len(picked), 3)
    check("发言人排第一", picked[0]["openid"], USER_C1)

    picked, _ = store.relevant_members(GROUP_C, 0, None, [], "今天天气不错")
    check("没点名就不注入", picked, [])

    check("单字别名不参与匹配", store.relevant_members(GROUP_C, 0, None, [], "阿")[0], [])

    # 清场，避免影响后面的删除/名册断言
    for cid in (id2, id3):
        store.forget(cid)
    store.forget(store.resolve("甲同学")[0])


def test_migration() -> None:
    """旧库（无 note 列）升级后不能丢数据。"""
    section("旧库迁移")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "old.db")
        old = sqlite3.connect(path)
        old.executescript(
            "CREATE TABLE identities (\n"
            "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "    primary_name TEXT NOT NULL,\n"
            "    source INTEGER NOT NULL DEFAULT 0,\n"
            "    created_at REAL NOT NULL,\n"
            "    last_seen REAL NOT NULL\n"
            ");"
        )
        old.execute(
            "INSERT INTO identities(primary_name,source,created_at,last_seen)"
            " VALUES('老用户',1,0,0)"
        )
        old.commit()
        old.close()

        migrated = IdentityStore(path)
        try:
            card = migrated.get_identity(1)
            check("旧数据保留", card["primary_name"], "老用户")
            check("note 列自动补上", card["note"], None)
            migrated.set_note(1, "迁移后可用")
            check("迁移后可写备注", migrated.get_identity(1)["note"], "迁移后可用")
            check("alias 列自动补上", migrated.get_identity(1)["alias"], None)
            migrated.set_alias(1, "老用户")
            check("迁移后可写别名", migrated.get_identity(1)["display_name"], "老用户")
        finally:
            migrated.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "identity.db")
        store = IdentityStore(db_path)
        try:
            test_dirty_names()
            identity_id = test_learn_and_link(store)
            test_name_rules(store, identity_id)
            test_resolve(store, identity_id)
            test_merge_undo(store, identity_id)
            test_note(store, identity_id)
            test_alias(store, identity_id)
            test_alias_merge(store, identity_id)
            test_listing(store, identity_id)
            test_export_import(store, identity_id)
            test_relevant_members(store, identity_id)
            test_forget_and_roster(store, identity_id)
            section("统计")
            print(" ", store.stats())
        finally:
            store.close()
    test_migration()
    print("\n全部通过")


if __name__ == "__main__":
    main()
