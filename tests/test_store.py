"""认人本自测：零第三方依赖，直接 ``python tests/test_store.py`` 运行。

覆盖：脏名过滤、跨 scope 自动归并、改名不覆盖主名、来源等级升级、三种解析方式、
合并与撤销、删除、名册生成。任何一项失败都会抛 AssertionError 并以非零码退出。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import (  # noqa: E402
    SOURCE_ADMIN,
    SOURCE_SELF,
    IdentityStore,
    is_openid_like,
    valid_name,
)

PLATFORM = "qq_official"
GROUP_A = "AAAA0000BBBB1111CCCC2222DDDD3333"
GROUP_B = "EEEE4444FFFF5555AAAA6666BBBB7777"
USER_A = "0123456789ABCDEF0123456789ABCDEF"
USER_B = "FEDCBA9876543210FEDCBA9876543210"


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
            test_forget_and_roster(store, identity_id)
            section("统计")
            print(" ", store.stats())
        finally:
            store.close()
    print("\n全部通过")


if __name__ == "__main__":
    main()
