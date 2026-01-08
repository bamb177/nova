import os, json, glob, subprocess, re
from typing import Dict, Any, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "public", "data", "zone-nova")
CHAR_DIR = os.path.join(DATA_DIR, "characters_ko")

RUNES_JS = os.path.join(DATA_DIR, "runes.js")
RUNES_EXPORT = os.path.join(DATA_DIR, "runes_export.json")
OUT_JSON = os.path.join(DATA_DIR, "runes_recommendations.json")


# ----------------------------
# Utilities
# ----------------------------
def safe_read_json(p: str) -> Dict[str, Any]:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def ensure_runes_export():
    if os.path.exists(RUNES_EXPORT):
        return
    # node script로 runes.js -> json 생성
    script = os.path.join(ROOT, "scripts", "export_runes_json.mjs")
    if not os.path.exists(script):
        raise FileNotFoundError("export_runes_json.mjs not found. Create it under scripts/.")
    subprocess.check_call(["node", script], cwd=ROOT)

def to_text_blob(obj: Any) -> str:
    # JSON 전체를 텍스트로 만들어 키워드 탐지
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s.lower()

def pick_first(d: Dict[str, Any], keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return default

def norm(s: str) -> str:
    return (s or "").strip()

# ----------------------------
# Archetype detection (heuristic)
# ----------------------------
KW = {
    "heal": [ "heal", "회복", "치유", "hp를 회복", "restore hp", "healing" ],
    "shield": [ "shield", "보호막", "실드" ],
    "taunt": [ "taunt", "도발" ],
    "reduce_dmg": [ "damage taken", "피해 감소", "받는 피해", "take less damage" ],
    "debuff": [ "debuff", "디버프", "방깎", "def down", "vulnerability", "받는 피해 증가", "취약" ],
    "dot": [ "continuous damage", "지속 피해", "도트", "damage over time", "bleed", "burn", "poison" ],
    "extra": [ "extra attack", "추가타", "추가 공격" ],
    "basic": [ "basic attack", "일반 공격", "기본 공격" ],
    "crit": [ "critical", "치명", "crit rate", "crit damage" ],
    "energy": [ "energy", "에너지", "gain 1 energy", "에너지 획득", "궁극기", "ultimate" ],
}

def has_any(blob: str, words: List[str]) -> bool:
    return any(w.lower() in blob for w in words)

def classify_character(ch: Dict[str, Any]) -> str:
    blob = to_text_blob(ch)

    # class 필드가 있으면 가중
    cls = str(pick_first(ch, ["class", "job", "roleClass", "type"], "")).lower()

    if "healer" in cls or has_any(blob, KW["heal"]):
        return "healer"
    if "defender" in cls or "tank" in cls or has_any(blob, KW["shield"]) or has_any(blob, KW["taunt"]) or has_any(blob, KW["reduce_dmg"]):
        return "tank"
    if "debuffer" in cls or has_any(blob, KW["debuff"]):
        # 도트/추가타가 더 뚜렷하면 그쪽 우선
        if has_any(blob, KW["dot"]):
            return "dot"
        return "debuffer"
    if has_any(blob, KW["dot"]):
        return "dot"
    if has_any(blob, KW["extra"]):
        return "extra"
    if has_any(blob, KW["basic"]):
        return "basic_dps"
    # 기본값은 딜러(치명 기반 가능)
    if has_any(blob, KW["crit"]):
        return "crit_dps"
    return "dps"

# ----------------------------
# Rune set selection rules
# (runes.js를 읽어도 되지만, 여기선 "세트 이름" 기준으로 매핑 후
# runes_export.json에서 실제 존재 여부만 검증)
# ----------------------------
SET_PREF = {
    "basic_dps": ("Alpha", "Beth"),
    "crit_dps": ("Beth", "Alpha"),
    "dps": ("Alpha", "Beth"),
    "dot": ("Gimel", "Beth"),
    "extra": ("Hert", "Beth"),
    "debuffer": ("Giants", "Beth"),
    "healer": ("Daleth", "Zahn"),
    "tank": ("Shattered Foundation", "Zahn"),
}

def find_set_names(runes_data: Any) -> List[str]:
    names = []
    if isinstance(runes_data, list):
        for x in runes_data:
            if isinstance(x, dict):
                n = x.get("name") or x.get("title") or x.get("setName")
                if n: names.append(str(n))
    elif isinstance(runes_data, dict):
        # 다양한 구조 대응
        for k, v in runes_data.items():
            if isinstance(v, dict):
                n = v.get("name") or v.get("title") or v.get("setName") or k
                names.append(str(n))
    return names

def choose_sets(archetype: str, available_sets: List[str]) -> Tuple[str, str]:
    primary, secondary = SET_PREF.get(archetype, ("Alpha", "Beth"))
    # 존재하지 않으면 fallback
    if primary not in available_sets:
        primary = "Alpha" if "Alpha" in available_sets else (available_sets[0] if available_sets else primary)
    if secondary not in available_sets or secondary == primary:
        secondary = "Beth" if "Beth" in available_sets and "Beth" != primary else (available_sets[1] if len(available_sets) > 1 else primary)
    return primary, secondary

# ----------------------------
# Main stats by slot (4/5/6 only matter)
# Position 1~3 are fixed (HP/ATK/DEF flat)
# ----------------------------
def element_of(ch: Dict[str, Any]) -> str:
    e = pick_first(ch, ["element", "attr", "attribute"], "")
    return str(e)

def main_stats(archetype: str, elem: str) -> Dict[str, str]:
    # slot 4: heal/crit/critdmg/pen/atk%/hp%/def%
    # slot 5: elem dmg% OR atk%/hp%/def%
    # slot 6: atk%/hp%/def%
    if archetype == "healer":
        return {"4": "Healing Effectiveness %", "5": "HP %", "6": "HP %"}
    if archetype == "tank":
        return {"4": "HP %", "5": "Defense %", "6": "HP %"}
    if archetype == "debuffer":
        return {"4": "Attack Penetration %", "5": "Attack %", "6": "Attack %"}
    if archetype in ("dot", "extra"):
        return {"4": "Attack %", "5": f"{elem} Attribute Damage %" if elem else "Attack %", "6": "Attack %"}
    # dps 계열
    if archetype in ("crit_dps", "basic_dps", "dps"):
        return {"4": "Critical Rate %" , "5": f"{elem} Attribute Damage %" if elem else "Attack %", "6": "Attack %"}
    return {"4": "Attack %", "5": f"{elem} Attribute Damage %" if elem else "Attack %", "6": "Attack %"}

def substats(archetype: str) -> List[str]:
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
    # dps
    return ["Critical Rate %", "Critical Damage %", "Attack %", "Attack Penetration"]

# ----------------------------
# Build recommendations
# ----------------------------
def recommend_for(ch: Dict[str, Any], available_sets: List[str]) -> Dict[str, Any]:
    arche = classify_character(ch)
    elem = element_of(ch)
    s4, s2 = choose_sets(arche, available_sets)

    return {
        "archetype": arche,
        "sets": {
            "primary": {"name": s4, "pieces": 4},
            "secondary": {"name": s2, "pieces": 2},
            "alt": [
                {"name": s2, "pieces": 4},
                {"name": s4, "pieces": 2},
            ]
        },
        "main_stats": {
            "1": "HP (Flat)",
            "2": "Attack (Flat)",
            "3": "Defense (Flat)",
            **main_stats(arche, elem),
        },
        "substats_priority": substats(arche),
        "notes": [
            "Slot 5 prefers elemental damage % matching the character element (for DPS builds).",
            "Some sets may be guild-raid-only or have class restrictions; enforce those in UI if needed."
        ]
    }

def main():
    if not os.path.isdir(CHAR_DIR):
        raise FileNotFoundError(f"characters_ko not found: {CHAR_DIR}")

    ensure_runes_export()
    runes_data = safe_read_json(RUNES_EXPORT)
    available_sets = find_set_names(runes_data)

    out: Dict[str, Any] = {"generated_from": {"characters_dir": "characters_ko", "runes_export": "runes_export.json"}, "items": {}}

    files = sorted(glob.glob(os.path.join(CHAR_DIR, "*.json")))
    for fp in files:
        ch = safe_read_json(fp)

        # id/slug/name 최대한 유연하게
        cid = pick_first(ch, ["id", "slug", "key", "code"], None)
        if not cid:
            cid = os.path.splitext(os.path.basename(fp))[0]

        out["items"][str(cid)] = recommend_for(ch, available_sets)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[OK] wrote {os.path.relpath(OUT_JSON, ROOT)} (characters={len(files)})")

if __name__ == "__main__":
    main()
