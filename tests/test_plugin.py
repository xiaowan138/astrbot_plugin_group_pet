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

    warning = info = __import__("builtins").print

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

    def get_sender_id(self) -> str:
        return self._sender

    def get_group_id(self) -> str:
        return self._group

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

    # ---- 领养 ----
    ev = FakeEvent("1001", group="g1")
    msgs = await collect(plugin.pet_adopt(ev, "汤圆"))
    pet = store.get_pet("1001")
    assert pet and pet["name"] == "汤圆", msgs
    assert pet["species"] in main.SPECIES
    assert pet["coins"] == 30 and pet["level"] == 1
    assert any("领养到了" in m for m in msgs)
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
    msgs = await collect(plugin.pet_feed(FakeEvent("1001")))
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
    msgs = await collect(plugin.pet_play(FakeEvent("1001")))
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

    await plugin.terminate()
    print("\n全部测试通过 ✅")


if __name__ == "__main__":
    asyncio.run(run_all())
