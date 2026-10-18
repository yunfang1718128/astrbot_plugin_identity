"""认人本：记住「谁是哪个 openid」。

为什么需要这个插件
------------------
QQ 官方号的身份信息很残缺，实测结论：

- **群聊**：网关会带 ``author.username``，AstrBot 补丁后能拿到昵称（历史 289 条群消息
  全部有名字），这是目前唯一稳定的名字来源。
- **私聊**：C2C 协议只有 ``user_openid``，**永远没有昵称**。
- **群成员列表/详情 API 未开放**（官方标注"内邀接入中"），所以只能被动学习，
  不能主动拉名单。
- **同一个人的 member_openid 在不同群不同**（官方"唯一身份机制"明示），
  所以跨群同一人**只能人工合并**。

因此本插件做三件事：群聊被动学名、私聊借用映射、管理员手动绑定与合并。
它只管「身份」，不管「事件记忆」（那是 livingmemory 的职责）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

from .store import (
    ALIAS_MAX_CHARS,
    SOURCE_ADMIN,
    SOURCE_LABEL,
    IdentityStore,
    valid_alias,
)

PLUGIN_NAME = "astrbot_plugin_identity"
SCOPE_PRIVATE = "c2c"


class IdentityPlugin(Star):
    """openid ↔ 人名 的持久记忆。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir = data_dir
        self.store = IdentityStore(
            data_dir / "identity.db",
            placeholder_prefix=self.config.get("placeholder_prefix", "群友-"),
        )

    async def terminate(self) -> None:
        self.store.close()

    # ---------------------------------------------------------------- 工具
    def _enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    def _protected(self, openid: str) -> bool:
        ids = self.config.get("protected_ids", []) or []
        return openid in {str(item).strip() for item in ids}

    @staticmethod
    def _scope(event: AstrMessageEvent) -> str:
        """当前会话作用域：群 openid，或 c2c。"""
        return event.get_group_id() or SCOPE_PRIVATE

    @staticmethod
    def _argv(event: AstrMessageEvent) -> list[str]:
        """把 '/我是 云帆少年' 拆成 ['我是', '云帆少年']。"""
        text = (event.message_str or "").strip()
        while text[:1] in ("/",):
            text = text[1:].lstrip()
        return text.split()

    @staticmethod
    def _short(openid: str) -> str:
        return openid[:8]

    @staticmethod
    def _when(ts: float | None) -> str:
        """时间戳 → 「月-日 时:分」，本地时区。"""
        if not ts:
            return "未知"
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))

    @staticmethod
    def _display(identity: dict[str, Any] | None, fallback: str = "（未知）") -> str:
        """展示/注入用名字：设了管理员别名就用别名，否则用主名。"""
        if not identity:
            return fallback
        return identity.get("alias") or identity["primary_name"]

    def _card(self, identity: dict[str, Any]) -> str:
        """把身份渲染成一行人类可读的卡。"""
        aliases = "、".join(identity["aliases"][:4])
        if identity.get("alias"):
            head = f"#{identity['id']} {identity['alias']}（原名：{identity['primary_name']}"
            head += f"｜曾用名：{aliases}）" if aliases else "）"
        else:
            head = f"#{identity['id']} {identity['primary_name']}"
            if aliases:
                head += f"（曾用名：{aliases}）"
        if identity.get("note"):
            head += f"｜备注：{identity['note']}"
        scopes = "，".join(
            f"{'私聊' if scope == SCOPE_PRIVATE else scope[:8]}={self._short(openid)}"
            for scope, openid, _ in identity["accounts"][:4]
        )
        source = SOURCE_LABEL.get(identity["source"], str(identity["source"]))
        return f"{head}｜{scopes}｜名字来源：{source}"

    def _note_suffix(self, note: str | None) -> str:
        """备注进提示词前截断，避免 token 失控。"""
        if not note or not self.config.get("note_inject_enabled", True):
            return ""
        return f"｜{note[:30]}"

    # ------------------------------------------------------------ 被动学习
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def learn(self, event: AstrMessageEvent) -> None:
        """每条消息都顺手学一次：谁、在哪个群、叫什么。"""
        if not self._enabled():
            return
        try:
            openid = event.get_sender_id()
            if not openid or openid == event.get_self_id():
                return
            if self._protected(openid):
                return

            platform = event.get_platform_name()
            scope = self._scope(event)
            group = getattr(event.message_obj, "group", None)
            if scope != SCOPE_PRIVATE and group is not None:
                group_name = getattr(group, "group_name", None)
                self.store.touch_group(scope, group_name)

            self.store.touch_account(
                platform=platform,
                scope=scope,
                openid=openid,
                nickname=event.get_sender_name(),
            )
        except Exception as exc:  # 学习失败绝不能影响正常对话
            logger.warning(f"[identity] 学习身份失败: {exc}")

    # -------------------------------------------------------------- 注入
    @filter.on_llm_request()
    async def inject_roster(self, event: AstrMessageEvent, req: Any) -> None:
        """把身份信息塞进 system prompt —— 前缀稳定，prompt cache 才命中。"""
        if not self._enabled() or not self.config.get("inject_enabled", True):
            return
        try:
            platform = event.get_platform_name()
            scope = self._scope(event)
            openid = event.get_sender_id()
            lines: list[str] = []

            own_id = self.store.identity_of(platform, scope, openid) if openid else None
            if scope == SCOPE_PRIVATE:
                # 私聊没有昵称，唯一有用的是「当前这个人是谁」
                own = self.store.get_identity(own_id) if own_id else None
                if own is not None and own["source"] > 0:
                    head = f"当前对话者：{self._display(own)}"
                    # 有别名时不再附长昵称/曾用名：那正是要省掉的部分。
                    if not own.get("alias") and own["aliases"]:
                        head += f"（曾用名：{'、'.join(own['aliases'][:3])}）"
                    lines.append(head + self._note_suffix(own.get("note")))
            else:
                days = int(self.config.get("roster_days", 30))
                limit = int(self.config.get("roster_max", 20))
                since = time.time() - days * 86400
                for item in self.store.roster(scope, since, limit):
                    line = f"{self._short(item['openid'])}={item['name']}"
                    if item["aliases"]:
                        line += f"（曾用名：{'、'.join(item['aliases'])}）"
                    line += self._note_suffix(item.get("note"))
                    lines.append(line)
                if own_id and all(
                    not line.startswith(self._short(openid)) for line in lines
                ):
                    own = self.store.get_identity(own_id)
                    if own is not None:
                        lines.append(
                            f"{self._short(openid)}={self._display(own)}（当前发言人）"
                        )

            if lines:
                req.system_prompt += (
                    "\n\n### 群成员身份表（短码=人名）\n"
                    "以下短码是群成员的唯一标识，可用它指代具体的人；"
                    "表里没有的人，直接问或让对方自报，不要猜。\n"
                    + "\n".join(lines)
                    + "\n"
                )
        except Exception as exc:
            logger.warning(f"[identity] 注入名册失败: {exc}")

    # -------------------------------------------------------------- 指令
    @filter.command("我是谁")
    async def whoami(self, event: AstrMessageEvent):
        """查看自己的 openid 与已识别的身份。"""
        openid = event.get_sender_id()
        if not openid:
            yield event.plain_result("拿不到你的 openid，这个平台可能不支持。")
            return
        scope = self._scope(event)
        where = "私聊" if scope == SCOPE_PRIVATE else f"群 {self._short(scope)}"
        platform = event.get_platform_name()
        identity_id = self.store.identity_of(platform, scope, openid)
        body = self.store.get_identity(identity_id) if identity_id else None
        if body is None or body["source"] == 0:
            yield event.plain_result(
                f"{where}｜你的短码：{self._short(openid)}\n我还不认识你。管理员可用 "
                f"/我是 {where if scope != SCOPE_PRIVATE else '群短码'} 你的名字 来登记。"
            )
            return
        yield event.plain_result(f"{where}｜{self._card(body)}")

    @filter.command("我是")
    async def i_am(self, event: AstrMessageEvent):
        """管理员：把当前身份绑定到某人。用法 /我是 <名字> 或 /我是 <群> <名字>。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能改身份库。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            yield event.plain_result(
                "用法：/我是 <名字>\n      /我是 <群名或群短码> <名字>\n"
                "先 /群列表 看群短码。"
            )
            return

        platform = event.get_platform_name()
        scope = self._scope(event)
        openid = event.get_sender_id()

        look_scope: str | None = None
        name = argv[-1]
        if len(argv) >= 2:
            group = self.store.find_group(argv[0])
            if group is None:
                yield event.plain_result(
                    f"找不到群「{argv[0]}」。用 /群列表 查看已知群及短码。"
                )
                return
            look_scope = group["group_openid"]

        candidates = self.store.resolve(name, scope=look_scope)
        if not candidates:
            yield event.plain_result(
                f"没找到叫「{name}」的人。可能需要先让对方在群里说句话让我学到他。"
            )
            return
        if len(candidates) > 1:
            lines = [self._card(self.store.get_identity(cid)) for cid in candidates[:6]]
            yield event.plain_result(
                "同名的人有多个，请用 #编号 指定：\n" + "\n".join(lines)
            )
            return

        target_id = candidates[0]
        current_id = self.store.identity_of(platform, scope, openid)
        self.store.record_name(target_id, name, SOURCE_ADMIN)

        if current_id is None:
            yield event.plain_result(
                f"你这条消息还没被登记（可能刚重启），再说一句话后再试一次。"
            )
            return
        if current_id == target_id:
            yield event.plain_result(f"已经对上了：{self._card(self.store.get_identity(target_id))}")
            return

        result = self.store.merge(
            keep_id=min(target_id, current_id),
            drop_id=max(target_id, current_id),
            operator=f"{platform}:{openid}",
        )
        merged = self.store.get_identity(result["keep"])
        yield event.plain_result(
            f"对上了：{self._card(merged)}\n（本次合并可用 /撤销 回滚）"
        )

    @filter.command("群名")
    async def name_group(self, event: AstrMessageEvent):
        """管理员：给当前群起一个中文名，方便私聊里寻址。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能命名群。")
            return
        if self._scope(event) == SCOPE_PRIVATE:
            yield event.plain_result("这条指令要在群里发。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            yield event.plain_result("用法：/群名 某某群")
            return
        scope = self._scope(event)
        new_name = " ".join(argv)
        self.store.set_group_name(scope, new_name)
        yield event.plain_result(f"已把本群记作「{new_name}」，短码 {self._short(scope)}。")

    @filter.command("群列表")
    async def list_groups(self, event: AstrMessageEvent):
        """管理员：查看已知群。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能看群列表。")
            return
        groups = self.store.list_groups()
        if not groups:
            yield event.plain_result("还没记录到任何群。")
            return
        lines = []
        for row in groups[:20]:
            count = len(self.store.roster(row["group_openid"], 0, 999))
            lines.append(
                f"{self._short(row['group_openid'])}｜{row['name'] or '（未命名）'}"
                f"｜认识 {count} 人"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("谁")
    async def who_is(self, event: AstrMessageEvent):
        """管理员：按名字 / 短码 / #编号 查身份卡。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能查身份库。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            yield event.plain_result("用法：/谁 <名字 或 短码 或 #编号>")
            return
        token = " ".join(argv)
        candidates = self.store.resolve(token)
        if not candidates:
            yield event.plain_result(f"没找到「{token}」。")
            return
        cards = [self._card(self.store.get_identity(cid)) for cid in candidates[:8]]
        suffix = "" if len(candidates) <= 8 else f"\n…共 {len(candidates)} 条"
        yield event.plain_result("\n".join(cards) + suffix)

    @filter.command("合并")
    async def merge_people(self, event: AstrMessageEvent):
        """管理员：把两个身份合并成一个人（跨群关联）。用法 /合并 A B。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能合并身份。")
            return
        argv = self._argv(event)[1:]
        if len(argv) != 2:
            yield event.plain_result("用法：/合并 <A> <B>，A/B 可以是名字、短码或 #编号。")
            return
        resolved: list[int] = []
        for token in argv:
            found = self.store.resolve(token)
            if len(found) != 1:
                yield event.plain_result(
                    f"「{token}」匹配到 {len(found)} 个身份，请用 /谁 {token} 查明后用 #编号 指定。"
                )
                return
            resolved.append(found[0])
        if resolved[0] == resolved[1]:
            yield event.plain_result("这两个指的是同一个人，不用合并。")
            return
        keep_id, drop_id = min(resolved), max(resolved)
        result = self.store.merge(keep_id, drop_id, f"{event.get_platform_name()}:{event.get_sender_id()}")
        merged = self.store.get_identity(result["keep"])
        moved = len(result["accounts"])
        yield event.plain_result(
            f"已合并：{self._card(merged)}\n搬了 {moved} 个账号、"
            f"{len(result['added_names'])} 个新名字。可用 /撤销 回滚。"
        )

    @filter.command("撤销")
    async def undo(self, event: AstrMessageEvent):
        """管理员：回滚最近一次合并。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能回滚。")
            return
        payload = self.store.undo_last(f"{event.get_platform_name()}:{event.get_sender_id()}")
        if payload is None:
            yield event.plain_result("没有可撤销的合并。")
            return
        restored = self.store.get_identity(payload["drop"]["id"])
        yield event.plain_result(
            f"已回滚，拆回：{self._card(restored) if restored else payload['drop']['primary_name']}"
        )

    @filter.command("备注")
    async def set_note(self, event: AstrMessageEvent):
        """管理员：查看 / 设置 / 清空某人的一句备注。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能写备注。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            yield event.plain_result(
                "用法：/备注 <名字或短码>          查看\n"
                "      /备注 <名字或短码> <一句话>  设置\n"
                "      /备注 <名字或短码> -        清空"
            )
            return
        target = argv[0]
        candidates = self.store.resolve(target)
        if not candidates:
            yield event.plain_result(f"没找到「{target}」。")
            return
        if len(candidates) > 1:
            cards = [self._card(self.store.get_identity(cid)) for cid in candidates[:6]]
            yield event.plain_result(
                "匹配到多个人，请用 #编号 指定：\n" + "\n".join(cards)
            )
            return
        identity_id = candidates[0]
        card = self.store.get_identity(identity_id)
        if len(argv) == 1:
            note = (card.get("note") or "（暂无备注）") if card else "（暂无备注）"
            yield event.plain_result(f"{self._display(card)} 的备注：{note}")
            return
        content = " ".join(argv[1:]).strip()
        if content in ("-", "清空", "删除"):
            self.store.set_note(identity_id, None)
            yield event.plain_result(f"已清空「{self._display(card)}」的备注。")
            return
        self.store.set_note(identity_id, content[:60])
        yield event.plain_result(
            f"已记下「{self._display(card)}」的备注：{content[:60]}\n"
            "（备注会随名册注入给模型，模型可能把它说出来，注意别写敏感信息）"
        )

    @filter.command("别名")
    async def set_alias(self, event: AstrMessageEvent):
        """管理员：给某人设一个短别名（长昵称不好打，还费 token）。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能设别名。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            items = self.store.list_aliases()
            if not items:
                yield event.plain_result(
                    "还没有人设别名。用法：/别名 <名字或短码> <短名>"
                )
                return
            lines = [
                f"#{item['id']} {item['alias']}（原名：{item['primary_name']}）"
                for item in items
            ]
            yield event.plain_result("已设别名：\n" + "\n".join(lines))
            return

        target = argv[0]
        candidates = self.store.resolve(target)
        if not candidates:
            yield event.plain_result(f"没找到「{target}」。")
            return
        if len(candidates) > 1:
            cards = [self._card(self.store.get_identity(cid)) for cid in candidates[:6]]
            yield event.plain_result(
                "匹配到多个人，请用 #编号 指定：\n" + "\n".join(cards)
            )
            return
        identity_id = candidates[0]
        card = self.store.get_identity(identity_id)

        if len(argv) == 1:
            current = card.get("alias") or "（未设置）"
            yield event.plain_result(
                f"#{identity_id}｜原名：{card['primary_name']}｜别名：{current}\n"
                f"设置：/别名 {target} <短名>　清空：/别名 {target} -"
            )
            return

        content = " ".join(argv[1:]).strip()
        if content in ("-", "清空", "删除"):
            self.store.set_alias(identity_id, None)
            yield event.plain_result(f"已清空「{card['primary_name']}」的别名。")
            return

        alias = valid_alias(content)
        if alias is None:
            yield event.plain_result(
                f"别名不合格：要 1~{ALIAS_MAX_CHARS} 个字、单行、不能长得像 ID。"
            )
            return
        owner = self.store.alias_owner(alias, exclude_id=identity_id)
        if owner is not None:
            owner_card = self.store.get_identity(owner)
            yield event.plain_result(
                f"别名「{alias}」已被 #{owner} {self._display(owner_card)} 占用，换一个。"
            )
            return
        # 与别人的名字/曾用名重名会让 /谁 /备注 这类指令产生歧义，直接拦住。
        if any(cid != identity_id for cid in self.store.resolve(alias)):
            yield event.plain_result(
                f"「{alias}」已经是别人的名字，换一个（避免 /谁 /备注 解析歧义）。"
            )
            return
        self.store.set_alias(identity_id, alias)
        yield event.plain_result(
            f"已给 #{identity_id} 设别名「{alias}」（原名：{card['primary_name']}）。\n"
            "名册注入、/谁、/备注 都已改用这个短名。"
        )

    @filter.command("名单")
    async def list_people(self, event: AstrMessageEvent):
        """管理员：分页列出身份库里的人。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能看名单。")
            return
        argv = self._argv(event)[1:]
        page = 1
        if argv:
            raw = argv[0].lstrip("#")
            if not raw.isdigit():
                yield event.plain_result("用法：/名单 [页码]，每页 10 人。")
                return
            page = max(1, int(raw))
        total = self.store.count_identities()
        if total == 0:
            yield event.plain_result("身份库还是空的。")
            return
        size = 10
        pages = (total + size - 1) // size
        page = min(page, pages)
        rows = self.store.page_identities(size, (page - 1) * size)
        lines = []
        for row in rows:
            head = f"#{row['id']} {row['display_name']}"
            if row["alias"]:
                head += f"（原名：{row['primary_name']}）"
            line = f"{head}｜{row['accounts']} 个账号｜{self._when(row['last_seen'])}"
            if row["note"]:
                line += f"｜{row['note'][:20]}"
            lines.append(line)
        tail = f"\n下一页：/名单 {page + 1}" if page < pages else ""
        yield event.plain_result(
            f"身份库 {total} 人（第 {page}/{pages} 页）\n" + "\n".join(lines) + tail
        )

    @filter.command("曾用名")
    async def name_history(self, event: AstrMessageEvent):
        """管理员：看某人用过的全部名字与时间线。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能查名字历史。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            yield event.plain_result("用法：/曾用名 <名字 或 短码 或 #编号>")
            return
        token = " ".join(argv)
        candidates = self.store.resolve(token)
        if not candidates:
            yield event.plain_result(f"没找到「{token}」。")
            return
        if len(candidates) > 1:
            cards = [self._card(self.store.get_identity(cid)) for cid in candidates[:6]]
            yield event.plain_result(
                "匹配到多个人，请用 #编号 指定：\n" + "\n".join(cards)
            )
            return
        identity_id = candidates[0]
        card = self.store.get_identity(identity_id)
        history = self.store.name_history(identity_id)
        if not history:
            yield event.plain_result(
                f"#{identity_id} {self._display(card)} 还没学到名字（只有占位名）。"
            )
            return
        lines = []
        for item in history:
            label = SOURCE_LABEL.get(item["source"], str(item["source"]))
            line = (
                f"{item['name']}（{label}）"
                f" {self._when(item['first_seen'])} → {self._when(item['last_seen'])}"
            )
            if item["name"] == card["primary_name"]:
                line += "　← 当前主名"
            lines.append(line)
        yield event.plain_result(
            f"#{identity_id} {self._display(card)} 的名字时间线：\n" + "\n".join(lines)
        )

    @filter.command("导出")
    async def export_backup(self, event: AstrMessageEvent):
        """管理员：把身份库导出成 JSON（落在插件数据目录）。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能导出。")
            return
        try:
            data = self.store.export_data()
            filename = time.strftime("identity-export-%Y%m%d-%H%M%S.json")
            path = self.data_dir / filename
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[identity] 导出失败: {exc}")
            yield event.plain_result(f"导出失败：{exc}")
            return
        stats = data["stats"]
        yield event.plain_result(
            f"已导出 {stats['identities']} 人 / {stats['accounts']} 个账号 / "
            f"{stats['names']} 个名字到：\n{path}\n"
            f"取回：scp aliyun:{path} ."
        )

    @filter.command("导入")
    async def import_backup(self, event: AstrMessageEvent):
        """管理员：从数据目录里的导出文件合并导入。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能导入。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            files = sorted(
                p.name for p in self.data_dir.glob("identity-export-*.json")
            )
            if not files:
                yield event.plain_result(
                    "数据目录里没有导出文件。先在本机 /导出 再 /导入 <文件名>。"
                )
                return
            yield event.plain_result(
                "用法：/导入 <文件名>\n可用：\n" + "\n".join(files[-10:])
            )
            return

        filename = argv[0]
        if "/" in filename or "\\" in filename or ".." in filename:
            yield event.plain_result("只接受数据目录里的文件名，不能带路径。")
            return
        path = self.data_dir / filename
        if not path.is_file():
            yield event.plain_result(f"找不到文件：{filename}")
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            yield event.plain_result(f"文件解析失败：{exc}")
            return
        try:
            result = self.store.import_data(payload)
        except ValueError as exc:
            yield event.plain_result(f"导入失败：{exc}")
            return
        yield event.plain_result(
            f"导入完成：新增 {result['identities']} 人、{result['accounts']} 个账号、"
            f"{result['names']} 个名字，跳过 {result['skipped']} 条。"
        )

    @filter.command("忘记")
    async def forget(self, event: AstrMessageEvent):
        """管理员：彻底删除某人的身份记录（数据删除权）。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能删除身份记录。")
            return
        argv = self._argv(event)[1:]
        if not argv:
            yield event.plain_result("用法：/忘记 <名字 或 短码 或 #编号>")
            return
        token = " ".join(argv)
        candidates = self.store.resolve(token)
        if not candidates:
            yield event.plain_result(f"没找到「{token}」。")
            return
        if len(candidates) > 1:
            cards = [self._card(self.store.get_identity(cid)) for cid in candidates[:6]]
            yield event.plain_result(
                "匹配到多个人，请用 #编号 指定要删除哪个：\n" + "\n".join(cards)
            )
            return
        info = self.store.forget(candidates[0])
        yield event.plain_result(
            f"已删除「{info['display_name']}」的全部记录："
            f"{info['accounts']} 个账号、{info['names']} 个名字。"
        )
