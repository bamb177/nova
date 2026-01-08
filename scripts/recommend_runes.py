# scripts/recommend_runes.py
import os, json, glob, subprocess, re
from typing import Dict, Any, List, Tuple, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "public", "data", "zone-nova")
CHAR_DIR = os.path.join(DATA_DIR, "characters_ko")

RUNES_EXPORT = os.path.join(DATA_DIR, "runes_export.json")
OUT_JSON = os.path.join(DATA_DIR, "runes_recommendations.json")
OVERRIDES_JSON = os.path.join(DATA_DIR, "rune_overrides.json")


# ----------------------------
# IO helpers
# ----------------------------
def safe_read_json(p: str) -> Dict[str, Any]:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def ensure_runes_export():
    if os.path.exists(RUNES_EXPORT):
        return
    script = os.path.join(ROOT, "scripts", "export_runes_json.mjs")
    if not os.path.exists(script):
        raise FileNotFoundError("scripts/export_runes_json.mjs not found.")
    subprocess.check_call(["node", script], cwd=ROOT)

def pick_first(d: Dict[str, Any], keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return default

def element_of(ch: Dict[str, Any]) -> str:
    e = pick_first(ch, ["element", "attr", "attribute"], "")
    return str(e or "").strip()

def deep_merge(base: Any, patch: Any) -> Any:
    # dict: 재귀 merge, list: patch가 있으면 patch로 교체, scalar: patch 우선
    if patch is None:
        return base
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = deep_merge(out.get(k), v)
        return out
    if isinstance(patch, list):
        return patch
    return patch

def to_text_list(ch: Dict[str, Any]) -> List[str]:
    """
    characters_ko json 구조가 제각각일 수 있어 description 류 문자열을 최대한 긁어모음
    """
    texts: List[str] = []

    def walk(x: Any):
        if x is None:
            return
        if isinstance(x, str):
            s = x.strip()
            if s:
                texts.append(s)
            return
        if isinstance(x, list):
            for i in x:
                walk(i)
            return
        if isinstance(x, dict):
            for k in ["description", "desc", "effect", "details", "text", "tooltip"]:
                v = x.get(k)
                if isinstance(v, str) and v.strip():
                    texts.append(v.strip())
            for v in x.values():
                walk(v)

    for key in ["skills", "teamSkill", "awakenings", "memoryCard", "ultimate", "passive"]:
        if key in ch:
            walk(ch[key])
    walk(ch)

    # de-dup (case-insensitive)
    seen = set()
    out = []
    for t in texts:
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        out.append(t)
    return out


# ----------------------------
# Runes export parsing
# ----------------------------
def normalize_runes_export(runes_data: Any) -> List[Dict[str, Any]]:
    """
    runes_export.json 구조가 list/dict 등 다양할 수 있어 list[dict]로 정규화.
    """
    out: List[Dict[str, Any]] = []
    if isinstance(runes_data, list):
        for x in runes_data:
            if isinstance(x, dict):
                out.append(x)
        return out

    if isinstance(runes_data, dict):
        for k, v in runes_data.items():
            if isinstance(v, dict):
                y = dict(v)
                y.setdefault("name", v.get("name") or v.get("title") or v.get("setName") or k)
                out.append(y)
        return out

    return out

def rune_set_name(x: Dict[str, Any]) -> str:
    return str(x.get("name") or x.get("title") or x.get("setName") or "").strip()

def rune_set_icon(x: Dict[str, Any]) -> Optional[str]:
    for k in ["img", "image", "icon", "file", "filename", "jpg", "png", "src"]:
        v = x.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def rune_set_effect(x: Dict[str, Any], pieces: int) -> Optional[str]:
    candidates = []
    if pieces == 2:
        candidates = ["set2", "twoSet", "effect2", "bonus2", "twoPieces", "two_piece", "2set"]
    else:
        candidates = ["set4", "fourSet", "effect4", "bonus4", "fourPieces", "four_piece", "4set"]

    for k in candidates:
        v = x.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    v = x.get("effects")
    if isinstance(v, dict):
        key = "2" if pieces == 2 else "4"
        w = v.get(key)
        if isinstance(w, str) and w.strip():
            return w.strip()
    return None

def build_rune_sets_map(sets: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    m: Dict[str, Dict[str, Any]] = {}
    for s in sets:
        name = rune_set_name(s)
        if not name:
            continue
        m[name] = {
            "name": name,
            "icon": rune_set_icon(s),
            "effect2": rune_set_effect(s, 2),
            "effect4": rune_set_effect(s, 4),
            "raw": s
        }
    return m


# ----------------------------
# Scaling / coefficient parsing
# ----------------------------
RE_ATK_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%[^%\n]{0,40}(?:attack power|atk)", re.I)
RE_HP_PCT  = re.compile(r"(\d+(?:\.\d+)?)\s*%[^%\n]{0,40}(?:max hp|maximum hp|hp)", re.I)
RE_DEF_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%[^%\n]{0,40}(?:defense|def)", re.I)

RE_BASE_ON_HP  = re.compile(r"(based on|scales with)[^.\n]{0,60}(max hp|hp)", re.I)
RE_BASE_ON_DEF = re.compile(r"(based on|scales with)[^.\n]{0,60}(defense|def)", re.I)

RE_HEALING     = re.compile(r"(heal|healing|restore hp|회복|치유|hp를 회복)", re.I)
RE_SHIELD      = re.compile(r"(shield|보호막|실드)", re.I)
RE_TAUNT       = re.compile(r"(taunt|도발)", re.I)
RE_DEBUFF      = re.compile(r"(debuff|디버프|def down|방깎|취약|받는 피해 증가)", re.I)
RE_DOT         = re.compile(r"(damage over time|dot|지속 피해|도트|bleed|burn|poison)", re.I)
RE_EXTRA       = re.compile(r"(extra attack|추가 공격|추가타)", re.I)
RE_BASIC       = re.compile(r"(basic attack|일반 공격|기본 공격)", re.I)
RE_CRIT        = re.compile(r"(critical|치명|crit rate|crit dmg|crit damage)", re.I)

def parse_scaling(texts: List[str]) -> Dict[str, float]:
    """
    스킬 설명에서 계수(%)를 모아서 ATK/HP/DEF 가중치 추정.
    - 'Deals 120% attack power' 같은 문구를 강하게 반영
    - 'based on max hp' 같은 문구는 계수 없이도 HP 스케일링으로 가중치 부여
    """
    w = {"atk": 0.0, "hp": 0.0, "def": 0.0}
    for t in texts:
        tl = t.lower()

        for m in RE_ATK_PCT.finditer(tl):
            w["atk"] += float(m.group(1))
        for m in RE_HP_PCT.finditer(tl):
            w["hp"] += float(m.group(1))
        for m in RE_DEF_PCT.finditer(tl):
            w["def"] += float(m.group(1))

        if RE_BASE_ON_HP.search(tl):
            w["hp"] += 80.0
        if RE_BASE_ON_DEF.search(tl):
            w["def"] += 80.0

    return w

def dominant_scaling(w: Dict[str, float]) -> str:
    best = max(w.items(), key=lambda kv: kv[1])
    if best[1] < 50.0:
        return "atk"
    return best[0]

def is_hybrid_healer(texts: List[str], scaling: Dict[str, float]) -> bool:
    """
    힐러인데 공격 계수 누적이 충분히 크면 하이브리드로 취급.
    기준(180%)은 운영하면서 조정 가능.
    """
    has_heal = any(RE_HEALING.search(t) for t in texts)
    atk_power = scaling.get("atk", 0.0)
    return bool(has_heal and atk_power >= 180.0)


# ----------------------------
# Archetype detection (heuristic + scaling override)
# ----------------------------
def classify(ch: Dict[str, Any], texts: List[str], scaling: Dict[str, float]) -> str:
    cls = str(pick_first(ch, ["class", "job", "roleClass", "type", "role"], "")).lower()
    blob = " ".join(t.lower() for t in texts)

    if "healer" in cls or RE_HEALING.search(blob):
        return "healer"
    if "defender" in cls or "tank" in cls or RE_SHIELD.search(blob) or RE_TAUNT.search(blob):
        return "tank"

    if RE_DOT.search(blob):
        return "dot"
    if RE_EXTRA.search(blob):
        return "extra"
    if RE_DEBUFF.search(blob):
        return "debuffer"

    dom = dominant_scaling(scaling)
    if dom == "hp":
        return "hp_dps"
    if dom == "def":
        return "def_dps"

    if RE_BASIC.search(blob):
        return "basic_dps"
    if RE_CRIT.search(blob):
        return "crit_dps"
    return "dps"


# ----------------------------
# Rune set preference (세트명은 runes.js와 일치해야 함)
# ----------------------------
SET_PREF = {
    "basic_dps": ("Alpha", "Beth"),
    "crit_dps":  ("Beth", "Alpha"),
    "dps":       ("Alpha", "Beth"),
    "hp_dps":    ("Alpha", "Zahn"),
    "def_dps":   ("Alpha", "Zahn"),
    "dot":       ("Gimel", "Beth"),
    "extra":     ("Hert", "Beth"),
    "debuffer":  ("Giants", "Beth"),
    "healer":    ("Daleth", "Zahn"),
    "tank":      ("Shattered Foundation", "Zahn"),
}

def choose_sets(archetype: str, available: List[str]) -> Tuple[str, str]:
    p, s = SET_PREF.get(archetype, ("Alpha", "Beth"))
    if p not in available:
        p = "Alpha" if "Alpha" in available else (available[0] if available else p)
    if s not in available or s == p:
        s = "Beth" if "Beth" in available and "Beth" != p else (available[1] if len(available) > 1 else p)
    return p, s


# ----------------------------
# Main stats / substats
# ----------------------------
def main_stats_by_archetype(archetype: str, elem: str) -> Dict[str, str]:
    if archetype == "healer":
        return {"4": "Healing Effectiveness %", "5": "HP %", "6": "HP %"}
    if archetype == "tank":
        return {"4": "HP %", "5": "Defense %", "6": "HP %"}
    if archetype == "debuffer":
        return {"4": "Attack Penetration %", "5": "Attack %", "6": "Attack %"}
    if archetype == "dot":
        return {"4": "Attack %", "5": f"{elem} Attribute Damage %" if elem else "Attack %", "6": "Attack %"}
    if archetype == "extra":
        return {"4": "Attack %", "5": f"{elem} Attribute Damage %" if elem else "Attack %", "6": "Attack %"}
    if archetype == "hp_dps":
        return {"4": "Critical Rate %", "5": f"{elem} Attribute Damage %" if elem else "HP %", "6": "HP %"}
    if archetype == "def_dps":
        return {"4": "Critical Rate %", "5": f"{elem} Attribute Damage %" if elem else "Defense %", "6": "Defense %"}
    return {"4": "Critical Rate %", "5": f"{elem} Attribute Damage %" if elem else "Attack %", "6": "Attack %"}

def substats_priority(archetype: str) -> List[str]:
    if archetype == "healer":
        return ["Healing Effectiveness %", "HP %", "Defense %", "Energy-related (if exists)"]
    if archetype == "tank":
        return ["HP %", "Defense %", "Damage Reduction (if exists)", "Energy-related (if exists)"]
    if archetype == "debuffer":
        return ["Attack %", "Attack Penetration", "Critical Rate %", "Energy-related (if exists)"]
    if archetype == "dot":
        return ["Attack %", "Attack Penetration", "Critical Rate % (optional)", "Energy-related (if exists)"]
    if archetype == "extra":
        return ["Attack %", "Critical Rate %", "Critical Damage %", "Attack Penetration"]
    if archetype == "hp_dps":
        return ["HP %", "Critical Rate %", "Critical Damage %", "Attack Penetration"]
    if archetype == "def_dps":
        return ["Defense %", "Critical Rate %", "Critical Damage %", "Attack Penetration"]
    return ["Critical Rate %", "Critical Damage %", "Attack %", "Attack Penetration"]


# ----------------------------
# Recommendation builder
# ----------------------------
def build_one(ch: Dict[str, Any], key: str, rune_sets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    texts = to_text_list(ch)
    scaling = parse_scaling(texts)
    arche = classify(ch, texts, scaling)
    elem = element_of(ch)

    available = list(rune_sets.keys())
    s4, s2 = choose_sets(arche, available)

    base = {
        "key": key,
        "archetype": arche,
        "scaling": {
            "weights": scaling,
            "dominant": dominant_scaling(scaling)
        },
        "builds": [
            {
                "id": "default",
                "title": "추천(기본)",
                "sets": {
                    "primary": {"name": s4, "pieces": 4},
                    "secondary": {"name": s2, "pieces": 2}
                },
                "main_stats": {
                    "1": "HP (Flat)",
                    "2": "Attack (Flat)",
                    "3": "Defense (Flat)",
                    **main_stats_by_archetype(arche, elem),
                },
                "substats_priority": substats_priority(arche),
                "notes": [
                    "Slot 5는 딜러 계열에서 캐릭터 속성과 동일한 속성 피해 %를 우선.",
                    "세트 제한(길드레이드 전용/클래스 제한)이 있으면 UI에서 경고 표시 권장."
                ]
            }
        ]
    }

    # 하이브리드: 힐러인데 공격 계수가 뚜렷하면 “서브 딜” 탭 추가
    if arche == "healer" and is_hybrid_healer(texts, scaling):
        s4b, s2b = choose_sets("crit_dps", available)
        base["builds"].append(
            {
                "id": "sub_dps",
                "title": "대체안(서브 딜)",
                "sets": {
                    "primary": {"name": s4b, "pieces": 4},
                    "secondary": {"name": s2b, "pieces": 2}
                },
                "main_stats": {
                    "1": "HP (Flat)",
                    "2": "Attack (Flat)",
                    "3": "Defense (Flat)",
                    "4": "Critical Rate %",
                    "5": f"{elem} Attribute Damage %" if elem else "Attack %",
                    "6": "Attack %"
                },
                "substats_priority": ["Critical Rate %", "Critical Damage %", "Attack %", "Attack Penetration"],
                "notes": [
                    "힐량이 부족해지면 기본(지원) 빌드를 우선.",
                    "공격 스킬 계수(attack power%)가 높아 하이브리드로 분기됨."
                ]
            }
        )

    # 세트 메타(아이콘/효과) 붙이기
    for b in base["builds"]:
        for which in ["primary", "secondary"]:
            name = b["sets"][which]["name"]
            meta = rune_sets.get(name, {})
            b["sets"][which]["icon"] = meta.get("icon")
            b["sets"][which]["effect2"] = meta.get("effect2")
            b["sets"][which]["effect4"] = meta.get("effect4")

    return base


def apply_overrides(items: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(OVERRIDES_JSON):
        return items

    try:
        ov = safe_read_json(OVERRIDES_JSON)
        overrides = ov.get("overrides", {}) if isinstance(ov, dict) else {}
    except Exception:
        return items

    if not isinstance(overrides, dict):
        return items

    for k, patch in overrides.items():
        if k not in items:
            continue
        items[k] = deep_merge(items[k], patch)

    return items


def main():
    if not os.path.isdir(CHAR_DIR):
        raise FileNotFoundError(f"characters_ko not found: {CHAR_DIR}")

    ensure_runes_export()
    runes_data = safe_read_json(RUNES_EXPORT)
    rune_sets_list = normalize_runes_export(runes_data)
    rune_sets = build_rune_sets_map(rune_sets_list)

    items: Dict[str, Any] = {}

    files = sorted(glob.glob(os.path.join(CHAR_DIR, "*.json")))
    for fp in files:
        ch = safe_read_json(fp)
        key = pick_first(ch, ["id", "slug", "key", "code", "nameId"], None)
        if not key:
            key = os.path.splitext(os.path.basename(fp))[0]
        key = str(key)
        items[key] = build_one(ch, key, rune_sets)

    items = apply_overrides(items)

    out = {
        "generated_from": {
            "characters_dir": "public/data/zone-nova/characters_ko",
            "runes_export": "public/data/zone-nova/runes_export.json",
            "overrides": "public/data/zone-nova/rune_overrides.json"
        },
        "items": items
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] wrote {os.path.relpath(OUT_JSON, ROOT)} (characters={len(files)})")


if __name__ == "__main__":
    main()
