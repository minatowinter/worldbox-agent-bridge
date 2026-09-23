"""把世界快照转换成「每个国家一份、方便 agent 阅读」的中文文档。

设计原则（这几条决定了输出能否被大模型 agent 真正用起来）：

1. **一国一文件夹**：领袖 agent 只需要打开 ``nations/<国名>/``，
   不必在 24 MB 的大 JSON 里自己筛选。
2. **给名字，不只给 id**：所有引用都解析成人类可读的名称，
   agent 不用再查表就能说"大仁王国"。
3. **体量有上限**：列表会截断，超大集合（单位、地块）只做汇总，
   agent 的上下文才是最稀缺的资源。
4. **分「本国」与「他国」**：``self.json`` 是完整的本国国情报告；
   ``others.json`` 是其他所有国家的精简横向对比。
5. **情报要诚实**：存档里能确切知道的数字直接给；
   无法从存档得知的（如国王年龄）明确标注为"存档未记录"，绝不让 agent 编。

字段口径（已对本机真实存档逐一验证，WorldBox 0.51.2 / saveVersion 17）：

* 人口  = ``actors_data`` 中 ``civ_kingdom_id`` 等于该国的单位数
* 军队  = 这些单位里带 ``army`` 字段（值为军队 id）的数量
* 领土  = 该国各城市 ``zones`` 数组长度之和（单位：地块）
* 建筑  = ``buildings`` 里 ``cityID`` 属于该国城市的条目数
* 城市自身**不存** army / loyalty / 资源字段，资源与财政请看
  ``history``（来自 ``map_stats.s3db`` 的逐年统计，字段更全）
* 国王年龄**不写入存档**，故不提供；改用``generation``与在位起始年
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import SCHEMA_VERSION
from .build import WorldSnapshot, _as_int, _as_list, _as_str

MAX_CITIES = 60
MAX_ARMIES = 40
MAX_WARS = 30
MAX_RELATIONS = 60
MAX_EVENTS = 45
MAX_HISTORY = 40
MAX_CLANS = 12
MAX_RULERS = 10

# --------------------------------------------------------------------------
# 简体中文标签表（键仍然是存档里的英文 id，另给 label 字段，程序稳定 + 人类可读）
# --------------------------------------------------------------------------

PROFESSION_LABELS = {
    0: "无业/幼儿",
    1: "国王",
    2: "平民",
    3: "战士",
    4: "士兵",
    5: "士兵",
    6: "队长",
}

WAR_TYPE_LABELS = {
    "inspire": "煽动而起",
    "conquest": "征服",
    "rebellion": "叛乱",
    "border": "边境冲突",
    "holy": "圣战",
}

EVENT_LABELS = {
    "king_new": "新国王登基",
    "king_dead": "国王驾崩",
    "king_killed": "国王被杀",
    "kingdom_royal_clan_changed": "王室宗族更替",
    "city_destroyed": "城市被摧毁",
    "city_conquered": "城市被攻占",
    "city_rebelled": "城市叛乱",
    "city_founded": "城市建立",
    "war_started": "战争爆发",
    "war_ended": "战争结束",
    "peace_made": "缔结和约",
    "alliance_created": "结成同盟",
    "alliance_destroyed": "同盟解散",
    "kingdom_created": "王国建立",
    "kingdom_destroyed": "王国覆灭",
    "plot_started": "阴谋开始",
    "plot_succeeded": "阴谋得逞",
    "plot_failed": "阴谋败露",
    "army_created": "组建军队",
    "army_destroyed": "军队覆灭",
    "new_book": "著书",
    "religion_created": "宗教创立",
    "language_created": "语言形成",
    "culture_created": "文化形成",
}

# 特质的简体中文标签。未收录的会原样保留英文 id（游戏更新可能会新增特质）。
KINGDOM_TRAIT_LABELS = {
    # —— 国家/文化层面的特质 ——
    "econ_power": "经济强权",
    "tax_rate_local_high": "地方高税",
    "tax_rate_local_low": "地方低税",
    "tax_rate_tribute_high": "高额贡赋",
    "tax_rate_tribute_low": "低额贡赋",
    "fast_builders": "善建者",
    "high_fecundity": "高繁殖力",
    "population_minimal": "人口稀少",
    "long_lifespan": "长寿",
    "gestation_short": "短孕期",
    "gestation_long": "长孕期",
    "gestation_very_long": "极长孕期",
    "reproduction_sexual": "有性生殖",
    "reproduction_strategy_viviparity": "胎生",
    "diet_cannibalism": "同类相食",
    "diet_florivore": "食花",
    "diet_folivore": "食叶",
    "diet_frugivore": "食果",
    "diet_granivore": "食谷",
    "diet_omnivore": "杂食",
    "monophasic_sleep": "单相睡眠",
    "polyphasic_sleep": "多相睡眠",
    "nocturnal_dormancy": "夜伏",
    "advanced_hippocampus": "发达海马体",
    "prefrontal_cortex": "前额叶皮层",
    "wernicke_area": "韦尼克区",
    "amygdala": "杏仁核",
    "shiny_love": "闪耀之爱",
    "stomach": "强健肠胃",
    "pure": "纯净血脉",
    "bad_genes": "劣质基因",
    "death_grow_tree": "死后化树",
    "phenotype_skin_skin_light": "浅色皮肤",
    "phenotype_skin_skin_mixed": "混色皮肤",
    "phenotype_skin_gray_black": "灰黑皮肤",
    "phenotype_skin_wood": "木色皮肤",
    # —— 角色层面的特质（国王/领袖也会带） ——
    "wise": "睿智", "genius": "天才", "stupid": "愚钝", "honest": "诚实",
    "deceitful": "狡诈", "paranoid": "多疑", "ambitious": "野心勃勃",
    "content": "知足", "greedy": "贪婪", "gluttonous": "贪食",
    "bloodlust": "嗜血", "peaceful": "爱好和平", "pacifist": "和平主义者",
    "hotheaded": "暴躁", "psychopath": "冷血", "lustful": "好色",
    "strong": "强壮", "weak": "虚弱", "tough": "坚韧", "fat": "肥胖",
    "tiny": "矮小", "giant": "巨人", "fast": "迅捷", "slow": "迟缓",
    "agile": "灵敏", "nimble": "灵巧", "clumsy": "笨拙", "weightless": "身轻如燕",
    "fragile_health": "体质脆弱", "boosted_vitality": "强健体魄",
    "regeneration": "再生", "immune": "免疫", "poison_immune": "抗毒",
    "freeze_proof": "抗寒", "hard_skin": "硬皮", "soft_skin": "软皮",
    "attractive": "俊美", "ugly": "丑陋", "mute": "哑巴", "crippled": "残疾",
    "eyepatch": "独眼", "eagle_eyed": "鹰眼", "short_sighted": "近视",
    "miner": "矿工", "well_paid": "高薪", "unpaid": "欠薪", "thief": "盗贼",
    "veteran": "老兵", "kingslayer": "弑君者", "mageslayer": "屠法者",
    "immortal": "不朽", "long_liver": "长寿者", "fertile": "多产",
    "infertile": "不育", "lucky": "幸运", "unlucky": "霉运",
    "miracle_born": "奇迹之子", "sunblessed": "日佑", "moonchild": "月之子",
    "nightchild": "夜之子", "light_lamp": "明灯", "golden_tooth": "金牙",
    "flesh_eater": "食肉者", "contagious": "带菌者", "pyromaniac": "纵火狂",
    "savage": "野蛮", "boat": "船",
    "block": "格挡", "dodge": "闪避", "dash": "冲刺", "backstep": "后撤",
    "deflect_projectile": "弹开投射物",
}

STANCE_LABELS = {"war": "交战", "alliance": "同盟", "contact": "已接触", "neutral": "中立",
                 "enemy": "敌对", "ally": "盟友", "unknown": "未知"}

DEATH_CAUSE_LABELS = {
    "natural": "自然死亡", "hunger": "饿死", "weapon": "战死", "fire": "烧死",
    "explosion": "炸死", "drowning": "溺死", "gravity": "摔死", "eaten": "被吞噬",
    "plague": "瘟疫", "poison": "中毒", "infection": "感染", "tumor": "肿瘤",
    "acid": "酸蚀", "divine": "神罚", "water": "水流", "other": "其他",
}

def _label(mapping: dict, key: Any, fallback: str = "") -> str:
    if key in mapping:
        return mapping[key]
    return fallback or str(key)


def _now() -> float:
    return time.time()


def slugify(name: str, fallback: str = "未命名") -> str:
    """生成文件系统与命令行都安全的目录名，保留中日韩文字。"""
    import re
    import unicodedata

    name = unicodedata.normalize("NFKC", name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", "-", name)
    name = name.strip(".-")
    if not name:
        return fallback
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if name.upper().split(".")[0] in reserved:
        return f"_{name}"
    return name[:60]


def _species_label(snap: WorldSnapshot, asset_id: str) -> str:
    sub = next((s for s in snap.subspecies.values()
                if _as_str(s.get("species_id")) == asset_id), None)
    if sub:
        return _as_str(sub.get("name"), asset_id)
    return asset_id or "未知"


def _resolve(snap: WorldSnapshot, table: str, entity_id: Any) -> str:
    if not isinstance(entity_id, int):
        return ""
    bucket = {
        "kingdom": snap.kingdoms, "city": snap.cities, "army": snap.armies,
        "war": snap.wars, "culture": snap.cultures, "language": snap.languages,
        "religion": snap.religions, "clan": snap.clans, "actor": snap.actors,
        "alliance": snap.alliances, "subspecies": snap.subspecies,
    }.get(table, {})
    row = bucket.get(entity_id)
    return _as_str(row.get("name")) if row else ""


def _traits(raw: Any) -> list[dict[str, str]]:
    out = []
    for trait in _as_list(raw):
        if isinstance(trait, str):
            out.append({"id": trait, "label": KINGDOM_TRAIT_LABELS.get(trait, trait)})
    return out


# 存档超过这个秒数就认为"战报过期"，用来提醒 agent 别把旧数据当实时局势。
STALE_AFTER_SECONDS = 600


def _world_header(snap: WorldSnapshot) -> dict[str, Any]:
    age = max(0.0, _now() - snap.timestamp)
    header: dict[str, Any] = {
        "世界名称": snap.name,
        "年份": snap.year,
        "存档时间戳": snap.timestamp,
        "存档距今秒数": round(age, 1),
        "_note": "存档距今秒数过大说明游戏已暂停或关闭；此时数据是最后一次自动存档的状态",
    }
    if age > STALE_AFTER_SECONDS:
        header["_警告"] = (
            f"⚠️ 这份数据已经 {age / 60:.1f} 分钟旧（阈值 "
            f"{STALE_AFTER_SECONDS // 60} 分钟）。游戏可能已暂停或关闭，"
            "世界局势很可能已经变化。请明确告知使用者数据时效，不要把旧战报当实时局势。"
        )
    return header


# --------------------------------------------------------------------------
# 城市 / 军队子视图
# --------------------------------------------------------------------------

def _city_view(snap: WorldSnapshot, city_id: int, kingdom_id: int) -> dict[str, Any]:
    city = snap.cities[city_id]
    units = snap.city_units.get(city_id, [])
    buildings = sum(1 for b_id in snap.city_building_index.get(city_id, ()))
    housing = snap.city_housing.get(city_id, {})
    soldiers = sum(1 for u in units if isinstance(snap.actors.get(u, {}).get("army"), int))
    leader_id = city.get("leaderID")
    leader = snap.actors.get(leader_id) if isinstance(leader_id, int) else None
    return {
        "id": city_id,
        "名称": _as_str(city.get("name")),
        "是否首都": city_id == snap.kingdoms.get(kingdom_id, {}).get("capitalID"),
        # 地图坐标：领主可以把这些坐标直接交给创世者，用作施放神力的落点。
        "坐标": snap.city_centers.get(city_id),
        "坐标说明": "城市地块的重心，可直接作为『在某处投放食物/矿物』的落点",
        "人口": len(units),
        "含船只条目数": snap.city_all_units.get(city_id, 0),
        "其中船只": snap.city_boats.get(city_id, 0),
        "其中士兵": soldiers,
        "领土地块": len(_as_list(city.get("zones"))),
        "建筑数": buildings,
        "房屋数": housing.get("houses", 0),
        "有房人数": housing.get("housed", 0),
        "无家可归": housing.get("homeless", 0),
        "城市领袖": _as_str(leader.get("name")) if leader else "",
        "城市领袖id": leader_id if isinstance(leader_id, int) else None,
        "历代领袖数": _as_int(city.get("total_leaders"), 0),
        "声望": _as_int(city.get("renown"), 0),
        "累计击杀": _as_int(city.get("total_kills"), 0),
        "累计死亡": _as_int(city.get("total_deaths"), 0),
        "累计出生": _as_int(city.get("total_births"), 0),
        "累计消耗食物": _as_int(city.get("total_food_consumed"), 0),
        "建国年": int(city.get("created_time") or 0),
        "加入现王国年": int(city.get("timestamp_kingdom") or 0),
        "曾属王国id": city.get("last_kingdom_id"),
        "语言id": city.get("id_language"),
        "语言": _resolve(snap, "language", city.get("id_language")),
        "宗教id": city.get("id_religion"),
        "宗教": _resolve(snap, "religion", city.get("id_religion")),
        "种族": _as_str(city.get("original_actor_asset")),
        "旧称": [str(x) for x in _as_list(city.get("past_names"))][-5:],
        "_note": "存档中城市本身不记录军队/忠诚度/资源库存，这些请看 history 与军事段",
    }


def _army_view(snap: WorldSnapshot, army_id: int) -> dict[str, Any]:
    army = snap.armies[army_id]
    size = snap.army_size_map.get(army_id, 0)
    return {
        "id": army_id,
        "番号": _as_str(army.get("name")),
        "兵力": size,
        "队长": _resolve(snap, "actor", army.get("id_captain")),
        "队长id": army.get("id_captain") if isinstance(army.get("id_captain"), int) else None,
        "驻地城市": _resolve(snap, "city", army.get("id_city")),
        "累计击杀": _as_int(army.get("total_kills"), 0),
        "累计阵亡": _as_int(army.get("total_deaths"), 0),
        "成立年": int(army.get("created_time") or 0),
    }


# --------------------------------------------------------------------------
# 本国完整情报
# --------------------------------------------------------------------------

def kingdom_self_view(snap: WorldSnapshot, kingdom_id: int, *,
                      history_table: str = "KingdomYearly1",
                      history_limit: int = MAX_HISTORY) -> Optional[dict[str, Any]]:
    """领袖 agent 用来做决策的完整本国国情报告。"""
    kingdom = snap.kingdoms.get(kingdom_id)
    if kingdom is None:
        return None

    king_id = kingdom.get("kingID")
    king = snap.actors.get(king_id) if isinstance(king_id, int) else None
    royal_clan_id = kingdom.get("royal_clan_id")
    royal_clan = snap.clans.get(royal_clan_id) if isinstance(royal_clan_id, int) else None

    unit_ids = snap.kingdom_units.get(kingdom_id, [])
    sick = hungry = soldiers = 0
    happiness: list[int] = []
    money: list[int] = []
    professions: dict[int, int] = {}
    generations: dict[int, int] = {}

    for uid in unit_ids:
        actor = snap.actors.get(uid)
        if actor is None:
            continue
        if isinstance(actor.get("army"), int):
            soldiers += 1
        health = _as_int(actor.get("health"), 10)
        nutrition = _as_int(actor.get("nutrition"), 100)
        if health <= 20:
            sick += 1
        if nutrition <= 25:
            hungry += 1
        happiness.append(_as_int(actor.get("happiness"), 0))
        if isinstance(actor.get("money"), int):
            money.append(actor["money"])
        prof = actor.get("profession")
        if isinstance(prof, int):
            professions[prof] = professions.get(prof, 0) + 1
        gen = actor.get("generation")
        if isinstance(gen, int):
            generations[gen] = generations.get(gen, 0) + 1

    cities = [_city_view(snap, cid, kingdom_id)
              for cid in snap.kingdom_cities.get(kingdom_id, [])]
    cities.sort(key=lambda c: (c["是否首都"], c["人口"]), reverse=True)

    armies = [_army_view(snap, aid) for aid in snap.kingdom_armies.get(kingdom_id, [])]
    armies.sort(key=lambda a: a["兵力"], reverse=True)

    wars = []
    for war in snap.wars_of(kingdom_id):
        attackers = [k for k in _as_list(war.get("list_attackers")) if isinstance(k, int)]
        defenders = [k for k in _as_list(war.get("list_defenders")) if isinstance(k, int)]
        is_attacker = kingdom_id in attackers
        opponents = defenders if is_attacker else attackers
        same_side = attackers if is_attacker else defenders
        wars.append({
            "id": war["id"],
            "战争名称": _as_str(war.get("name")),
            "我方角色": "进攻方" if is_attacker else "防守方",
            "战争类型": _as_str(war.get("war_type")),
            "战争类型说明": _label(WAR_TYPE_LABELS, _as_str(war.get("war_type")), "未知"),
            "敌方": [{"id": o, "名称": _resolve(snap, "kingdom", o),
                      "人口": snap.population_of(o),
                      "城市数": snap.city_count_of(o),
                      "声望": _as_int((snap.kingdoms.get(o) or {}).get("renown"), 0)}
                     for o in opponents],
            "共同交战国": [{"id": o, "名称": _resolve(snap, "kingdom", o)}
                           for o in same_side if o != kingdom_id],
            "由谁发起": _as_str(war.get("started_by_actor_name")),
            "发起者id": war.get("started_by_actor_id"),
            "爆发年": int(war.get("created_time") or 0),
            "总死亡": _as_int(war.get("total_deaths"), 0),
            "我方阵亡": _as_int(war.get("dead_attackers" if is_attacker else "dead_defenders"), 0),
            "敌方阵亡": _as_int(war.get("dead_defenders" if is_attacker else "dead_attackers"), 0),
            "声望收益": _as_int(war.get("renown"), 0),
        })
    wars.sort(key=lambda w: w["总死亡"], reverse=True)

    relations = []
    for other in snap.kingdoms.values():
        if other["id"] == kingdom_id:
            continue
        oid = other["id"]
        rel = snap.relation_between(kingdom_id, oid)
        alliance = next((a for a in snap.alliances.values()
                         if kingdom_id in _as_list(a.get("list_kingdoms"))
                         and oid in _as_list(a.get("list_kingdoms"))), None)
        # 战争关系按"双方是否出现在同一场战争的两侧"判断
        at_war = bool(set(snap.kingdom_wars.get(kingdom_id, []))
                      & set(snap.kingdom_wars.get(oid, [])))
        stance = "war" if at_war else ("alliance" if alliance else ("contact" if rel else "unknown"))
        relations.append({
            "id": oid,
            "名称": _as_str(other.get("name")),
            "关系": _label(STANCE_LABELS, stance),
            "关系码": stance,
            "同盟名称": _as_str(alliance.get("name")) if alliance else None,
            "人口": snap.population_of(oid),
            "城市数": snap.city_count_of(oid),
            "军队": snap.army_size_of(oid),
            "声望": _as_int(other.get("renown"), 0),
            "种族": _as_str(other.get("original_actor_asset")),
            "是否已灭亡": not snap.kingdom_cities.get(oid),
        })
    relations.sort(key=lambda r: (r["关系码"] != "war", -r["声望"]))

    events = []
    for row in snap.kingdom_log.get(kingdom_id, [])[-MAX_EVENTS:]:
        asset = _as_str(row.get("asset_id"))
        events.append({
            "年份": row.get("timestamp"),
            "事件": _label(EVENT_LABELS, asset, asset),
            "事件码": asset,
            "主角": row.get("special1"),
            "对象": row.get("special2"),
            "补充": row.get("special3"),
            "单位id": row.get("unit_id"),
            "坐标": [row.get("x"), row.get("y")],
        })

    live = snap.live_kingdoms()
    pop_rank = next((i + 1 for i, k in enumerate(
        sorted(snap.kingdoms.values(), key=lambda k: snap.population_of(k["id"]), reverse=True))
        if k["id"] == kingdom_id), 0)

    previous_rulers = []
    for past in _as_list(kingdom.get("past_rulers"))[-MAX_RULERS:]:
        if isinstance(past, dict):
            previous_rulers.append({
                "姓名": _as_str(past.get("name")),
                "id": past.get("id"),
                "继位年": int(past.get("timestamp_ago") or 0),
                "终止年": int(past.get("timestamp_end") or 0),
            })

    history = snap.history_of(kingdom_id, history_table, history_limit)
    latest_history = history[-1] if history else {}

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "nation.self",
        "文档类型": "本国国情报告",
        "生成时间": snap.generated_at,
        "world": _world_header(snap),
        "identity": {
            "id": kingdom_id,
            "名称": _as_str(kingdom.get("name")),
            "格言": _as_str(kingdom.get("motto")),
            "种族": _as_str(kingdom.get("original_actor_asset")),
            "种族称呼": _species_label(snap, _as_str(kingdom.get("original_actor_asset"))),
            "国旗色号": kingdom.get("color_id"),
            "国旗图标号": kingdom.get("banner_icon_id"),
            "国旗背景号": kingdom.get("banner_background_id"),
            "建国年": int(kingdom.get("created_time") or 0),
            "旧国名": [str(x) for x in _as_list(kingdom.get("past_names"))][-5:],
        },
        "ruler": {
            "国王": _as_str(king.get("name")) if king else "",
            "国王id": king_id if isinstance(king_id, int) else None,
            "国王特质": _traits(king.get("saved_traits")) if king else [],
            "国王健康": _as_int(king.get("health"), 0) if king else None,
            "国王营养": _as_int(king.get("nutrition"), 0) if king else None,
            "国王幸福度": _as_int(king.get("happiness"), 0) if king else None,
            "国王所在城市": _resolve(snap, "city", king.get("cityID")) if king else "",
            "国王击杀数": _as_int(king.get("kills"), 0) if king else 0,
            "国王声望": _as_int(king.get("renown"), 0) if king else 0,
            "世代": _as_int(king.get("generation"), 0) if king else 0,
            "在位起始": int(kingdom.get("timestamp_king_rule") or 0),
            "王室宗族": _as_str(royal_clan.get("name")) if royal_clan else "",
            "王室宗族id": royal_clan_id if isinstance(royal_clan_id, int) else None,
            "历任国王数": _as_int(kingdom.get("total_kings"), 0),
            "前任国王": previous_rulers,
            "_note": "存档不记录角色年龄，只能给出世代与在位时间",
        },
        "status": {
            "是否存续": bool(snap.kingdom_cities.get(kingdom_id)),
            "声望": _as_int(kingdom.get("renown"), 0),
            "声望排名": snap.renown_rank(kingdom_id),
            "人口排名": pop_rank,
            "国家特质": _traits(kingdom.get("saved_traits")),
            "上次战争年": int(kingdom.get("timestamp_last_war") or 0),
            "上次征服年": int(kingdom.get("timestamp_new_conquest") or 0),
            "上次结盟年": int(kingdom.get("timestamp_alliance") or 0),
            "迁出人数": _as_int(kingdom.get("left"), 0),
            "迁入人数": _as_int(kingdom.get("moved"), 0),
            "向外迁徙": _as_int(kingdom.get("migrated"), 0),
        },
        "demographics": {
            "人口": len(unit_ids),
            "含船只条目数": snap.total_population_of(kingdom_id),
            "成年人口": latest_history.get("adults"),
            "儿童人口": latest_history.get("children"),
            "船只": snap.boats_of(kingdom_id),
            "患病": sick,
            "饥饿": hungry,
            "平均幸福度": round(sum(happiness) / len(happiness), 1) if happiness else 0,
            "幸福度区间": [min(happiness), max(happiness)] if happiness else [],
            "职业分布": [{"profession_id": k, "职业": _label(PROFESSION_LABELS, k, f"未知({k})"),
                          "人数": v} for k, v in sorted(professions.items(), key=lambda x: -x[1])],
            "世代分布": [{"世代": k, "人数": v}
                         for k, v in sorted(generations.items())],
            "金钱总量": sum(money) if money else None,
            "人均金钱": round(sum(money) / len(money), 1) if money else None,
            "累计出生": _as_int(kingdom.get("total_births"), 0),
            "累计死亡": _as_int(kingdom.get("total_deaths"), 0),
            "死因统计": [
                {"死因": _label(DEATH_CAUSE_LABELS, cause, cause),
                 "人数": _as_int(kingdom.get(f"deaths_{cause}"), 0)}
                for cause in ("natural", "hunger", "weapon", "fire", "explosion",
                              "drowning", "gravity")
                if _as_int(kingdom.get(f"deaths_{cause}"), 0) > 0
            ],
            "_note": "成年/儿童与粮食、住房等更细的数据来自 history（游戏逐年统计）",
        },
        "military": {
            "军队总兵力": soldiers,
            "兵力占人口比": round(soldiers / len(unit_ids) * 100, 1) if unit_ids else 0,
            "军队支数": len(armies),
            "累计击杀": _as_int(kingdom.get("total_kills"), 0),
            "进行中的战争数": len(wars),
            "军队明细": armies[:MAX_ARMIES],
        },
        "territory": {
            "城市数": len(cities),
            "领土地块": snap.kingdom_territory.get(kingdom_id, 0),
            "建筑总数": snap.kingdom_buildings.get(kingdom_id, 0),
            "首都": _resolve(snap, "city", kingdom.get("capitalID")),
            "首都id": kingdom.get("capitalID"),
            "上一任首都id": kingdom.get("last_capital_id"),
            "城市列表": cities[:MAX_CITIES],
        },
        "culture_and_faith": {
            "国家文化": _resolve(snap, "culture", kingdom.get("id_culture")),
            "国家文化id": kingdom.get("id_culture"),
            "国家语言": _resolve(snap, "language", kingdom.get("id_language")),
            "国家语言id": kingdom.get("id_language"),
            "国家宗教": _resolve(snap, "religion", kingdom.get("id_religion")),
            "国家宗教id": kingdom.get("id_religion"),
            "境内使用的语言": sorted({c["语言"] for c in cities if c["语言"]}),
            "境内宗族": [
                {"id": c["id"], "名称": _as_str(c.get("name")),
                 "累计击杀": _as_int(c.get("total_kills"), 0),
                 "累计死亡": _as_int(c.get("total_deaths"), 0),
                 "声望": _as_int(c.get("renown"), 0),
                 "格言": _as_str(c.get("motto"))}
                for c in sorted(snap.clans.values(),
                                key=lambda c: _as_int(c.get("total_kills"), 0),
                                reverse=True)[:MAX_CLANS]
            ],
        },
        "diplomacy": {
            "战争": wars[:MAX_WARS],
            "对外关系": relations[:MAX_RELATIONS],
            "盟友": [{"id": a, "名称": _resolve(snap, "kingdom", a)}
                     for a in snap.ally_ids_of(kingdom_id)],
            "敌人": [{"id": e, "名称": _resolve(snap, "kingdom", e)}
                     for e in snap.enemy_ids_of(kingdom_id)],
        },
        "recent_events": events,
        "history": history,
        "history_meta": {
            "来源表": history_table,
            "可用": bool(history),
            "行数": len(history),
            "说明": "来自 map_stats.s3db 的游戏逐年统计，是本工具最可靠的趋势数据；"
                    "如果为空说明该存档没有配套统计库",
            "字段含义": {
                "timestamp": "游戏年份",
                "population": "总人口", "adults": "成年人口", "children": "儿童人口",
                "army": "军队人数", "boats": "船只", "territory": "领土地块",
                "buildings": "建筑数", "food": "粮食储备", "money": "国库金钱",
                "kills": "该年击杀", "deaths": "该年死亡", "births": "该年出生",
                "hungry": "饥饿人数", "starving": "濒临饿死人数",
                "happy": "幸福度总和", "homeless": "无家可归", "housed": "有房人数",
                "families": "家庭数", "males": "男性", "females": "女性",
                "cities": "城市数", "renown": "声望", "joined": "加入人数",
                "left": "离开人数", "moved": "迁入人数", "migrated": "对外迁徙",
            },
        },
        "knowledge": {
            "可以确信": ["本国人口、军队、城市、国王、宗族、外交与历史",
                        "本国全部事件记录"],
            "仅为估算": ["他国人口的数字是精确的，但他国内部的饥荒、忠诚度只能看到表象"],
            "无法得知": ["他国军队的具体位置", "尚未接触前发生在远方的事件",
                        "任何角色的年龄（存档本身不记录）"],
            "口径歧义提示": [
                "本文件的『人口』不含船只；『含船只条目数』是含船只的存档条目数。"
                "两者都会给出，避免你误以为其中一个是错的。",
                f"『声望』这里是存档即时值 {_as_int(kingdom.get('renown'), 0)}；"
                f"而 history 里 {history_table} 最新一行的 renown 是游戏当年年初的统计值"
                f"{latest_history.get('renown')}。两个都对，只是时间点不同："
                "判断当前国力用即时值，判断趋势用 history。",
                "军队同理：即时『军队总兵力』对比 history 的 army，差异来自两者时点不同。",
            ],
        },
    }


# --------------------------------------------------------------------------
# 他国简报 / 世界概览 / 索引
# --------------------------------------------------------------------------

def kingdom_brief(snap: WorldSnapshot, kingdom_id: int) -> Optional[dict[str, Any]]:
    kingdom = snap.kingdoms.get(kingdom_id)
    if kingdom is None:
        return None
    return {
        "id": kingdom_id,
        "名称": _as_str(kingdom.get("name")),
        "是否存续": bool(snap.kingdom_cities.get(kingdom_id)),
        "种族": _as_str(kingdom.get("original_actor_asset")),
        "国王": _resolve(snap, "actor", kingdom.get("kingID")),
        "国王id": kingdom.get("kingID") if isinstance(kingdom.get("kingID"), int) else None,
        "国王特质": _traits((snap.actors.get(kingdom.get("kingID")) or {}).get("saved_traits")),
        "格言": _as_str(kingdom.get("motto")),
        "首都": _resolve(snap, "city", kingdom.get("capitalID")),
        "首都坐标": snap.city_centers.get(kingdom.get("capitalID"))
                    if isinstance(kingdom.get("capitalID"), int) else None,
        "人口": snap.population_of(kingdom_id),
        "城市数": snap.city_count_of(kingdom_id),
        "领土地块": snap.kingdom_territory.get(kingdom_id, 0),
        "建筑数": snap.kingdom_buildings.get(kingdom_id, 0),
        "军队兵力": snap.army_size_of(kingdom_id),
        "声望": _as_int(kingdom.get("renown"), 0),
        "累计击杀": _as_int(kingdom.get("total_kills"), 0),
        "累计死亡": _as_int(kingdom.get("total_deaths"), 0),
        "建国年": int(kingdom.get("created_time") or 0),
        "迁入人数": _as_int(kingdom.get("moved"), 0),
        "迁出人数": _as_int(kingdom.get("left"), 0),
        "国家文化": _resolve(snap, "culture", kingdom.get("id_culture")),
        "国家特质": _traits(kingdom.get("saved_traits")),
    }


def _city_brief(snap: WorldSnapshot, city_id: int) -> dict[str, Any]:
    """城市精简条目：外交与军事调动只需要这些。"""
    city = snap.cities[city_id]
    housing = snap.city_housing.get(city_id, {})
    return {
        "id": city_id,
        "名称": _as_str(city.get("name")),
        "坐标": snap.city_centers.get(city_id),
        "人口": len(snap.city_units.get(city_id, ())),
        "其中士兵": sum(1 for u in snap.city_units.get(city_id, ())
                        if isinstance(snap.actors.get(u, {}).get("army"), int)),
        "领土地块": len(_as_list(city.get("zones"))),
        "建筑数": len(snap.city_building_index.get(city_id, ())),
        "无家可归": housing.get("homeless", 0),
        "是否首都": False,  # 由调用方按需覆盖
        # 领土主张的唯一线索：这座城市上一任属于谁。判断"这仗该不该打、
        # 这块地是不是旧账"只能靠它，而它只存在于城市记录里。
        "曾属王国id": city.get("last_kingdom_id"),
        "加入现王国年": int(city.get("timestamp_kingdom") or 0),
        "旧称": [str(x) for x in _as_list(city.get("past_names"))][-3:],
    }


def _territory_hints(snap: WorldSnapshot, kingdom_id: int) -> list[dict[str, Any]]:
    """该国的城市里，哪些曾经属于别的国家——潜在领土主张与怨仇来源。"""
    hints = []
    for cid in snap.kingdom_cities.get(kingdom_id, ()):
        city = snap.cities[cid]
        previous = city.get("last_kingdom_id")
        if not isinstance(previous, int) or previous == kingdom_id:
            continue
        hints.append({
            "城市": _as_str(city.get("name")),
            "坐标": snap.city_centers.get(cid),
            "原属王国id": previous,
            "原属王国": _resolve(snap, "kingdom", previous) or ("已灭亡" if previous not in snap.kingdoms else ""),
            "加入现王国年": int(city.get("timestamp_kingdom") or 0),
            "旧称": [str(x) for x in _as_list(city.get("past_names"))[-3:]],
        })
    return hints


def others_view(snap: WorldSnapshot, kingdom_id: int) -> dict[str, Any]:
    """站在本国立场上，其他所有国家的精简横向对比 + 外交关系网。"""
    enemies = set(snap.enemy_ids_of(kingdom_id))
    allies = set(snap.ally_ids_of(kingdom_id))

    others = []
    for other in snap.live_kingdoms():
        oid = other["id"]
        if oid == kingdom_id:
            continue
        brief = kingdom_brief(snap, oid)
        if brief is None:
            continue
        stance = ("enemy" if oid in enemies else
                  "ally" if oid in allies else "neutral")
        # 对方的城市与坐标：宣战、调动、防御都需要落点，而 others.json 是
        # skill 明确要求"任何外交决定之前"读的文件，所以这里必须自带坐标。
        capital_id = other.get("capitalID")
        cities = []
        for cid in snap.kingdom_cities.get(oid, ()):
            entry = _city_brief(snap, cid)
            entry["是否首都"] = cid == capital_id
            cities.append(entry)
        cities.sort(key=lambda c: (c["是否首都"], c["人口"]), reverse=True)
        brief["首都坐标"] = snap.city_centers.get(capital_id) if isinstance(capital_id, int) else None
        brief["城市列表"] = cities
        brief["领土主张线索"] = _territory_hints(snap, oid)
        brief["我方立场"] = _label(STANCE_LABELS, stance)
        brief["我方立场码"] = stance
        brief["其参与的战争"] = sorted({_as_str(w.get("name"))
                                        for w in snap.wars_of(oid)})
        others.append(brief)

    matrix: dict[str, str] = {}
    wars_by_pair = []
    live = {k["id"] for k in snap.live_kingdoms()}
    for war in snap.wars.values():
        attackers = [k for k in _as_list(war.get("list_attackers")) if k in live]
        defenders = [k for k in _as_list(war.get("list_defenders")) if k in live]
        for a in attackers:
            for d in defenders:
                matrix[f"{a}|{d}"] = "war"
                matrix[f"{d}|{a}"] = "war"
        wars_by_pair.append({
            "id": war["id"],
            "战争名称": _as_str(war.get("name")),
            "进攻方": [{"id": k, "名称": _resolve(snap, "kingdom", k)} for k in attackers],
            "防守方": [{"id": k, "名称": _resolve(snap, "kingdom", k)} for k in defenders],
            "总死亡": _as_int(war.get("total_deaths"), 0),
            "爆发年": int(war.get("created_time") or 0),
        })
    for alliance in snap.alliances.values():
        members = [k for k in _as_list(alliance.get("list_kingdoms")) if k in live]
        for a in members:
            for b in members:
                if a != b:
                    matrix.setdefault(f"{a}|{b}", "alliance")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "nation.others",
        "文档类型": "列国简报",
        "生成时间": snap.generated_at,
        "world": _world_header(snap),
        "viewpoint_kingdom_id": kingdom_id,
        "本国名称": _resolve(snap, "kingdom", kingdom_id),
        "他国数量": len(others),
        "列国": others,
        "正在进行的战争": wars_by_pair,
        "关系矩阵": matrix,
        "关系矩阵说明": "键为 'A|B'，值为 war（交战）或 alliance（同盟）；"
                        "A、B 都是国家 id，可到 _index.json 查名字",
        "同盟": [
            {"id": a["id"], "名称": _as_str(a.get("name")),
             "成员": [{"id": m, "名称": _resolve(snap, "kingdom", m)}
                      for m in _as_list(a.get("list_kingdoms")) if m in live]}
            for a in snap.alliances.values()
        ],
    }


def world_overview(snap: WorldSnapshot) -> dict[str, Any]:
    """全局局势：谁存在、谁在打谁、世界长期趋势。"""
    live = snap.live_kingdoms()
    stats = snap.world_stats
    deaths_by_cause = {}
    for key, value in stats.items():
        if key.startswith("deaths_") and isinstance(value, int) and value:
            cause = key[len("deaths_"):]
            deaths_by_cause[_label(DEATH_CAUSE_LABELS, cause, cause)] = value

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "world.overview",
        "文档类型": "世界概览",
        "生成时间": snap.generated_at,
        "source": {"path": snap.source, "kind": snap.source_kind,
                   "save_version": snap.save_version},
        "存档距今秒数": round(max(0.0, _now() - snap.timestamp), 1),
        "world": {
            "名称": _as_str(stats.get("name"), snap.name),
            "描述": _as_str(stats.get("description")),
            "可用坐标范围": (
                {"x": [snap.coord_bounds[0], snap.coord_bounds[2]],
                 "y": [snap.coord_bounds[1], snap.coord_bounds[3]],
                 "说明": "实测自地图上的城市、角色与建筑；给创世者指落点时用这个范围"}
                if len(snap.coord_bounds) == 4 else None
            ),
            "存档引擎尺寸字段": f"{snap.width}x{snap.height}",
            "_尺寸提示": "『存档引擎尺寸字段』是游戏内部分块数，不是地图大小，"
                        "不要拿它来判断地图范围；请用『可用坐标范围』",
            "年份": _as_int(stats.get("history_current_year"), snap.year),
            "世界时间": stats.get("world_time"),
            "纪元": _as_str(stats.get("world_age_id")),
            "纪元进度": stats.get("current_age_progress"),
            "纪元已暂停": bool(stats.get("is_world_ages_paused")),
            "玩家名": _as_str(stats.get("player_name")),
            "玩家心情": _as_str(stats.get("player_mood")),
            "世界法则": snap.world_laws,
            "是否使用模组": bool(stats.get("modded")),
            "启用的模组": _as_list(stats.get("modsActive")),
        },
        "totals": {
            "现存王国": len(live),
            "累计建立王国_游戏计数器": _as_int(stats.get("kingdomsCreated"), 0),
            "城市总数": len(snap.cities),
            "存档角色条目总数": len(snap.actors),
            "各国常住人口之和": sum(snap.population_of(k["id"]) for k in live),
            "进行中的战争": len(snap.wars),
            "同盟数": len(snap.alliances),
            "文化数": len(snap.cultures),
            "语言数": len(snap.languages),
            "宗教数": len(snap.religions),
            "宗族数": len(snap.clans),
            "家族数": len(snap.families),
            "军队数": len(snap.armies),
            "累计死亡": _as_int(stats.get("deaths"), 0),
            "死因分布": deaths_by_cause,
            "_口径提示": "『各国常住人口之和』不含船只，可能略小于『存档角色条目总数』"
                        "（后者含船只与不属于任何国家的野怪）。"
                        "『累计建立王国_游戏计数器』来自游戏统计，"
                        "在该世界里可能出现小于现存王国数的反常值，仅供参考。",
        },
        "国力排行": [kingdom_brief(snap, k["id"]) for k in live],
        "进行中的战争": [
            {"id": w["id"], "战争名称": _as_str(w.get("name")),
             "进攻方": [{"id": k, "名称": _resolve(snap, "kingdom", k)}
                        for k in _as_list(w.get("list_attackers"))],
             "防守方": [{"id": k, "名称": _resolve(snap, "kingdom", k)}
                        for k in _as_list(w.get("list_defenders"))],
             "总死亡": _as_int(w.get("total_deaths"), 0),
             "爆发年": int(w.get("created_time") or 0),
             "声望收益": _as_int(w.get("renown"), 0)}
            for w in sorted(snap.wars.values(),
                            key=lambda w: _as_int(w.get("total_deaths"), 0), reverse=True)
        ],
        "世界逐年统计": (snap.stats.world_history("WorldYearly1", 60)
                         if snap.stats and snap.stats.available else []),
        "全局事件记录": [
            {"年份": r.get("timestamp"),
             "事件": _label(EVENT_LABELS, _as_str(r.get("asset_id")), _as_str(r.get("asset_id"))),
             "事件码": r.get("asset_id"),
             "主角": r.get("special1"), "对象": r.get("special2"),
             "补充": r.get("special3"), "王国id": r.get("kingdom_id"),
             "单位id": r.get("unit_id"), "坐标": [r.get("x"), r.get("y")]}
            for r in snap.world_log[-80:]
        ],
        "统计数据源": {
            "可用": bool(snap.stats and snap.stats.available),
            "路径": str(snap.stats.path) if snap.stats and snap.stats.path else None,
            "错误": snap.stats.error if snap.stats else "未打开",
        },
    }


def nation_index(snap: WorldSnapshot, slugs: dict[int, str]) -> dict[str, Any]:
    """查找表：国名 -> id -> 目录。agent 应该最先读这个文件。"""
    by_name: dict[str, list[int]] = {}
    entries = []
    for kingdom in snap.kingdoms.values():
        kid = kingdom["id"]
        name = _as_str(kingdom.get("name"), f"王国{kid}")
        by_name.setdefault(name, []).append(kid)
        slug = slugs.get(kid, f"王国{kid}")
        entries.append({
            "id": kid,
            "名称": name,
            "目录": f"nations/{slug}",
            "是否存续": bool(snap.kingdom_cities.get(kid)),
            "人口": snap.population_of(kid),
            "城市数": snap.city_count_of(kid),
            "军队兵力": snap.army_size_of(kid),
            "声望": _as_int(kingdom.get("renown"), 0),
        })
    entries.sort(key=lambda e: (not e["是否存续"], -e["声望"]))
    age = max(0.0, _now() - snap.timestamp)
    index: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "world.index",
        "文档类型": "列国索引",
        "生成时间": snap.generated_at,
        "世界名称": snap.name,
        "年份": snap.year,
        "存档距今秒数": round(age, 1),
        "王国数量": len(entries),
        "kingdoms": entries,
        "按名称查id": {k: (v[0] if len(v) == 1 else v) for k, v in by_name.items()},
        "id到目录": {str(k): f"nations/{v}" for k, v in slugs.items()},
        "用法": "先用『按名称查id』把自己的国名换成 id，再打开 id到目录 对应的文件夹读 self.json",
        "人口口径": "这里的『人口』不含船只，与各国 self.json 的 demographics.人口 一致",
    }
    if age > STALE_AFTER_SECONDS:
        index["_警告"] = (
            f"⚠️ 这份数据已经 {age / 60:.1f} 分钟旧（阈值 "
            f"{STALE_AFTER_SECONDS // 60} 分钟）。游戏可能已暂停或关闭，"
            "请明确告知使用者数据时效。"
        )
    return index
