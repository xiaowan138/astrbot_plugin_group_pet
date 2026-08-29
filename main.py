"""AstrBot 群宠物养成插件。

领养一只宠物，喂食、玩耍、打工、冒险，帮它升级。
宠物会随时间变饿变无聊，太久不管会离家出走。

指令（默认需带唤醒前缀，如 /宠物）：
  宠物 / 我的宠物          查看宠物状态（无宠物时显示领养引导）
  宠物领养 <名字>          领养一只随机品种的宠物
  宠物喂食                 花金币喂食，恢复饱食度
  宠物玩耍                 陪宠物玩，恢复心情
  宠物打工                 让宠物打工赚金币
  宠物冒险                 随机冒险事件，有收益也有风险
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

FEED_COOLDOWN_S = 60  # 仅作默认值兜底，实际读配置 feed_cooldown_s


def need_exp(level: int) -> int:
    return int(level) * 50


def bar(value: int, width: int = 10) -> str:
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


def status_panel(pet: dict) -> str:
    sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)
    lines = [
        f"🐾 {pet['name']} · {pet['species']} {sp['emoji']} · Lv.{pet['level']}",
        "━━━━━━━━━━━━━━━",
        f"经验 {pet['exp']}/{need_exp(pet['level'])}  {bar(pet['exp'] * 100 // max(1, need_exp(pet['level'])))}",
        f"饱食 {pet['hunger']}/100  {bar(pet['hunger'])}",
        f"心情 {pet['mood']}/100  {bar(pet['mood'])}",
        f"钱包 💰 {pet['coins']}",
        "━━━━━━━━━━━━━━━",
        f"特性：{sp['trait']}",
        f"状态：{state_text(pet)}",
    ]
    return "\n".join(lines)


@register(
    "astrbot_plugin_group_pet",
    "xiaowan",
    "群宠物养成：领养、喂食、打工、冒险、排行，太久不管会离家出走",
    "v1.0.0",
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
        interval = max(5, self._cfg_int("decay_interval_min", 30)) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self._decay_once()
            except Exception as e:
                logger.error(f"[群宠物] 状态衰减出错: {e!r}")

    async def _decay_once(self) -> None:
        now = time.time()
        step = self._cfg_int("decay_hunger", 2)
        async with self._lock:
            for pet in self.store.all_pets():
                sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)
                pet["hunger"] = clamp(round(pet["hunger"] - step * sp["hunger_decay"]))
                if pet["hunger"] <= 0 and not pet.get("starve_since"):
                    pet["starve_since"] = now
                mood_drop = (1 if pet["hunger"] >= 30 else 3) * sp["mood_decay"]
                pet["mood"] = clamp(round(pet["mood"] - mood_drop))
                self.store.save_pet(pet)

    # ================= 内部工具 =================

    def _home_group(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        return str(gid) if gid else ""

    def _cooldown_left(self, pet: dict, action: str, minutes: float) -> int:
        last = pet["cooldowns"].get(action, 0)
        return max(0, int(last + minutes * 60 - time.time()))

    def _set_cooldown(self, pet: dict, action: str) -> None:
        # 记录上次动作时间，配合 _cooldown_left 计算剩余冷却
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

    # ================= 指令 =================

    @filter.command("宠物", alias={"我的宠物"})
    async def pet_home(self, event: AstrMessageEvent):
        """查看宠物状态，没有宠物时显示领养引导。"""
        owner = event.get_sender_id()
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result(
                    "🏠 宠物领养中心\n"
                    "你还没有宠物哦～发送：宠物领养 <名字>\n"
                    "例如：宠物领养 汤圆\n\n"
                    "已有宠物的包括：\n"
                    + (self._mini_board() or "　（还没有人领养宠物，快来当第一人！）")
                )
                return
            pet["group_id"] = self._home_group(event) or pet.get("group_id", "")
            self.store.save_pet(pet)
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            yield event.plain_result(status_panel(pet))

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
            if self.store.get_pet(owner):
                existing = self.store.get_pet(owner)
                yield event.plain_result(
                    f"你已经有宠物啦：{existing['name']}，一心不能二用哦～\n"
                    f"发送 宠物 查看它的状态。"
                )
                return
            species = random.choice(list(SPECIES.keys()))
            pet = self.store.create_pet(owner, name, species, self._home_group(event))
            sp = SPECIES[species]
            yield event.plain_result(
                f"🎉 恭喜！你领养到了一只{species} {sp['emoji']}！\n"
                f"它叫「{name}」，特性：{sp['trait']}\n\n"
                f"新手礼包已发放 💰30\n"
                f"记得定时 宠物喂食 / 宠物玩耍，不然它会饿着哦～\n\n"
                f"{status_panel(pet)}"
            )

    @filter.command("宠物喂食", alias={"喂宠物"})
    async def pet_feed(self, event: AstrMessageEvent):
        """喂食宠物，恢复饱食度。"""
        owner = event.get_sender_id()
        cost = self._cfg_int("feed_cost", 5)
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            cd = self._cooldown_left(pet, "feed", self._cfg_int("feed_cooldown_s", 60) / 60)
            if cd > 0:
                yield event.plain_result(f"{pet['name']}刚吃过东西，{fmt_cooldown(cd)}后再来喂吧～")
                return
            if pet["coins"] < cost:
                yield event.plain_result(
                    f"喂食需要 💰{cost}，钱包只剩 💰{pet['coins']}，让宠物去 宠物打工 赚点钱吧！"
                )
                return
            pet["coins"] -= cost
            pet["hunger"] = clamp(pet["hunger"] + 35)
            pet["mood"] = clamp(pet["mood"] + 5)
            if pet["hunger"] > 0:
                pet["starve_since"] = None
            self._set_cooldown(pet, "feed")
            levels = self._gain_exp(pet, 5)
            self.store.save_pet(pet)
            extra = f"\n🎊 {pet['name']}升到了 Lv.{pet['level']}，奖励 💰{self._cfg_int('levelup_coin_bonus', 20)}！" if levels else ""
            yield event.plain_result(
                f"🍚 你花 💰{cost} 给{pet['name']}买了一顿大餐！\n"
                f"饱食度 +35，它吃得津津有味～{extra}\n\n{status_panel(pet)}"
            )

    @filter.command("宠物玩耍", alias={"逗宠物"})
    async def pet_play(self, event: AstrMessageEvent):
        """陪宠物玩耍，恢复心情。"""
        owner = event.get_sender_id()
        cd_min = max(0, self._cfg_int("play_cooldown_min", 5))
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            cd = self._cooldown_left(pet, "play", cd_min)
            if cd > 0:
                yield event.plain_result(f"{pet['name']}玩累了，{fmt_cooldown(cd)}后再来陪它吧～")
                return
            sp = SPECIES.get(pet["species"], DEFAULT_SPECIES)
            mood_gain = round(30 * (1 + sp["play"]))
            pet["mood"] = clamp(pet["mood"] + mood_gain)
            self._set_cooldown(pet, "play")
            levels = self._gain_exp(pet, 8)
            self.store.save_pet(pet)
            extra = f"\n🎊 {pet['name']}升到了 Lv.{pet['level']}，奖励 💰{self._cfg_int('levelup_coin_bonus', 20)}！" if levels else ""
            yield event.plain_result(
                f"🎾 你陪{pet['name']}玩了很久，它开心得转圈圈！\n"
                f"心情 +{mood_gain}～{extra}\n\n{status_panel(pet)}"
            )

    @filter.command("宠物打工")
    async def pet_work(self, event: AstrMessageEvent):
        """让宠物打工赚金币。"""
        owner = event.get_sender_id()
        cd_min = max(0, self._cfg_int("work_cooldown_min", 10))
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            cd = self._cooldown_left(pet, "work", cd_min)
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
            self.store.save_pet(pet)
            extra = f"\n🎊 {pet['name']}升到了 Lv.{pet['level']}，奖励 💰{self._cfg_int('levelup_coin_bonus', 20)}！" if levels else ""
            yield event.plain_result(
                f"💼 {pet['name']}去{job}，赚到了 💰{earned}！"
                f"{'（含品种加成）' if sp['work'] else ''}{extra}\n\n{status_panel(pet)}"
            )

    @filter.command("宠物冒险")
    async def pet_adventure(self, event: AstrMessageEvent):
        """随机冒险事件，有收益也有风险。"""
        owner = event.get_sender_id()
        cd_min = max(0, self._cfg_int("adventure_cooldown_min", 15))
        async with self._lock:
            pet = self.store.get_pet(owner)
            if not pet:
                yield event.plain_result("你还没有宠物，先发送 宠物领养 <名字> 领养一只吧～")
                return
            farewell = await self._precheck_runaway(pet)
            if farewell:
                yield event.plain_result(farewell)
                return
            cd = self._cooldown_left(pet, "adventure", cd_min)
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
            self.store.save_pet(pet)
            text = random.choice(ADVENTURE_TEXTS[kind])
            parts = [f"🗺️ {pet['name']}出门冒险：{text}"]
            if coins:
                parts.append(f"获得 💰{coins}")
            if exp:
                parts.append(f"经验 +{exp}")
            if mood < 0:
                parts.append(f"心情 {mood}")
            if levels:
                parts.append(f"🎊 升到 Lv.{pet['level']}，奖励 💰{self._cfg_int('levelup_coin_bonus', 20)}！")
            yield event.plain_result("\n".join(parts) + f"\n\n{status_panel(pet)}")

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

    def _mini_board(self) -> str:
        pets = self.store.top_pets(None, 3)
        if not pets:
            return ""
        medals = ["🥇", "🥈", "🥉"]
        return "\n".join(
            f"{medals[i]} Lv.{p['level']} {p['name']}（{p['species']}）"
            for i, p in enumerate(pets)
        )
