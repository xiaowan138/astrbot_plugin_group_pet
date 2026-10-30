"""AstrBot 群宠物养成插件。

领养一只宠物，喂食、玩耍、打工、冒险，帮它升级。
宠物会随时间变饿变无聊，太久不管会离家出走。

指令（默认需带唤醒前缀，如 /宠物）：
  宠物 / 我的宠物          查看宠物状态（无宠物时显示领养引导）
  宠物领养 <名字>          领养一只随机品种的宠物
  宠物改名 <名字>          给宠物改名（有冷却）
  宠物喂食 [食物]          花金币喂食，或消耗背包里的食物
  宠物玩耍 [玩具]          陪宠物玩，或消耗背包里的玩具
  宠物打工                 让宠物打工赚金币
  宠物冒险                 随机冒险事件，有收益也有风险
  宠物商店                 查看可购买的食物与玩具
  宠物购买 <商品>          买一个食物/玩具放进背包
  宠物签到                 每日签到领金币
  宠物任务                 查看今日任务进度
  宠物图鉴                 查看品种收集进度
  宠物成就                 查看成就进度
  宠物排行                 查看等级排行（群聊内为群排行）
  宠物放生 确认            放生宠物（需二次确认）
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .pet_store import PetStore, clamp

# ---------------- 品种设定 ----------------
# hunger_decay / mood_decay: 后台衰减倍率；work/play/coin/exp: 对应收益加成
SPECIES: dict[str, dict] = {
    "仓鼠": {"emoji": "🐹", "hunger_decay": 0.5, "mood_decay": 1.0,
             "work": 0.0, "play": 0.0, "coin": 0.0, "exp": 0.0,
             "trait": "腮帮子里全是粮，饿得比别的宠物慢"},
    "柴犬": {"emoji": "🐕", "hunger_decay": 1.0, "mood_decay": 1.0,
             "work": 0.2, "play": 0.0, "coin": 0.0, "exp": 0.0,
             "trait": "打工小能手，打工收益 +20%"},
    "布偶猫": {"emoji": "🐱", "hunger_decay": 1.0, "mood_decay": 1.0,
              "work": 0.0, "play": 0.3, "coin": 0.0, "exp": 0.0,
              "trait": "撒娇大师，玩耍效果 +30%"},
    "兔子": {"emoji": "🐰", "hunger_decay": 1.0, "mood_decay": 1.0,
             "work": 0.0, "play": 0.0, "coin": 0.25, "exp": 0.0,
             "trait": "脚印里藏着运气，冒险捡到的金币 +25%"},
    "小狐狸": {"emoji": "🦊", "hunger_decay": 1.0, "mood_decay": 1.0,
              "work": 0.0, "play": 0.0, "coin": 0.0, "exp": 0.3,
              "trait": "机灵鬼一个，冒险获得的经验 +30%"},
    "熊猫": {"emoji": "🐼", "hunger_decay": 1.0, "mood_decay": 0.5,
             "work": 0.0, "play": 0.0, "coin": 0.0, "exp": 0.0,
             "trait": "心态大师，心情流失减半"},
}
DEFAULT_SPECIES = SPECIES["仓鼠"]

WORK_FLAVOR = ("搬砖", "送外卖", "摆摊卖炒酸奶", "帮邻居家遛狗", "直播带货",
               "给代码群当客服", "捡废品攒钱")
ADVENTURE_TEXTS = {
    "little": ("在草丛里翻到了几枚硬币", "帮迷路的小鸟找到了家，收到答谢礼",
               "捡到一个被人落下的钱包（只有零钱）", "抓虫子技术大增"),
    "big": ("发现了一个被遗忘的松鼠仓库", "挖到一小罐埋在树下的金币",
            "帮商人找回了走失的羊驼，获得丰厚报酬"),
    "danger": ("被一只大鹅追了三条街", "踩进水坑，浑身湿透",
               "被蜜蜂蛰了一下鼻子"),
    "nothing": ("围着大树转了三圈，一无所获", "睡了一下午觉",
                "追自己的尾巴追到头晕"),
    "treasure": ("挖到了传说中的宝藏箱！！", "捡到一个鼓鼓的钱包，失主重金答谢！"),
}

# ---------------- 商店 / 每日任务 / 成就 ----------------
# kind: food 只能用 宠物喂食；toy 只能用 宠物玩耍
SHOP: dict[str, dict] = {
    "小鱼干": {"kind": "food", "price": 30, "hunger": 20, "mood": 5, "exp": 8},
    "高级大餐": {"kind": "food", "price": 80, "hunger": 60, "mood": 20, "exp": 20},
    "毛线球": {"kind": "toy", "price": 40, "mood": 40, "exp": 10},
    "逗猫棒": {"kind": "toy", "price": 70, "mood": 65, "exp": 15},
}
ITEM_LIMIT = 99

DAILY_TASKS: dict[str, str] = {
    "feed": "喂食",
    "play": "玩耍",
    "work": "打工",
    "adventure": "冒险",
}

ACHIEVEMENTS: dict[str, dict] = {
    "first": {"name": "初次相遇", "desc": "领养第一只宠物"},
    "rename": {"name": "新名字新气象", "desc": "给宠物改一次名字"},
    "feed": {"name": "大胃王", "desc": "累计喂食 50 次"},
    "play": {"name": "最佳玩伴", "desc": "累计玩耍 50 次"},
    "work": {"name": "打工达人", "desc": "累计打工 30 次"},
    "adv": {"name": "探险家", "desc": "累计冒险 30 次"},
    "dex": {"name": "图鉴收集者", "desc": "集齐 6 个品种"},
    "rich": {"name": "小富翁", "desc": "单只宠物金币达到 500"},
    "loyal": {"name": "长情陪伴", "desc": "同一只宠物陪伴满 7 天"},
}


def need_exp(level: int) -> int:
    return int(level) * 50


def bar(value: float, width: int = 10) -> str:
    filled = round(clamp(value) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def fmt_cooldown(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}小时{seconds % 3600 // 60}分"
    if seconds >= 60:
        return f"{seconds // 60}分{seconds % 60}秒"
    return f"{seconds}秒"


def state_text(pet: dict) -> str:
    if pet.get("starve_since"):
        return "⚠️ 已经饿坏了，再不管它就要离家出走了！"
    hunger, mood = pet["hunger"], pet["mood"]
    if hunger >= 60 and mood >= 60:
        return "今天也是元气满满的一天～"
    if hunger < 20:
        return "肚子饿得咕咕叫，快喂点吃的吧！"
    if mood < 20:
        return "无聊得开始拆家了，快陪它玩玩！"
    if hunger < 40:
        return "有点饿了，想吃东西。"
    if mood < 40:
        return "有点闷，想出去玩。"
    return "平平安安，无忧无虑。"


def status_panel(pet: dict, extra: list[str] | None = None) -> str:
    sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)
    level_need = max(1, need_exp(pet["level"]))
    lines = [
        f"🐾 {pet['name']} · {pet['species']} {sp['emoji']} · Lv.{pet['level']}",
        "━━━━━━━━━━━━━━━",
        f"经验 {pet['exp']}/{level_need}  {bar(pet['exp'] * 100 // level_need)}",
        f"饱食 {round(pet['hunger'])}/100  {bar(pet['hunger'])}",
        f"心情 {round(pet['mood'])}/100  {bar(pet['mood'])}",
        f"钱包 💰 {pet['coins']}",
    ]
    if extra:
        lines.extend(extra)
    lines.extend([
        "━━━━━━━━━━━━━━━",
        f"特性：{sp['trait']}",
        f"状态：{state_text(pet)}",
    ])
    return "\n".join(lines)


def today_str() -> str:
    return time.strftime("%Y-%m-%d")


def yesterday_str() -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))


def item_effect(info: dict) -> str:
    parts = []
    if info.get("hunger"):
        parts.append(f"饱食+{info['hunger']}")
    if info.get("mood"):
        parts.append(f"心情+{info['mood']}")
    if info.get("exp"):
        parts.append(f"经验+{info['exp']}")
    return " ".join(parts)


@register(
    "astrbot_plugin_group_pet",
    "xiaowan",
    "群宠物养成：领养、喂食、打工、冒险、商店、签到、排行，太久不管会离家出走",
    "v1.1.0",
)
class GroupPetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_group_pet"
        self.store = PetStore(data_dir / "pets.db")
        self._lock = asyncio.Lock()
        self._decay_task: asyncio.Task | None = None

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return max(0, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    # ================= 生命周期 =================

    async def initialize(self) -> None:
        self._decay_task = asyncio.create_task(self._decay_loop())
        logger.info(f"[群宠物] 已加载，当前共有 {self.store.count()} 只宠物")

    async def terminate(self) -> None:
        if self._decay_task:
            self._decay_task.cancel()
            try:
                await self._decay_task
            except asyncio.CancelledError:
                pass
            self._decay_task = None
        self.store.close()
        logger.info("[群宠物] 已卸载")

    async def _decay_loop(self) -> None:
        while True:
            # 每轮重新读配置，改衰减间隔不用重载插件
            interval = max(5, self._cfg_int("decay_interval_min", 30)) * 60
            await asyncio.sleep(interval)
            try:
                await self._decay_once()
            except Exception as e:
                logger.error(f"[群宠物] 状态衰减出错: {e!r}")

    async def _decay_once(self) -> None:
        now = time.time()
        step = self._cfg_int("decay_hunger", 2)
        runaway_hours = self._cfg_int("runaway_hours", 72)
        warn_hours = self._cfg_int("starve_warn_hours", 6)
        changed: list[dict] = []
        runaways: list[dict] = []
        notices: list[tuple[str, str]] = []
        async with self._lock:
            for pet in self.store.all_pets():
                sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)
                pet["hunger"] = clamp(pet["hunger"] - step * sp["hunger_decay"])
                if pet["hunger"] <= 0 and not pet.get("starve_since"):
                    pet["starve_since"] = now
                mood_drop = (1 if pet["hunger"] >= 30 else 3) * sp["mood_decay"]
                pet["mood"] = clamp(pet["mood"] - mood_drop)

                since = pet.get("starve_since")
                if runaway_hours > 0 and since and now - since >= runaway_hours * 3600:
                    # 饿太久：直接在衰减中清理，避免僵尸数据长期占榜
                    runaways.append(pet)
                    continue
                if since and runaway_hours > 0 and warn_hours > 0 and not pet.get("starve_warned"):
                    left = runaway_hours * 3600 - (now - since)
                    if left <= warn_hours * 3600:
                        pet["starve_warned"] = 1
                        left_h = int(left // 3600)
                        left_txt = "不到 1 小时" if left_h <= 0 else f"约 {left_h} 小时"
                        notices.append((
                            pet.get("umo", ""),
                            f"⚠️ {pet['name']}已经饿坏了，再过{left_txt}就要离家出走了，快喂喂它！",
                        ))
                changed.append(pet)

            self.store.save_pets(changed)
            if runaways:
                self.store.delete_pets([p["owner_id"] for p in runaways])
                for p in runaways:
                    notices.append((
                        p.get("umo", ""),
                        f"😢 {p['name']}已经饿了太久，收拾好小包袱离家出走了……\n"
                        f"（宠物已离开，可重新使用 宠物领养 开启新的生活）",
                    ))

        for umo, text in notices:
            await self._push(umo, text)

    async def _push(self, umo: str, text: str) -> None:
        """主动推送消息；平台不支持或无会话时静默跳过。"""
        if not umo or self.context is None:
            return
        try:
            try:
                from astrbot.api.event import MessageChain
            except ImportError:
                from astrbot.core.message.message_event_result import MessageChain
            from astrbot.api.message_components import Plain

            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as e:
            logger.error(f"[群宠物] 主动消息发送失败: {e!r}")

    # ================= 内部工具 =================

    def _home_group(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        return str(gid) if gid else ""

    def _touch(self, event: AstrMessageEvent, pet: dict) -> None:
        """更新主动推送地址；宠物首次进入群聊时绑定群，之后不再漂移。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            pet["umo"] = umo
        if not pet.get("group_id"):
            gid = self._home_group(event)
            if gid:
                pet["group_id"] = gid

    def _cooldown_left(self, pet: dict, action: str, seconds: float) -> int:
        last = pet["cooldowns"].get(action, 0)
        return max(0, int(last + seconds - time.time()))

    def _set_cooldown(self, pet: dict, action: str) -> None:
        pet["cooldowns"][action] = time.time()

    def _gain_exp(self, pet: dict, exp: int) -> int:
        """加经验并处理升级，返回升级次数。"""
        pet["exp"] += int(exp)
        levels = 0
        while pet["exp"] >= need_exp(pet["level"]):
            pet["exp"] -= need_exp(pet["level"])
            pet["level"] += 1
            pet["coins"] += self._cfg_int("levelup_coin_bonus", 20)
            levels += 1
        return levels

    def _levelup_text(self, pet: dict, levels: int) -> str:
        if levels <= 0:
            return ""
        bonus = self._cfg_int("levelup_coin_bonus", 20) * levels
        if levels == 1:
            return f"🎊 {pet['name']}升到了 Lv.{pet['level']}，奖励 💰{bonus}！"
        return f"🎊 {pet['name']}连升 {levels} 级，现在 Lv.{pet['level']}，奖励 💰{bonus}！"

    async def _precheck_runaway(self, pet: dict) -> str | None:
        """饿太久则放宠物离开，返回告别文案；否则返回 None。"""
        hours = self._cfg_int("runaway_hours", 72)
        if hours <= 0:
            return None
        since = pet.get("starve_since")
        if since and time.time() - since >= hours * 3600:
            self.store.delete_pet(pet["owner_id"])
            return (
                f"😢 {pet['name']}已经饿了太久太久，"
                f"它收拾好了小包袱，头也不回地离开了……\n"
                f"（宠物已离家出走，可重新使用 宠物领养 开启新的生活）"
            )
        return None

    # ---------- 档案 / 任务 / 成就 ----------

    def _daily_reset(self, profile: dict) -> None:
        daily = profile.get("daily") or {}
        if daily.get("date") != today_str():
            profile["daily"] = {
                "date": today_str(),
                "feed": 0,
                "play": 0,
                "work": 0,
                "adventure": 0,
                "done": False,
            }

    def _bump_daily(self, profile: dict, action: str) -> bool:
        """记录每日任务进度，全部完成时返回 True（且只返回一次）。"""
        self._daily_reset(profile)
        daily = profile["daily"]
        daily[action] = int(daily.get(action, 0)) + 1
        if not daily.get("done") and all(int(daily.get(k, 0)) > 0 for k in DAILY_TASKS):
            daily["done"] = True
            return True
        return False

    def _daily_text(self, profile: dict) -> str:
        self._daily_reset(profile)
        daily = profile["daily"]
        if daily.get("done"):
            return "全部完成 ✅"
        marks = " ".join(
            f"{label}{'✅' if int(daily.get(k, 0)) > 0 else '⬜'}"
            for k, label in DAILY_TASKS.items()
        )
        count = sum(1 for k in DAILY_TASKS if int(daily.get(k, 0)) > 0)
        return f"{marks}（{count}/{len(DAILY_TASKS)}）"

    def _ach_states(self, pet: dict | None, profile: dict) -> dict[str, tuple[bool, str]]:
        stats = profile.get("stats") or {}
        seen = profile.get("seen_species") or []
        coins = int(pet["coins"]) if pet else 0
        age_days = int((time.time() - pet["adopt_time"]) // 86400) if pet else 0

        def progress(value: int, target: int) -> int:
            return max(0, min(int(value), target))

        feed = int(stats.get("feed", 0))
        play = int(stats.get("play", 0))
        work = int(stats.get("work", 0))
        adv = int(stats.get("adventure", 0))
        rename = int(stats.get("rename", 0))
        states = {
            "first": (bool(seen), f"{1 if seen else 0}/1"),
            "rename": (rename >= 1, f"{progress(rename, 1)}/1"),
            "feed": (feed >= 50, f"{progress(feed, 50)}/50"),
            "play": (play >= 50, f"{progress(play, 50)}/50"),
            "work": (work >= 30, f"{progress(work, 30)}/30"),
            "adv": (adv >= 30, f"{progress(adv, 30)}/30"),
            "dex": (len(seen) >= len(SPECIES), f"{len(seen)}/{len(SPECIES)}"),
            "rich": (coins >= 500, f"{progress(coins, 500)}/500"),
            "loyal": (age_days >= 7, f"{progress(age_days, 7)}/7 天"),
        }
        # 已解锁的成就不因金币变少等可逆条件而重新变灰
        unlocked = set(profile.get("achievements") or [])
        return {
            key: (ok or key in unlocked, text)
            for key, (ok, text) in states.items()
        }

    def _achievement_updates(self, pet: dict | None, profile: dict) -> list[str]:
        states = self._ach_states(pet, profile)
        unlocked = set(profile.get("achievements") or [])
        newly = [k for k, (ok, _) in states.items() if ok and k not in unlocked]
        if not newly:
            return []
        profile.setdefault("achievements", []).extend(newly)
        return [f"🎖️ 成就解锁：{ACHIEVEMENTS[k]['name']}" for k in newly]

    def _post_action(self, pet: dict, profile: dict, action: str) -> list[str]:
        """成功执行一次行为后：记统计、记每日任务、检查成就。"""
        stats = profile.setdefault("stats", {})
        stats[action] = int(stats.get(action, 0)) + 1
        lines: list[str] = []
        if self._bump_daily(profile, action):
            coins = self._cfg_int("task_coins", 30)
            pet["coins"] += coins
            levels = self._gain_exp(pet, 20)
            lines.append(f"📋 今日任务全部完成！奖励 💰{coins} + 经验 20")
            if levels:
                lines.append(self._levelup_text(pet, levels))
        lines.extend(self._achievement_updates(pet, profile))
        return lines

    @staticmethod
    def _inv_text(pet: dict) -> str:
        items = pet.get("items") or {}
        if not items:
            return "空"
        return "  ".join(f"{name}×{count}" for name, count in items.items())

    def _panel(self, pet: dict, profile: dict) -> str:
        return status_panel(pet, [
            f"背包 🎒 {self._inv_text(pet)}",
            f"今日任务 {self._daily_text(profile)}",
        ])

    # ================= 指令 =================

    @filter.command("宠物", alias={"我的宠物"})
    async def pet_home(self, event: AstrMessageEvent):
        """查看宠物状态，没有宠物时显示领养引导。"""
        owner = event.get_sender_id()
        gid = self._home_group(event)
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                scope = "本群" if gid else "全服"
                yield event.plain_result(
                    "🏠 宠物领养中心\n"
                    "你还没有宠物哦～发送：宠物领养 <名字>\n"
                    "例如：宠物领养 汤圆\n\n"
                    f"{scope}已有宠物的有：\n"
                    + (self._mini_board(gid) or "　（还没有人领养宠物，快来当第一人！）")
                )
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            profile = self.store.get_profile(owner)
            if pet["species"] not in profile["seen_species"]:
                # 兼容 v1.0.0 老数据：把当前品种补进图鉴
                profile["seen_species"].append(pet["species"])
                self.store.save_profile(profile)
            self.store.save_pet(pet)
            yield event.plain_result(self._panel(pet, profile))

    @filter.command("宠物领养", alias={"领养"})
    async def pet_adopt(self, event: AstrMessageEvent, name: str = ""):
        """领养一只宠物：宠物领养 <名字>"""
        name = (name or "").strip()
        owner = event.get_sender_id()
        if not name:
            yield event.plain_result("请给宠物取个名字：宠物领养 <名字>（1~8个字）")
            return
        if len(name) > 8 or "\n" in name:
            yield event.plain_result("名字太长啦，1~8 个字就好。")
            return
        async with self._lock:
            existing = self.store.get_pet(owner)
            if existing:
                farewell = await self._precheck_runaway(existing)
                if farewell:
                    # 旧宠物已经饿走了，告知一句后继续领养，不再卡住用户
                    yield event.plain_result(farewell)
                else:
                    yield event.plain_result(
                        f"你已经有宠物啦：{existing['name']}，一心不能二用哦～\n"
                        f"发送 宠物 查看它的状态。"
                    )
                    return
            species = random.choice(list(SPECIES.keys()))
            pet = self.store.create_pet(owner, name, species, self._home_group(event))
            self._touch(event, pet)
            profile = self.store.get_profile(owner)
            if species not in profile["seen_species"]:
                profile["seen_species"].append(species)
            profile["stats"]["adopt"] = int(profile["stats"].get("adopt", 0)) + 1
            ach = self._achievement_updates(pet, profile)
            self.store.save_profile(profile)
            self.store.save_pet(pet)
            sp = SPECIES[species]
            text = (
                f"🎉 恭喜！你领养到了一只{species} {sp['emoji']}！\n"
                f"它叫「{name}」，特性：{sp['trait']}\n\n"
                f"新手礼包已发放 💰30\n"
                f"记得定时 宠物喂食 / 宠物玩耍，不然它会饿着哦～\n\n"
                f"{self._panel(pet, profile)}"
            )
            if ach:
                text += "\n" + "\n".join(ach)
            yield event.plain_result(text)

    @filter.command("宠物改名")
    async def pet_rename(self, event: AstrMessageEvent, name: str = ""):
        """给宠物改名：宠物改名 <新名字>"""
        name = (name or "").strip()
        owner = event.get_sender_id()
        if not name:
            yield event.plain_result("请给宠物取个新名字：宠物改名 <名字>（1~8个字）")
            return
        if len(name) > 8 or "\n" in name:
            yield event.plain_result("名字太长啦，1~8 个字就好。")
            return
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            cd = self._cooldown_left(
                pet, "rename", self._cfg_int("rename_cooldown_min", 10) * 60
            )
            if cd > 0:
                yield event.plain_result(
                    f"{pet['name']}刚改过名字，{fmt_cooldown(cd)}后再来吧～"
                )
                return
            old = pet["name"]
            pet["name"] = name
            self._set_cooldown(pet, "rename")
            profile = self.store.get_profile(owner)
            profile["stats"]["rename"] = int(profile["stats"].get("rename", 0)) + 1
            ach = self._achievement_updates(pet, profile)
            self.store.save_profile(profile)
            self.store.save_pet(pet)
            text = f"✍️ {old}现在改名叫「{name}」啦！"
            if ach:
                text += "\n" + "\n".join(ach)
            yield event.plain_result(text + f"\n\n{self._panel(pet, profile)}")

    @filter.command("宠物喂食", alias={"喂宠物"})
    async def pet_feed(self, event: AstrMessageEvent, item: str = ""):
        """喂食宠物：不带参数花金币买基础饲料，带食物名则消耗背包道具。"""
        owner = event.get_sender_id()
        item = (item or "").strip()
        cost = self._cfg_int("feed_cost", 5)
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            profile = self.store.get_profile(owner)
            cd = self._cooldown_left(pet, "feed", self._cfg_int("feed_cooldown_s", 60))
            if cd > 0:
                yield event.plain_result(f"{pet['name']}刚吃过东西，{fmt_cooldown(cd)}后再来喂吧～")
                return

            if item:
                info = SHOP.get(item)
                if not info:
                    yield event.plain_result(f"🏪 没有叫「{item}」的东西，发送 宠物商店 看看货架～")
                    return
                if info["kind"] != "food":
                    yield event.plain_result(f"「{item}」是玩具，想玩它请发送：宠物玩耍 {item}")
                    return
                held = int(pet["items"].get(item, 0))
                if held <= 0:
                    yield event.plain_result(
                        f"背包里没有{item}了，发送 宠物购买 {item} 买一个吧～"
                    )
                    return
                if pet["hunger"] >= 100:
                    yield event.plain_result(f"{pet['name']}已经很饱了，先别喂{item}啦～")
                    return
                if held == 1:
                    pet["items"].pop(item, None)
                else:
                    pet["items"][item] = held - 1
                hunger_gain = int(info["hunger"])
                mood_gain = int(info["mood"])
                exp = int(info["exp"])
                headline = f"🍚 {pet['name']}吃掉了{item}！"
            else:
                if pet["coins"] < cost:
                    yield event.plain_result(
                        f"喂食需要 💰{cost}，钱包只剩 💰{pet['coins']}，让宠物去 宠物打工 赚点钱吧！"
                    )
                    return
                pet["coins"] -= cost
                hunger_gain, mood_gain, exp = 35, 5, 5
                headline = f"🍚 你花 💰{cost} 给{pet['name']}买了一顿大餐！"

            pet["hunger"] = clamp(pet["hunger"] + hunger_gain)
            pet["mood"] = clamp(pet["mood"] + mood_gain)
            if pet["hunger"] > 0:
                pet["starve_since"] = None
                pet["starve_warned"] = 0
            self._set_cooldown(pet, "feed")
            levels = self._gain_exp(pet, exp)
            extra = [f"饱食 +{hunger_gain}，心情 +{mood_gain}，它吃得津津有味～"]
            if levels:
                extra.append(self._levelup_text(pet, levels))
            extra.extend(self._post_action(pet, profile, "feed"))
            self.store.save_profile(profile)
            self.store.save_pet(pet)
            yield event.plain_result(
                headline + "\n" + "\n".join(extra) + f"\n\n{self._panel(pet, profile)}"
            )

    @filter.command("宠物玩耍", alias={"逗宠物"})
    async def pet_play(self, event: AstrMessageEvent, item: str = ""):
        """陪宠物玩耍：不带参数普通玩耍，带玩具名则消耗背包道具。"""
        owner = event.get_sender_id()
        item = (item or "").strip()
        cd_min = self._cfg_int("play_cooldown_min", 5)
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            profile = self.store.get_profile(owner)
            cd = self._cooldown_left(pet, "play", cd_min * 60)
            if cd > 0:
                yield event.plain_result(f"{pet['name']}玩累了，{fmt_cooldown(cd)}后再来陪它吧～")
                return
            sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)

            if item:
                info = SHOP.get(item)
                if not info:
                    yield event.plain_result(f"🏪 没有叫「{item}」的东西，发送 宠物商店 看看货架～")
                    return
                if info["kind"] != "toy":
                    yield event.plain_result(f"「{item}」是食物，想喂它请发送：宠物喂食 {item}")
                    return
                held = int(pet["items"].get(item, 0))
                if held <= 0:
                    yield event.plain_result(
                        f"背包里没有{item}了，发送 宠物购买 {item} 买一个吧～"
                    )
                    return
                if pet["mood"] >= 100:
                    yield event.plain_result(f"{pet['name']}现在心情好得很，先别拿{item}逗它啦～")
                    return
                if held == 1:
                    pet["items"].pop(item, None)
                else:
                    pet["items"][item] = held - 1
                mood_gain = round(int(info["mood"]) * (1 + sp["play"]))
                exp = int(info["exp"])
                headline = f"🧶 你用{item}逗{pet['name']}玩，它开心得满地打滚！"
            else:
                mood_gain = round(30 * (1 + sp["play"]))
                exp = 8
                headline = f"🎾 你陪{pet['name']}玩了很久，它开心得转圈圈！"

            pet["mood"] = clamp(pet["mood"] + mood_gain)
            self._set_cooldown(pet, "play")
            levels = self._gain_exp(pet, exp)
            extra = [f"心情 +{mood_gain}～"]
            if levels:
                extra.append(self._levelup_text(pet, levels))
            extra.extend(self._post_action(pet, profile, "play"))
            self.store.save_profile(profile)
            self.store.save_pet(pet)
            yield event.plain_result(
                headline + "\n" + "\n".join(extra) + f"\n\n{self._panel(pet, profile)}"
            )

    @filter.command("宠物打工")
    async def pet_work(self, event: AstrMessageEvent):
        """让宠物打工赚金币。"""
        owner = event.get_sender_id()
        cd_min = self._cfg_int("work_cooldown_min", 10)
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            profile = self.store.get_profile(owner)
            cd = self._cooldown_left(pet, "work", cd_min * 60)
            if cd > 0:
                yield event.plain_result(f"{pet['name']}还没下班，{fmt_cooldown(cd)}后再来吧～")
                return
            if pet["hunger"] < 15 or pet["mood"] < 15:
                yield event.plain_result(
                    f"{pet['name']}又饿又没心情，实在没力气打工……\n先 宠物喂食 / 宠物玩耍 照顾一下吧！"
                )
                return
            sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)
            base = random.randint(
                max(1, self._cfg_int("work_reward_min", 10)),
                max(self._cfg_int("work_reward_min", 10) + 1, self._cfg_int("work_reward_max", 25)),
            )
            earned = int(base * (1 + sp["work"]))
            job = random.choice(WORK_FLAVOR)
            pet["coins"] += earned
            pet["hunger"] = clamp(pet["hunger"] - 5)
            pet["mood"] = clamp(pet["mood"] - 10)
            self._set_cooldown(pet, "work")
            levels = self._gain_exp(pet, 10)
            extra = [self._levelup_text(pet, levels)] if levels else []
            extra.extend(self._post_action(pet, profile, "work"))
            self.store.save_profile(profile)
            self.store.save_pet(pet)
            parts = [
                f"💼 {pet['name']}去{job}，赚到了 💰{earned}！"
                f"{'（含品种加成）' if sp['work'] else ''}"
            ]
            parts.extend(extra)
            parts.extend(["", self._panel(pet, profile)])
            yield event.plain_result("\n".join(parts))

    @filter.command("宠物冒险")
    async def pet_adventure(self, event: AstrMessageEvent):
        """随机冒险事件，有收益也有风险。"""
        owner = event.get_sender_id()
        cd_min = self._cfg_int("adventure_cooldown_min", 15)
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            profile = self.store.get_profile(owner)
            cd = self._cooldown_left(pet, "adventure", cd_min * 60)
            if cd > 0:
                yield event.plain_result(f"{pet['name']}冒险归来正在休息，{fmt_cooldown(cd)}后可再次出发～")
                return
            if pet["hunger"] < 20:
                yield event.plain_result(f"{pet['name']}饿得走不动路了，先 宠物喂食 补充体力！")
                return
            sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)
            roll = random.random()
            if roll < 0.05:
                kind, coins, exp, mood = "treasure", random.randint(80, 120), 40, 10
            elif roll < 0.30:
                kind, coins, exp, mood = "big", random.randint(20, 50), 20, 5
            elif roll < 0.65:
                kind, coins, exp, mood = "little", random.randint(5, 15), 15, 0
            elif roll < 0.85:
                kind, coins, exp, mood = "danger", 0, 10, -15
            else:
                kind, coins, exp, mood = "nothing", 0, 5, -5
            coins = int(coins * (1 + sp["coin"]))
            exp = int(exp * (1 + sp["exp"]))
            pet["coins"] += coins
            pet["hunger"] = clamp(pet["hunger"] - 10)
            pet["mood"] = clamp(pet["mood"] + mood)
            pet["adventures"] += 1
            self._set_cooldown(pet, "adventure")
            levels = self._gain_exp(pet, exp)
            text = random.choice(ADVENTURE_TEXTS[kind])
            extra = [self._levelup_text(pet, levels)] if levels else []
            extra.extend(self._post_action(pet, profile, "adventure"))
            self.store.save_profile(profile)
            self.store.save_pet(pet)
            parts = [f"🗺️ {pet['name']}出门冒险：{text}"]
            if coins:
                parts.append(f"获得 💰{coins}")
            if exp:
                parts.append(f"经验 +{exp}")
            if mood < 0:
                parts.append(f"心情 {mood}")
            parts.extend(extra)
            yield event.plain_result("\n".join(parts) + f"\n\n{self._panel(pet, profile)}")

    @filter.command("宠物商店")
    async def pet_shop(self, event: AstrMessageEvent):
        """查看商店里的食物与玩具。"""
        owner = event.get_sender_id()
        async with self._lock:
            pet = self.store.get_pet(owner)
            if pet:
                self._touch(event, pet)
            lines = ["🏪 宠物商店"]
            lines.append(f"钱包 💰{pet['coins']}" if pet else "（先领养宠物才有钱包哦）")
            lines.append("━━━━━━━━━━━━━━━")
            for name, info in SHOP.items():
                icon = "🍚" if info["kind"] == "food" else "🧶"
                held = int(pet["items"].get(name, 0)) if pet else 0
                lines.append(f"{icon} {name} 💰{info['price']} · {item_effect(info)} · 持有 {held}")
            lines.append("")
            lines.append("发送 宠物购买 <商品名> 买一个")
            lines.append("喂食时带上食物名、玩耍时带上玩具名，就会用背包里的道具")
            yield event.plain_result("\n".join(lines))

    @filter.command("宠物购买")
    async def pet_buy(self, event: AstrMessageEvent, item: str = ""):
        """购买一个食物/玩具：宠物购买 <商品名>"""
        owner = event.get_sender_id()
        item = (item or "").strip()
        if not item:
            yield event.plain_result("请告诉我要买什么：宠物购买 <商品名>（发送 宠物商店 查看货架）")
            return
        info = SHOP.get(item)
        if not info:
            yield event.plain_result(f"🏪 商店里没有「{item}」，发送 宠物商店 看看有什么～")
            return
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            held = int(pet["items"].get(item, 0))
            if held >= ITEM_LIMIT:
                yield event.plain_result(f"🎒 背包里的{item}已经堆到 {ITEM_LIMIT} 个上限啦～")
                return
            price = int(info["price"])
            if pet["coins"] < price:
                yield event.plain_result(
                    f"{item}要 💰{price}，钱包只剩 💰{pet['coins']}，先去 宠物打工 赚点钱吧！"
                )
                return
            pet["coins"] -= price
            pet["items"][item] = held + 1
            self.store.save_pet(pet)
            yield event.plain_result(
                f"🛒 买到了{item}！花了 💰{price}，背包里现在有 {held + 1} 个。"
            )

    @filter.command("宠物签到")
    async def pet_sign(self, event: AstrMessageEvent):
        """每日签到领金币与经验。"""
        owner = event.get_sender_id()
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            self._touch(event, pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            profile = self.store.get_profile(owner)
            today = today_str()
            if profile["sign_date"] == today:
                yield event.plain_result(
                    f"📅 今天已经签到过啦，连续签到 {profile['sign_streak']} 天，明天再来～"
                )
                return
            streak = (
                profile["sign_streak"] + 1
                if profile["sign_date"] == yesterday_str()
                else 1
            )
            base = self._cfg_int("sign_coins", 20)
            coins = base + min(5 * (streak - 1), 30)
            pet["coins"] += coins
            levels = self._gain_exp(pet, 10)
            profile["sign_date"] = today
            profile["sign_streak"] = streak
            self.store.save_profile(profile)
            self.store.save_pet(pet)
            extra = [self._levelup_text(pet, levels)] if levels else []
            parts = [f"📅 签到成功！连续签到 {streak} 天，获得 💰{coins} + 经验 10"]
            parts.extend(extra)
            parts.extend(["", self._panel(pet, profile)])
            yield event.plain_result("\n".join(parts))

    @filter.command("宠物任务", alias={"每日任务"})
    async def pet_tasks(self, event: AstrMessageEvent):
        """查看今日任务进度。"""
        owner = event.get_sender_id()
        async with self._lock:
            profile = self.store.get_profile(owner)
            self._daily_reset(profile)
            self.store.save_profile(profile)
            daily = profile["daily"]
            count = sum(1 for k in DAILY_TASKS if int(daily.get(k, 0)) > 0)
            lines = [f"📋 今日任务（{count}/{len(DAILY_TASKS)}）", "━━━━━━━━━━━━━━━"]
            for key, label in DAILY_TASKS.items():
                mark = "✅" if int(daily.get(key, 0)) > 0 else "⬜"
                lines.append(f"{mark} {label} 1 次")
            lines.append("")
            if daily.get("done"):
                lines.append("✅ 今日任务已全部完成，奖励已发放，明天再来～")
            else:
                lines.append(
                    f"完成全部任务可领取 💰{self._cfg_int('task_coins', 30)} + 经验 20"
                )
            yield event.plain_result("\n".join(lines))

    @filter.command("宠物图鉴", alias={"图鉴"})
    async def pet_dex(self, event: AstrMessageEvent):
        """查看品种收集进度。"""
        owner = event.get_sender_id()
        async with self._lock:
            profile = self.store.get_profile(owner)
            seen = set(profile["seen_species"])
            lines = [f"📖 宠物图鉴（{len(seen)}/{len(SPECIES)}）", "━━━━━━━━━━━━━━━"]
            for name, sp in SPECIES.items():
                mark = "✅" if name in seen else "⬜"
                lines.append(f"{mark} {name} {sp['emoji']} — {sp['trait']}")
            if len(seen) < len(SPECIES):
                lines.append("")
                lines.append("放生后重新领养也不会丢失收集进度～")
            yield event.plain_result("\n".join(lines))

    @filter.command("宠物成就", alias={"成就"})
    async def pet_achievements(self, event: AstrMessageEvent):
        """查看成就进度。"""
        owner = event.get_sender_id()
        async with self._lock:
            pet = self.store.get_pet(owner)
            profile = self.store.get_profile(owner)
            states = self._ach_states(pet, profile)
            unlocked = sum(1 for ok, _ in states.values() if ok)
            lines = [f"🏅 宠物成就（{unlocked}/{len(ACHIEVEMENTS)}）", "━━━━━━━━━━━━━━━"]
            for key, meta in ACHIEVEMENTS.items():
                ok, progress = states[key]
                if ok:
                    lines.append(f"✅ {meta['name']} — {meta['desc']}")
                else:
                    lines.append(f"⬜ {meta['name']} — {meta['desc']}（{progress}）")
            yield event.plain_result("\n".join(lines))

    @filter.command("宠物排行", alias={"宠物榜"})
    async def pet_rank(self, event: AstrMessageEvent):
        """查看宠物等级排行，群聊内为群排行。"""
        gid = self._home_group(event)
        pets = self.store.top_pets(gid or None, 10)
        if not pets:
            yield event.plain_result("还没有宠物上榜，发送 宠物领养 <名字> 抢占榜一！")
            return
        medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
        scope = "本群" if gid else "全局"
        lines = [f"🏆 {scope}宠物排行 TOP{len(pets)}", "━━━━━━━━━━━━━━━"]
        for i, p in enumerate(pets):
            sp = SPECIES.get(p["species"], DEFAULT_SPECIES)
            owner = str(p["owner_id"])
            try:
                owner = event.get_sender_name() if owner == event.get_sender_id() else owner
            except Exception:
                pass
            lines.append(f"{medals[i]} Lv.{p['level']} {p['name']}（{p['species']} {sp['emoji']}）· 主人 {owner}")
        yield event.plain_result("\n".join(lines))

    @filter.command("宠物放生", alias={"放生宠物"})
    async def pet_release(self, event: AstrMessageEvent, confirm: str = ""):
        """放生宠物：宠物放生 确认"""
        owner = event.get_sender_id()
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物哦～")
                return
            if (confirm or "").strip() != "确认":
                yield event.plain_result(
                    f"⚠️ 确定要和{pet['name']}（Lv.{pet['level']}）说再见吗？\n"
                    f"此操作不可恢复！发送：宠物放生 确认"
                )
                return
            self.store.delete_pet(owner)
            yield event.plain_result(
                f"👋 你挥挥手，{pet['name']}一步三回头地走进了草丛……\n祝它前程似锦。（已放生，可重新领养）"
            )

    def _mini_board(self, group_id: str = "") -> str:
        pets = self.store.top_pets(group_id or None, 3)
        if not pets:
            return ""
        medals = ["🥇", "🥈", "🥉"]
        return "\n".join(
            f"{medals[i]} Lv.{p['level']} {p['name']}（{p['species']}）"
            for i, p in enumerate(pets)
        )
