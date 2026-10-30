"""离线测试：stub 掉 astrbot 运行时，验证插件完整玩法逻辑。

运行：python tests/test_plugin.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def stub_astrbot(data_root: Path) -> None:
    """在 sys.modules 中构造最小 astrbot API。"""
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")

    class _Logger:
        def info(self, *a, **k):
            pass

        def error(self, *a, **k):
            print("[logger.error]", *a)

    class _Filter:
        PermissionType = types.SimpleNamespace(ADMIN="admin")

        def command(self, *a, **k):
            return lambda f: f

        def command_group(self, *a, **k):
            return lambda f: f

    class _Star:
        def __init__(self, context):
            self.context = context

    api.AstrBotConfig = dict
    api.logger = _Logger()
    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = object
    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.register = lambda *a, **k: (lambda cls: cls)
    path_mod.get_astrbot_plugin_data_path = lambda: str(data_root)

    for name, mod in {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.astrbot_path": path_mod,
    }.items():
        sys.modules[name] = mod


def load_plugin(data_root: Path):
    pkg_name = "astrbot_plugin_group_pet"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[pkg_name] = pkg
    spec = importlib.util.spec_from_file_location(f"{pkg_name}.main", PLUGIN_DIR / "main.py")
    main = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.main"] = main
    spec.loader.exec_module(main)
    plugin = main.GroupPetPlugin(context=None, config={})
    return main, plugin


class FakeEvent:
    def __init__(self, sender: str, group: str = ""):
        self._sender = sender
        self._group = group
        self.sent: list[str] = []
        self.unified_msg_origin = f"test:{group or 'private'}:{sender}"

    def get_sender_id(self) -> str:
        return self._sender

    def get_group_id(self) -> str:
        return self._group

    def get_sender_name(self) -> str:
        return f"用户{self._sender}"

    def plain_result(self, text: str):
        self.sent.append(text)
        return ("plain", text)


async def collect(handler_gen) -> list[str]:
    out = []
    async for res in handler_gen:
        out.append(res[1])
    return out


async def run_all() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="group_pet_test_"))
    stub_astrbot(tmp)
    main, plugin = load_plugin(tmp)
    store = plugin.store

    # ================= 基础玩法 =================

    # ---- 领养 ----
    ev = FakeEvent("1001", group="g1")
    msgs = await collect(plugin.pet_adopt(ev, "汤圆"))
    pet = store.get_pet("1001")
    assert pet and pet["name"] == "汤圆", msgs
    assert pet["species"] in main.SPECIES
    assert pet["coins"] == 30 and pet["level"] == 1
    assert any("领养到了" in m for m in msgs)
    assert pet["group_id"] == "g1" and pet["umo"].endswith(":1001")
    print(f"[OK] 领养: 品种={pet['species']} 面板首行={msgs[-1].splitlines()[0]}")

    # 重复领养
    msgs = await collect(plugin.pet_adopt(FakeEvent("1001"), "二哈"))
    assert any("已经有宠物" in m for m in msgs)
    print("[OK] 重复领养被拦截")

    # 名字校验
    msgs = await collect(plugin.pet_adopt(FakeEvent("2002"), "一个超级无敌长的名字"))
    assert any("名字太长" in m for m in msgs)
    assert store.get_pet("2002") is None
    print("[OK] 名字长度校验")

    # ---- 喂食 ----
    ev = FakeEvent("1001", group="g1")
    hunger_before = pet["hunger"]
    msgs = await collect(plugin.pet_feed(ev))
    pet = store.get_pet("1001")
    assert pet["coins"] == 25 and pet["hunger"] == min(100, hunger_before + 35)
    assert any("大餐" in m for m in msgs)
    msgs = await collect(plugin.pet_feed(FakeEvent("1001")))
    assert any("刚吃过" in m for m in msgs), msgs
    print("[OK] 喂食扣费/回复饱食度/冷却生效")

    # 金币不足喂食
    pet["coins"] = 0
    store.save_pet(pet)
    await collect(plugin.pet_feed(FakeEvent("1001")))
    pet["cooldowns"]["feed"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_feed(FakeEvent("1001")))
    assert any("打工 赚点钱" in m for m in msgs), msgs
    pet["coins"] = 100
    store.save_pet(pet)
    print("[OK] 金币不足提示")

    # ---- 玩耍 ----
    mood_before = pet["mood"]
    msgs = await collect(plugin.pet_play(FakeEvent("1001")))
    pet = store.get_pet("1001")
    assert pet["mood"] == min(100, mood_before + 30), (mood_before, pet["mood"])
    msgs = await collect(plugin.pet_play(FakeEvent("1001")))
    assert any("玩累了" in m for m in msgs)
    print("[OK] 玩耍回复心情/冷却生效")

    # ---- 打工 ----
    pet["cooldowns"]["work"] = 0
    store.save_pet(pet)
    coins_before = pet["coins"]
    msgs = await collect(plugin.pet_work(FakeEvent("1001")))
    pet = store.get_pet("1001")
    assert pet["coins"] > coins_before, msgs
    msgs = await collect(plugin.pet_work(FakeEvent("1001")))
    assert any("没下班" in m for m in msgs)
    print("[OK] 打工赚钱/冷却生效")

    # 饿/丧时不肯打工
    pet["hunger"], pet["mood"] = 5, 5
    pet["cooldowns"]["work"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_work(FakeEvent("1001")))
    assert any("没力气打工" in m for m in msgs)
    pet["hunger"], pet["mood"] = 80, 80
    store.save_pet(pet)
    print("[OK] 饿/丧拒绝打工")

    # ---- 冒险：跑 60 次验证不崩且结果合法 ----
    for i in range(60):
        pet = store.get_pet("1001")
        pet["hunger"] = 80
        pet["mood"] = 80
        pet["cooldowns"]["adventure"] = 0
        store.save_pet(pet)
        msgs = await collect(plugin.pet_adventure(FakeEvent("1001")))
        pet = store.get_pet("1001")
        assert pet["adventures"] >= i + 1
        assert msgs, "冒险必须有输出"
    assert store.get_pet("1001")["coins"] >= 0
    print("[OK] 冒险 60 次无异常，输出正常")

    # 饿时不能冒险
    pet = store.get_pet("1001")
    pet["hunger"] = 10
    pet["cooldowns"]["adventure"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_adventure(FakeEvent("1001")))
    assert any("走不动路" in m for m in msgs)
    print("[OK] 饥饿禁止冒险")

    # ---- 升级 ----
    pet = store.get_pet("1001")
    pet["exp"] = main.need_exp(pet["level"]) - 1
    store.save_pet(pet)
    await collect(plugin.pet_play(FakeEvent("1001")))
    pet["cooldowns"]["play"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_play(FakeEvent("1001")))
    pet = store.get_pet("1001")
    assert any("升到了" in m for m in msgs), msgs
    assert pet["exp"] < main.need_exp(pet["level"])
    print(f"[OK] 升级: Lv.{pet['level']} 经验回卷")

    # ---- 排行 ----
    store.create_pet("2002", "小白", "柴犬", "g1")
    store.create_pet("3003", "小黑", "仓鼠", "g2")
    pet2 = store.get_pet("2002")
    pet2["level"] = store.get_pet("1001")["level"] + 1  # 确保严格高于汤圆
    store.save_pet(pet2)
    msgs = await collect(plugin.pet_rank(FakeEvent("1001", group="g1")))
    assert "小白" in msgs[0] and "小黑" not in msgs[0], msgs
    assert msgs[0].index("小白") < msgs[0].index("汤圆")
    msgs = await collect(plugin.pet_rank(FakeEvent("1001", group="")))
    assert "小黑" in msgs[0], msgs
    print("[OK] 排行群过滤/排序/全局榜")

    # ---- 放生 ----
    msgs = await collect(plugin.pet_release(FakeEvent("2002")))
    assert any("确定" in m for m in msgs)
    assert store.get_pet("2002")
    msgs = await collect(plugin.pet_release(FakeEvent("2002"), "确认"))
    assert store.get_pet("2002") is None
    assert any("放生" in m for m in msgs)
    print("[OK] 放生二次确认")

    # ---- 离家出走 ----
    pet = store.get_pet("1001")
    pet["starve_since"] = time.time() - 100 * 3600
    store.save_pet(pet)
    msgs = await collect(plugin.pet_feed(FakeEvent("1001")))
    assert any("离家出走" in m for m in msgs), msgs
    assert store.get_pet("1001") is None
    print("[OK] 饿太久离家出走并删除数据")

    # runaway_hours=0 关闭离家出走
    plugin.config["runaway_hours"] = 0
    store.create_pet("4004", "阿呆", "熊猫", "g1")
    pet = store.get_pet("4004")
    pet["starve_since"] = time.time() - 999 * 3600
    store.save_pet(pet)
    msgs = await collect(plugin.pet_feed(FakeEvent("4004")))
    assert store.get_pet("4004"), msgs
    plugin.config["runaway_hours"] = 72
    print("[OK] runaway_hours=0 关闭离家出走")

    # ---- 后台衰减 ----
    store.create_pet("5005", "阿衰", "熊猫", "g1")
    await plugin._decay_once()
    pet = store.get_pet("5005")
    assert pet["hunger"] == 78, pet["hunger"]  # 80 - 2*1.0
    store.create_pet("6006", "米粒", "仓鼠", "g1")
    await plugin._decay_once()
    pet = store.get_pet("6006")
    assert pet["hunger"] == 79, pet["hunger"]  # 仓鼠衰减减半: 80-1
    print("[OK] 后台衰减（含熊猫/仓鼠倍率）")

    # 喂食清除饥饿计时
    pet = store.get_pet("6006")
    pet["hunger"] = 0
    pet["starve_since"] = time.time()
    pet["coins"] = 100
    pet["cooldowns"]["feed"] = 0
    store.save_pet(pet)
    await collect(plugin.pet_feed(FakeEvent("6006")))
    pet = store.get_pet("6006")
    assert pet["starve_since"] is None and pet["hunger"] == 35
    print("[OK] 喂食清除饥饿计时")

    # ---- 状态面板 ----
    pet = store.get_pet("6006")
    panel = main.status_panel(pet)
    assert "Lv." in panel and "饱食" in panel and "心情" in panel
    print("[OK] 状态面板渲染")

    # ================= Bug 修复回归 =================

    # ---- 熊猫心情衰减必须真正减半（浮点） ----
    store.create_pet("8008", "滚滚", "熊猫", "g1")
    pet = store.get_pet("8008")
    assert pet["hunger"] == 80 and pet["mood"] == 80
    await plugin._decay_once()
    pet = store.get_pet("8008")
    assert pet["hunger"] == 78, pet["hunger"]
    assert abs(pet["mood"] - 79.5) < 1e-6, pet["mood"]  # 1 * 0.5
    await plugin._decay_once()
    pet = store.get_pet("8008")
    assert abs(pet["mood"] - 79.0) < 1e-6, pet["mood"]
    print("[OK] 熊猫心情衰减减半（不再被取整吃掉）")

    # ---- 衰减中结算离家出走 + 饿前提醒 ----
    store.create_pet("9009", "快饿坏", "柴犬", "g2")
    pet = store.get_pet("9009")
    pet["hunger"] = 0
    pet["starve_since"] = time.time() - 67 * 3600  # 72-67=5 小时 < 6 小时提醒阈值
    store.save_pet(pet)
    store.create_pet("9010", "已经跑了", "仓鼠", "g2")
    pet2 = store.get_pet("9010")
    pet2["hunger"] = 0
    pet2["starve_since"] = time.time() - 100 * 3600
    store.save_pet(pet2)
    await plugin._decay_once()
    pet = store.get_pet("9009")
    assert pet and pet["starve_warned"] == 1, pet
    assert store.get_pet("9010") is None, "饿过头的宠物应在衰减中清理"
    print("[OK] 饿前提醒标记 / 衰减中清理离家出走")

    # ---- 旧宠物饿走后不再卡住领养 ----
    store.create_pet("9101", "旧宠", "兔子", "g2")
    pet = store.get_pet("9101")
    pet["starve_since"] = time.time() - 100 * 3600
    store.save_pet(pet)
    msgs = await collect(plugin.pet_adopt(FakeEvent("9101", group="g2"), "新宠"))
    assert any("离家出走" in m for m in msgs), msgs
    assert any("领养到了" in m for m in msgs), msgs
    pet = store.get_pet("9101")
    assert pet and pet["name"] == "新宠"
    print("[OK] 饿死后可直接重新领养")

    # ---- 多级升级奖励与文案一致 ----
    await collect(plugin.pet_adopt(FakeEvent("1101"), "跳级王"))
    pet = store.get_pet("1101")
    pet["exp"] = 245  # 喂食 +5 后连升 2 级（50、100）
    store.save_pet(pet)
    msgs = await collect(plugin.pet_feed(FakeEvent("1101")))
    pet = store.get_pet("1101")
    assert pet["level"] == 3, pet["level"]
    assert any("连升 2 级" in m and "💰40" in m for m in msgs), msgs
    print("[OK] 多级升级文案与累计奖励一致")

    # ---- 群归属不漂移 ----
    store.create_pet("1201", "守家", "柴犬", "g1")
    await collect(plugin.pet_home(FakeEvent("1201", group="g2")))
    pet = store.get_pet("1201")
    assert pet["group_id"] == "g1", pet["group_id"]
    assert ":g2:" in pet["umo"], pet["umo"]
    print("[OK] 群归属固定，仅更新主动推送地址")

    # ---- 领养中心只推荐本群 ----
    store.create_pet("1202", "本群宠", "兔子", "gz")
    store.create_pet("1203", "别群宠", "兔子", "gz2")
    msgs = await collect(plugin.pet_home(FakeEvent("1299", group="gz")))
    assert "本群宠" in msgs[0] and "别群宠" not in msgs[0], msgs
    print("[OK] 领养中心按本群推荐")

    # ================= 新功能 =================

    # ---- 改名 ----
    await collect(plugin.pet_adopt(FakeEvent("1301"), "原名"))
    msgs = await collect(plugin.pet_rename(FakeEvent("1301"), "新名"))
    pet = store.get_pet("1301")
    assert pet["name"] == "新名"
    assert any("改名叫" in m for m in msgs), msgs
    assert any("新名字新气象" in m for m in msgs), msgs
    msgs = await collect(plugin.pet_rename(FakeEvent("1301"), "又改名"))
    assert any("刚改过名字" in m for m in msgs), msgs
    pet["cooldowns"]["rename"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_rename(FakeEvent("1301"), "这个名字真的超级长"))
    assert any("名字太长" in m for m in msgs), msgs
    print("[OK] 改名/冷却/长度校验/成就")

    # ---- 商店与购买 ----
    await collect(plugin.pet_adopt(FakeEvent("1401"), "买家"))
    msgs = await collect(plugin.pet_shop(FakeEvent("1401")))
    assert any("小鱼干" in m and "高级大餐" in m for m in msgs), msgs
    msgs = await collect(plugin.pet_buy(FakeEvent("1401"), "小鱼干"))
    pet = store.get_pet("1401")
    assert pet["coins"] == 0 and pet["items"].get("小鱼干") == 1
    msgs = await collect(plugin.pet_buy(FakeEvent("1401"), "高级大餐"))
    assert any("赚点钱" in m for m in msgs), msgs
    msgs = await collect(plugin.pet_buy(FakeEvent("1401"), "不存在的商品"))
    assert any("商店里没有" in m for m in msgs), msgs
    print("[OK] 商店/购买/余额与商品校验")

    # ---- 使用背包食物 ----
    msgs = await collect(plugin.pet_feed(FakeEvent("1401"), "小鱼干"))
    pet = store.get_pet("1401")
    assert "小鱼干" not in pet["items"]
    assert pet["coins"] == 0 and pet["hunger"] == 100
    assert any("吃掉了小鱼干" in m for m in msgs), msgs
    pet["cooldowns"]["feed"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_feed(FakeEvent("1401"), "小鱼干"))
    assert any("背包里没有" in m for m in msgs), msgs
    msgs = await collect(plugin.pet_feed(FakeEvent("1401"), "毛线球"))
    assert any("是玩具" in m for m in msgs), msgs
    print("[OK] 食物道具使用/库存校验/类型校验")

    # ---- 使用背包玩具 ----
    store.create_pet("1411", "玩咖", "兔子", "g1")
    pet = store.get_pet("1411")
    pet["coins"] = 200
    store.save_pet(pet)
    await collect(plugin.pet_buy(FakeEvent("1411"), "毛线球"))
    pet = store.get_pet("1411")
    assert pet["items"].get("毛线球") == 1 and pet["coins"] == 160
    msgs = await collect(plugin.pet_play(FakeEvent("1411"), "毛线球"))
    pet = store.get_pet("1411")
    assert "毛线球" not in pet["items"] and pet["mood"] == 100
    assert any("毛线球" in m and "心情" in m for m in msgs), msgs
    pet["cooldowns"]["play"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_play(FakeEvent("1411"), "小鱼干"))
    assert any("是食物" in m for m in msgs), msgs
    print("[OK] 玩具道具使用/类型校验")

    # ---- 每日签到 ----
    await collect(plugin.pet_adopt(FakeEvent("1501"), "签到员"))
    msgs = await collect(plugin.pet_sign(FakeEvent("1501")))
    pet = store.get_pet("1501")
    profile = store.get_profile("1501")
    assert pet["coins"] == 50 and profile["sign_streak"] == 1
    assert any("签到成功" in m for m in msgs), msgs
    msgs = await collect(plugin.pet_sign(FakeEvent("1501")))
    assert any("已经签到" in m for m in msgs), msgs
    profile["sign_date"] = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    profile["sign_streak"] = 1
    store.save_profile(profile)
    pet["coins"] = 0
    store.save_pet(pet)
    msgs = await collect(plugin.pet_sign(FakeEvent("1501")))
    pet = store.get_pet("1501")
    profile = store.get_profile("1501")
    assert pet["coins"] == 25 and profile["sign_streak"] == 2, (pet["coins"], profile)
    print("[OK] 签到/重复拦截/连续签到加成")

    # ---- 每日任务 ----
    await collect(plugin.pet_adopt(FakeEvent("1601"), "任务达人"))
    await collect(plugin.pet_feed(FakeEvent("1601")))
    await collect(plugin.pet_play(FakeEvent("1601")))
    await collect(plugin.pet_work(FakeEvent("1601")))
    pet = store.get_pet("1601")
    pet["hunger"], pet["mood"] = 80, 80
    store.save_pet(pet)
    msgs = await collect(plugin.pet_adventure(FakeEvent("1601")))
    profile = store.get_profile("1601")
    assert profile["daily"]["done"] is True, profile["daily"]
    assert any("今日任务全部完成" in m for m in msgs), msgs
    assert any("💰30" in m for m in msgs), msgs
    msgs = await collect(plugin.pet_tasks(FakeEvent("1601")))
    assert any("已全部完成" in m for m in msgs), msgs
    print("[OK] 每日任务进度与自动奖励")

    # ---- 图鉴 / 成就 ----
    await collect(plugin.pet_adopt(FakeEvent("1701"), "图鉴迷"))
    pet = store.get_pet("1701")
    profile = store.get_profile("1701")
    assert "first" in profile["achievements"], profile["achievements"]
    msgs = await collect(plugin.pet_dex(FakeEvent("1701")))
    assert "1/6" in msgs[0] and pet["species"] in msgs[0], msgs
    profile["seen_species"] = list(main.SPECIES.keys())
    profile["stats"]["feed"] = 50
    store.save_profile(profile)
    pet["coins"] = 500
    store.save_pet(pet)
    # adopt_time 是只读字段，这里直接改库模拟“陪伴满 7 天”
    store._db.execute(
        "UPDATE pets SET adopt_time=? WHERE owner_id=?",
        (time.time() - 8 * 86400, "1701"),
    )
    store._db.commit()
    msgs = await collect(plugin.pet_achievements(FakeEvent("1701")))
    text = msgs[0]
    assert "5/9" in text, text
    assert "✅ 图鉴收集者" in text and "✅ 大胃王" in text, text
    print("[OK] 图鉴进度/成就状态")

    # ---- 放生后成就与图鉴保留 ----
    await collect(plugin.pet_release(FakeEvent("1701"), "确认"))
    profile = store.get_profile("1701")
    assert "first" in profile["achievements"]
    assert len(profile["seen_species"]) == len(main.SPECIES)
    msgs = await collect(plugin.pet_achievements(FakeEvent("1701")))
    assert "✅ 图鉴收集者" in msgs[0], msgs
    print("[OK] 成就/图鉴跨放生保留")

    await plugin.terminate()
    print("\n全部测试通过 ✅")


if __name__ == "__main__":
    asyncio.run(run_all())
