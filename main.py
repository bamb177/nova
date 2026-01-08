import os
import json
import re
from datetime import datetime, timezone
from flask import Flask, jsonify, redirect, render_template

APP_TITLE = os.getenv("APP_TITLE", "Nova")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "public", "data", "zone-nova")
CHAR_KO_DIR = os.path.join(DATA_DIR, "characters_ko")

# ✅ 필수 유지
OVERRIDE_NAMES = os.path.join(DATA_DIR, "overrides_names.json")
OVERRIDE_FACTIONS = os.path.join(DATA_DIR, "overrides_factions.json")

# ✅ 룬 데이터(추천용)
RUNES_JS = os.path.join(DATA_DIR, "runes.js")
RUNE_OVERRIDES = os.path.join(DATA_DIR, "rune_overrides.json")

# ✅ 이미지 경로(사용자 제공 경로/파일명)
CHAR_IMG_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters")
ELEM_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "element")
CLASS_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "classes")
RUNE_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "runes")

VALID_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

ELEMENT_RENAME = {"Fire": "Blaze", "Wind": "Storm", "Ice": "Frost"}

app = Flask(__name__, static_folder="public", static_url_path="")

CACHE = {
    "chars": [],
    "details": {},
    "last_refresh": None,
    "error": None,
    "runes_db": None,
    "rune_overrides": None,
    "rune_icon_map": None,
    "source": {
        "characters": "public/data/zone-nova/characters_ko/*.json",
        "overrides_names": "public/data/zone-nova/overrides_names.json",
        "overrides_factions": "public/data/zone-nova/overrides_factions.json",
        "runes_js": "public/data/zone-nova/runes.js",
        "rune_overrides": "public/data/zone-nova/rune_overrides.json",
        "runes_icons": "public/images/games/zone-nova/runes/*",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def safe_load_json(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_read_text(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def normalize_char_name(name: str) -> str:
    name = (name or "").replace("’", "'").strip()
    return " ".join(name.split())


def normalize_element(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return "-"
    s2 = s[:1].upper() + s[1:].lower()
    return ELEMENT_RENAME.get(s2, s2)


def load_overrides() -> tuple[dict, dict]:
    names = safe_load_json(OVERRIDE_NAMES)
    factions = safe_load_json(OVERRIDE_FACTIONS)
    return (names if isinstance(names, dict) else {}), (factions if isinstance(factions, dict) else {})


def build_character_image_map(folder: str) -> dict:
    m = {}
    if not os.path.isdir(folder):
        return m

    for fn in os.listdir(folder):
        ext = os.path.splitext(fn)[1].lower()
        if ext not in VALID_IMG_EXT:
            continue

        base = os.path.splitext(fn)[0]
        base_low = base.lower()

        keys = set()
        keys.add(base_low)
        keys.add(slug_id(base))

        compact = re.sub(r"[\s\-_]+", "", base_low)
        if compact:
            keys.add(compact)
            keys.add(slug_id(compact))

        stripped = re.sub(r"^[0-9]+[_\-\s]*", "", base_low).strip()
        if stripped:
            keys.add(stripped)
            keys.add(slug_id(stripped))

        for k in keys:
            if k and k not in m:
                m[k] = fn

    return m


def candidate_image_keys(cid: str, raw_name: str, display_name: str, image_hint: str | None) -> list[str]:
    out = []

    def add(x: str):
        x = (x or "").strip()
        if not x:
            return
        out.append(x.lower())
        out.append(slug_id(x))
        out.append(re.sub(r"[\s\-_]+", "", x.lower()))
        out.append(slug_id(re.sub(r"[\s\-_]+", "", x)))

    add(image_hint or "")
    add(raw_name or "")
    add(cid or "")
    add(display_name or "")

    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def find_file_by_stem(folder: str, stem: str) -> str | None:
    if not os.path.isdir(folder):
        return None
    target = (stem or "").strip().lower()
    if not target:
        return None

    for fn in os.listdir(folder):
        base, ext = os.path.splitext(fn)
        if ext.lower() in VALID_IMG_EXT and base.lower() == target:
            return fn
    return None


def element_icon_url(element: str) -> str | None:
    if not element or element == "-":
        return None
    fn = find_file_by_stem(ELEM_ICON_DIR, element)
    if fn:
        return f"/images/games/zone-nova/element/{fn}"
    return None


def class_icon_url(cls: str) -> str | None:
    if not cls or cls == "-":
        return None
    cls_clean = str(cls).strip()
    if not cls_clean:
        return None
    fn = find_file_by_stem(CLASS_ICON_DIR, cls_clean)
    if fn:
        return f"/images/games/zone-nova/classes/{fn}"
    return None


# =========================
# Rune DB + Icon resolver
# =========================

FALLBACK_RUNES = [
    {"name": "Alpha", "twoPiece": "Attack Power +8%", "fourPiece": "Basic Attack Damage +30%"},
    {"name": "Beth", "twoPiece": "Critical Hit Rate +6%", "fourPiece": "When HP >80%: Critical Hit Damage +24%"},
    {"name": "Zahn", "twoPiece": "HP +8%", "fourPiece": "After Ultimate: Take 5% less damage (10s)"},
    {"name": "Shattered Foundation", "twoPiece": "Defense +12%", "fourPiece": "Shield Effectiveness +20%"},
    {"name": "Daleth", "twoPiece": "Healing Effectiveness +10%", "fourPiece": "Battle Start: Gain 1 Energy immediately"},
    {"name": "Epsilon", "twoPiece": "Attack Power +8%", "fourPiece": "After ultimate, team damage +10% (10s)", "note": "Same passive effect cannot stack"},
    {"name": "Hert", "twoPiece": "Extra Attack damage +20%", "fourPiece": "After dealing Extra Attack damage, Critical Hit Rate +15% (10s)", "note": "Guild raid only"},
    {"name": "Gimel", "twoPiece": "Continuous damage +20%", "fourPiece": "After dealing continuous damage, own attack power +2% (stacks up to 10, 5s)", "note": "Guild raid only"},
    {"name": "Giants", "twoPiece": "Attack power +8%", "fourPiece": "Debuffer only: after casting ultimate, targets take 10% increased damage (5s)", "note": "Guild raid only / Same passive effect cannot stack", "classRestriction": ["Debuffer"]},
    {"name": "Tide", "twoPiece": "Defense +12%", "fourPiece": "Within 10s after combat starts, team's energy gain efficiency +30%", "note": "Guild raid only / Does not stack / Daleth 4-piece team effect disabled", "teamConflict": ["Daleth:4"]},
]


def _strip_js_comments(s: str) -> str:
    s = re.sub(r"//.*?$", "", s, flags=re.M)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return s


def _extract_js_literal(s: str) -> str | None:
    if not s:
        return None
    s = _strip_js_comments(s).strip()

    m = re.search(r"export\s+default\s+(\[.*\]|\{.*\})\s*;?\s*$", s, flags=re.S)
    if m:
        return m.group(1).strip()

    m = re.search(r"module\.exports\s*=\s*(\[.*\]|\{.*\})\s*;?\s*$", s, flags=re.S)
    if m:
        return m.group(1).strip()

    m = re.search(r"\bconst\s+\w+\s*=\s*(\[.*\]|\{.*\})\s*;?\s*$", s, flags=re.S)
    if m:
        return m.group(1).strip()

    i, j = s.find("["), s.rfind("]")
    if i != -1 and j != -1 and j > i:
        return s[i:j+1].strip()

    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        return s[i:j+1].strip()

    return None


def _json_friendly(js: str) -> str:
    s = js.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing comma
    s = re.sub(r"\bundefined\b", "null", s)
    s = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1"\2"\3:', s)
    s = re.sub(r"'", '"', s)
    return s


def _norm_key(s: str) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"[\s\-_]+", "", t)
    return t


def load_rune_icon_map(force: bool = False) -> dict:
    if CACHE["rune_icon_map"] is not None and not force:
        return CACHE["rune_icon_map"]

    m = {}
    if os.path.isdir(RUNE_ICON_DIR):
        for fn in os.listdir(RUNE_ICON_DIR):
            base, ext = os.path.splitext(fn)
            if ext.lower() not in VALID_IMG_EXT:
                continue
            m[_norm_key(base)] = fn
            m[_norm_key(fn)] = fn
    CACHE["rune_icon_map"] = m
    return m


def _resolve_rune_icon_path(icon_hint: str | None, set_name: str) -> str | None:
    # 1) runes.js가 준 파일명/경로(icon_hint)
    # 2) 실제 폴더 스캔 결과(대소문자/하이픈/공백 차이 보정)
    icon_map = load_rune_icon_map()

    if isinstance(icon_hint, str) and icon_hint.strip():
        hint = icon_hint.strip().lstrip("/")
        hint_base = os.path.basename(hint)
        stem, ext = os.path.splitext(hint_base)

        hit = icon_map.get(_norm_key(hint_base))
        if hit:
            return f"/images/games/zone-nova/runes/{hit}"

        hit = icon_map.get(_norm_key(stem))
        if hit:
            return f"/images/games/zone-nova/runes/{hit}"

        if ext.lower() in VALID_IMG_EXT:
            return f"/images/games/zone-nova/runes/{hint_base}"

    cand = [
        set_name,
        set_name.replace(" ", "-"),
        set_name.replace(" ", "_"),
        _norm_key(set_name),
    ]
    for c in cand:
        hit = icon_map.get(_norm_key(c))
        if hit:
            return f"/images/games/zone-nova/runes/{hit}"

    return None


def load_rune_overrides(force: bool = False) -> dict:
    if CACHE["rune_overrides"] is not None and not force:
        return CACHE["rune_overrides"]

    data = safe_load_json(RUNE_OVERRIDES)
    CACHE["rune_overrides"] = data if isinstance(data, dict) else {}
    return CACHE["rune_overrides"]


def load_runes_db(force: bool = False) -> list[dict]:
    if CACHE["runes_db"] is not None and not force:
        return CACHE["runes_db"]

    raw = safe_read_text(RUNES_JS)
    runes = None

    if raw:
        lit = _extract_js_literal(raw)
        if lit:
            try:
                runes = json.loads(lit)
            except Exception:
                try:
                    runes = json.loads(_json_friendly(lit))
                except Exception:
                    runes = None

    if not isinstance(runes, list):
        runes = FALLBACK_RUNES

    norm = []
    for r in runes:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or r.get("title") or "").strip()
        if not name:
            continue

        icon_hint = r.get("icon") or r.get("image") or r.get("img") or r.get("jpg") or r.get("file")
        icon = _resolve_rune_icon_path(icon_hint if isinstance(icon_hint, str) else None, name)

        norm.append({
            "name": name,
            "twoPiece": r.get("twoPiece") or r.get("two_piece") or r.get("2pc") or r.get("two") or "",
            "fourPiece": r.get("fourPiece") or r.get("four_piece") or r.get("4pc") or r.get("four") or "",
            "note": r.get("note") or "",
            "classRestriction": r.get("classRestriction") or r.get("class_restriction") or [],
            "teamConflict": r.get("teamConflict") or r.get("team_conflict") or [],
            "icon": icon,
        })

    CACHE["runes_db"] = norm
    return norm


def rune_icon_for(set_name: str) -> str | None:
    if not set_name:
        return None
    db = load_runes_db()
    hit = next((x for x in db if x.get("name") == set_name), None)
    if hit and hit.get("icon"):
        return hit["icon"]
    return _resolve_rune_icon_path(None, set_name)


# =========================
# Rune Recommendation Engine
# =========================

_KW_HEAL = ["heal", "healing", "restore", "recovery", "회복", "치유", "힐"]
_KW_SHIELD = ["shield", "barrier", "보호막", "실드"]
_KW_DOT = ["continuous damage", "dot", "burn", "bleed", "poison", "지속", "지속 피해", "도트"]
_KW_EXTRA = ["extra attack", "follow-up", "추가 공격", "추격", "연속 공격"]
_KW_ULT = ["ultimate", "ult", "궁극기", "필살기"]


def _collect_texts(x) -> list[str]:
    out: list[str] = []

    def walk(v):
        if v is None:
            return
        if isinstance(v, str):
            t = v.strip()
            if t:
                out.append(t)
            return
        if isinstance(v, (int, float, bool)):
            return
        if isinstance(v, list):
            for it in v:
                walk(it)
            return
        if isinstance(v, dict):
            for _, vv in v.items():
                walk(vv)
            return

    walk(x)
    seen = set()
    uniq = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _pct_hits_with_examples(text: str, keys: list[str]) -> list[tuple[float, str]]:
    hits: list[tuple[float, str]] = []
    t = text.lower()

    for k in keys:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*[^%\n]{0,24}\b" + re.escape(k) + r"\b", t):
            try:
                pct = float(m.group(1))
                hits.append((pct, text.strip()[:140]))
            except Exception:
                pass

    for k in keys:
        for m in re.finditer(re.escape(k) + r"\s*의\s*(\d+(?:\.\d+)?)\s*%", text):
            try:
                pct = float(m.group(1))
                hits.append((pct, text.strip()[:140]))
            except Exception:
                pass

    return hits


def _detect_profile(detail: dict, base: dict) -> dict:
    texts = []
    if isinstance(detail, dict):
        texts.extend(_collect_texts(detail.get("skills")))
        texts.extend(_collect_texts(detail.get("teamSkill") or detail.get("team_skill") or detail.get("team")))
        texts.extend(_collect_texts(detail.get("awakenings")))
        texts.extend(_collect_texts(detail.get("memoryCard") or detail.get("memory_card")))
    texts = [t for t in texts if isinstance(t, str)]

    atk_hits, hp_hits, def_hits = [], [], []
    heal_cnt = shield_cnt = dot_cnt = extra_cnt = ult_cnt = 0

    for t in texts:
        atk_hits += _pct_hits_with_examples(t, ["attack power", "atk", "공격력"])
        hp_hits += _pct_hits_with_examples(t, ["max hp", "hp", "체력", "생명"])
        def_hits += _pct_hits_with_examples(t, ["defense", "def", "방어력"])

        tl = t.lower()
        if any(k in tl for k in _KW_HEAL):
            heal_cnt += 1
        if any(k in tl for k in _KW_SHIELD):
            shield_cnt += 1
        if any(k in tl for k in _KW_DOT):
            dot_cnt += 1
        if any(k in tl for k in _KW_EXTRA):
            extra_cnt += 1
        if any(k in tl for k in _KW_ULT):
            ult_cnt += 1

    def score(hits: list[tuple[float, str]]) -> float:
        if not hits:
            return 0.0
        avg = sum(p for p, _ in hits) / len(hits)
        return len(hits) * 10.0 + avg

    atk_s = score(atk_hits)
    hp_s = score(hp_hits)
    def_s = score(def_hits)

    best = max(atk_s, hp_s, def_s)
    if best <= 0:
        scaling = "MIX"
    else:
        scaling = "ATK" if best == atk_s else ("HP" if best == hp_s else "DEF")

    cls = str(base.get("class") or "-")
    role = str(base.get("role") or "-")

    cls_l = cls.lower()
    role_l = role.lower()

    if cls_l == "healer" or role_l == "healer":
        archetype = "healer"
    elif cls_l == "guardian" or role_l == "tank":
        archetype = "tank"
    elif cls_l == "debuffer" or role_l == "debuffer":
        archetype = "debuffer"
    else:
        archetype = "dps"

    if archetype == "tank" and heal_cnt > 0 and shield_cnt == 0:
        archetype = "healer"

    healer_hybrid = bool(archetype == "healer" and atk_s >= 15.0 and (atk_s >= hp_s or atk_s >= def_s))

    def top_examples(hits):
        return [ex for _, ex in hits[:2]]

    return {
        "scaling": scaling,
        "atk_score": atk_s,
        "hp_score": hp_s,
        "def_score": def_s,
        "atk_examples": top_examples(atk_hits),
        "hp_examples": top_examples(hp_hits),
        "def_examples": top_examples(def_hits),
        "heal_cnt": heal_cnt,
        "shield_cnt": shield_cnt,
        "dot_cnt": dot_cnt,
        "extra_cnt": extra_cnt,
        "ult_cnt": ult_cnt,
        "archetype": archetype,
        "healer_hybrid": healer_hybrid,
        "class": cls,
        "role": role,
    }


def _element_damage_label(element: str) -> str:
    e = normalize_element(element or "-")
    if e in ("Storm", "Blaze", "Frost", "Holy", "Chaos"):
        return f"{e} Attribute Damage (%)"
    return "Element Attribute Damage (%)"


def _slot_plan_for(archetype: str, scaling: str, element: str) -> dict:
    plan = {
        "1": ["HP (Flat Value)"],
        "2": ["Attack (Flat Value)"],
        "3": ["Defense (Flat Value)"],
        "4": [],
        "5": [],
        "6": [],
    }

    if archetype == "healer":
        plan["4"] = ["Healing Effectiveness (%)", "HP (%)", "Defense (%)"]
        plan["5"] = ["HP (%)", "Defense (%)"]
        plan["6"] = ["HP (%)", "Defense (%)"]
        return plan

    if archetype == "tank":
        if scaling == "DEF":
            plan["4"] = ["Defense (%)", "HP (%)"]
            plan["5"] = ["Defense (%)", "HP (%)"]
            plan["6"] = ["Defense (%)", "HP (%)"]
        else:
            plan["4"] = ["HP (%)", "Defense (%)"]
            plan["5"] = ["HP (%)", "Defense (%)"]
            plan["6"] = ["HP (%)", "Defense (%)"]
        return plan

    plan["4"] = ["Critical Rate (%)", "Attack Penetration (%)", "Critical Damage (%)", "Attack (%)"]
    plan["5"] = [_element_damage_label(element), "Attack (%)", "HP (%)", "Defense (%)"]
    plan["6"] = ["Attack (%)", "HP (%)", "Defense (%)"]
    return plan


def _substats_for(archetype: str, scaling: str, healer_hybrid: bool) -> list[str]:
    if archetype == "healer":
        base = ["Healing Effectiveness (%)", "HP (%)", "Defense (%)", "Flat HP / Flat DEF"]
        if healer_hybrid:
            base.insert(3, "Attack (%) (하이브리드일 때)")
        return base
    if archetype == "tank":
        return ["HP (%)", "Defense (%)", "Flat HP / Flat DEF", "Damage Reduction / RES (존재 시)"]
    out = ["Critical Rate (%)", "Critical Damage (%)", "Attack (%)", "Attack Penetration (%)", "Flat Attack", "HP (%) / Defense (%) (생존)"]
    if scaling in ("HP", "DEF"):
        out.insert(2, f"{scaling} (%) (스킬 스케일링 기반)")
    return out


def _pick_sets(profile: dict, base: dict) -> tuple[list[dict], list[list[dict]], list[str]]:
    archetype = profile["archetype"]
    dot = profile["dot_cnt"] > 0
    extra = profile["extra_cnt"] > 0
    shield = profile["shield_cnt"] > 0
    ult = profile["ult_cnt"] > 0

    reasons = []
    primary: list[dict] = []
    alternates: list[list[dict]] = []

    if archetype == "tank":
        if shield:
            primary = [{"set": "Shattered Foundation", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
            reasons.append("보호막/생존 키워드가 감지되어, 탱커 생존(방어/보호막) 중심 세트를 우선 추천했습니다.")
        else:
            primary = [{"set": "Zahn", "pieces": 4}, {"set": "Shattered Foundation", "pieces": 2}]
            reasons.append("탱커로 분류되어, 생존/피해 경감 중심 세트를 우선 추천했습니다.")
        return primary, alternates, reasons

    if archetype == "healer":
        primary = [{"set": "Daleth", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
        reasons.append("힐/회복 키워드 또는 힐러 클래스 기반으로, 힐 효율 및 안정성 중심 세트를 추천했습니다.")
        if profile.get("healer_hybrid"):
            alternates.append([{"set": "Daleth", "pieces": 4}, {"set": "Beth", "pieces": 2}])
            alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Daleth", "pieces": 2}])
            reasons.append("ATK 계수(스케일링)가 강하게 감지되어, 하이브리드(힐+딜) 대체안도 함께 제공합니다.")
        return primary, alternates, reasons

    cls = str(base.get("class") or "")
    if archetype == "debuffer" or cls.lower() == "debuffer":
        primary = [{"set": "Giants", "pieces": 4}, {"set": "Beth", "pieces": 2}]
        alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        reasons.append("디버퍼로 분류되어(클래스/키워드), 약화/팀 딜 증폭 계열 세트를 우선 추천했습니다.")
        return primary, alternates, reasons

    if dot:
        primary = [{"set": "Gimel", "pieces": 4}, {"set": "Beth", "pieces": 2}]
        alternates.append([{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        reasons.append("지속 피해(DoT) 관련 키워드가 감지되어, 지속 피해 강화 세트를 우선 추천했습니다.")
        return primary, alternates, reasons

    if extra:
        primary = [{"set": "Hert", "pieces": 4}, {"set": "Beth", "pieces": 2}]
        alternates.append([{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        reasons.append("추가 공격/추격(Extra Attack) 관련 키워드가 감지되어, 추가 공격 강화 세트를 우선 추천했습니다.")
        return primary, alternates, reasons

    primary = [{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}]
    reasons.append("명확한 특화 키워드가 없어서, 범용 딜러(치명/공격) 세트를 기본 추천했습니다.")
    if ult:
        alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        reasons.append("궁극기 관련 키워드가 감지되어, 궁극기/팀 시너지 대체안도 함께 제공합니다.")
    return primary, alternates, reasons


def _scaling_reason(profile: dict) -> list[str]:
    scaling = profile.get("scaling") or "MIX"
    ex = []
    if scaling == "ATK":
        ex.append("스킬 설명에서 '공격력(ATK) %' 계수가 반복적으로 감지되어 ATK 기반으로 판단했습니다.")
        if profile.get("atk_examples"):
            ex.append(f"예시: {profile['atk_examples'][0]}")
    elif scaling == "HP":
        ex.append("스킬 설명에서 'HP/최대 HP %' 계수가 감지되어 HP 스케일링 기반으로 판단했습니다.")
        if profile.get("hp_examples"):
            ex.append(f"예시: {profile['hp_examples'][0]}")
    elif scaling == "DEF":
        ex.append("스킬 설명에서 '방어력(DEF) %' 계수가 감지되어 DEF 스케일링 기반으로 판단했습니다.")
        if profile.get("def_examples"):
            ex.append(f"예시: {profile['def_examples'][0]}")
    else:
        ex.append("스킬 설명에서 특정 스탯(ATK/HP/DEF) 기반 계수가 명확히 감지되지 않아 MIX로 처리했습니다.")
    return ex


def recommend_runes(cid: str, base: dict, detail: dict) -> dict:
    overrides = load_rune_overrides()
    ov = overrides.get(cid)

    if isinstance(ov, dict) and isinstance(ov.get("builds"), list) and ov["builds"]:
        builds = []
        for b in ov["builds"]:
            if not isinstance(b, dict):
                continue
            sp = []
            for it in (b.get("setPlan") or []):
                if not isinstance(it, dict):
                    continue
                sname = str(it.get("set") or "").strip()
                if not sname:
                    continue
                sp.append({
                    "set": sname,
                    "pieces": int(it.get("pieces") or 0),
                    "icon": rune_icon_for(sname),
                })
            builds.append({
                "title": str(b.get("title") or "수동 지정"),
                "setPlan": sp,
                "slots": b.get("slots") or {},
                "substats": b.get("substats") or [],
                "notes": b.get("notes") or [],
                "rationale": b.get("rationale") or ["rune_overrides.json 수동 오버라이드가 적용되었습니다."],
            })
        return {
            "mode": "override",
            "profile": {"note": "rune_overrides.json 적용"},
            "builds": builds,
        }

    profile = _detect_profile(detail or {}, base or {})
    primary, alternates, set_reasons = _pick_sets(profile, base or {})

    archetype = profile.get("archetype") or "dps"
    scaling = profile.get("scaling") or "MIX"
    element = base.get("element") or "-"

    def mk_build(title: str, setplan: list[dict], extra_rationale: list[str]) -> dict:
        slots = _slot_plan_for(archetype, scaling, element)
        substats = _substats_for(archetype, scaling, profile.get("healer_hybrid", False))

        rationale = []
        rationale.append(f"분류: {archetype} (class={profile.get('class')}, role={profile.get('role')})")
        rationale += _scaling_reason(profile)
        rationale += set_reasons
        rationale += extra_rationale
        rationale.append("슬롯 메인스탯은 역할 및 스케일링(ATK/HP/DEF)에 맞춰 우선순위를 부여했습니다.")
        rationale.append("부옵 우선순위는 치명/공격(딜러), HP/DEF(탱커/힐러) 중심으로 정렬했습니다.")

        return {
            "title": title,
            "setPlan": [{"set": x["set"], "pieces": x["pieces"], "icon": rune_icon_for(x["set"])} for x in setplan],
            "slots": slots,
            "substats": substats,
            "notes": [],
            "rationale": rationale,
        }

    builds = [mk_build("추천(자동)", primary, [])]
    for idx, alt in enumerate(alternates[:3], start=1):
        builds.append(mk_build(f"대체안 {idx}", alt, ["상황/콘텐츠에 따라 세트 대체가 가능하도록 보조 빌드를 제공합니다."]))

    db = load_runes_db()

    def set_notes_for_build(b):
        notes = []
        for s in b.get("setPlan") or []:
            nm = s.get("set")
            rdb = next((x for x in db if x.get("name") == nm), None)
            if not rdb:
                continue
            cr = rdb.get("classRestriction") or []
            if cr:
                notes.append(f"{nm}: 클래스 제한({', '.join(map(str, cr))})")
            if rdb.get("note"):
                notes.append(f"{nm}: {rdb.get('note')}")
            if rdb.get("teamConflict"):
                notes.append(f"{nm}: 팀 세트 상충 주의({', '.join(map(str, rdb.get('teamConflict')))} )")
        b["notes"] = notes

    for b in builds:
        set_notes_for_build(b)

    return {"mode": "auto", "profile": profile, "builds": builds}


def rune_summary_for_list(cid: str, base: dict, detail: dict) -> dict | None:
    reco = recommend_runes(cid, base, detail)
    builds = reco.get("builds") or []
    if not builds:
        return None
    b0 = builds[0]
    return {"mode": reco.get("mode"), "sets": b0.get("setPlan") or []}


# =========================
# Load characters
# =========================

def load_all(force: bool = False) -> None:
    if CACHE["chars"] and not force:
        return

    CACHE["error"] = None
    CACHE["chars"] = []
    CACHE["details"] = {}

    try:
        if not os.path.isdir(CHAR_KO_DIR):
            raise RuntimeError(f"characters_ko 디렉터리 없음: {CHAR_KO_DIR}")

        overrides_names, overrides_factions = load_overrides()
        char_img_map = build_character_image_map(CHAR_IMG_DIR)

        chars = []
        details = {}

        files = [fn for fn in os.listdir(CHAR_KO_DIR) if fn.lower().endswith(".json")]
        files.sort()

        for fn in files:
            cid = slug_id(os.path.splitext(fn)[0])
            if not cid:
                continue

            path = os.path.join(CHAR_KO_DIR, fn)
            d = safe_load_json(path)
            if not isinstance(d, dict):
                continue

            details[cid] = d

            raw_name = normalize_char_name(d.get("name") or cid)
            display_name = overrides_names.get(raw_name, raw_name)

            rarity = (d.get("rarity") or "-").strip().upper()
            element = normalize_element(str(d.get("element") or "-"))

            raw_faction = str(d.get("faction") or "-").strip() or "-"
            faction = overrides_factions.get(raw_faction, raw_faction)

            cls = str(d.get("class") or "-").strip() or "-"
            role = str(d.get("role") or "-").strip() or "-"

            image_url = None
            image_hint = d.get("image")
            image_hint = image_hint.strip() if isinstance(image_hint, str) else None

            for k in candidate_image_keys(cid, raw_name, display_name, image_hint):
                real = char_img_map.get(k)
                if real:
                    image_url = f"/images/games/zone-nova/characters/{real}"
                    break

            elem_icon = element_icon_url(element)
            cls_icon = class_icon_url(cls)

            base = {
                "id": cid,
                "name": display_name,
                "raw_name": raw_name,
                "rarity": rarity,
                "element": element,
                "faction": faction,
                "class": cls,
                "role": role,
                "image": image_url,
                "element_icon": elem_icon,
                "class_icon": cls_icon,
            }

            try:
                base["runes"] = rune_summary_for_list(cid, base, d)
            except Exception:
                base["runes"] = None

            chars.append(base)

        rarity_order = {"SSR": 0, "SR": 1, "R": 2, "-": 9}
        chars.sort(key=lambda x: (rarity_order.get(x.get("rarity", "-"), 9), (x.get("name") or "").lower()))

        CACHE["chars"] = chars
        CACHE["details"] = details
        CACHE["last_refresh"] = now_iso()

    except Exception as e:
        CACHE["error"] = str(e)
        CACHE["last_refresh"] = now_iso()


# =========================
# Routes
# =========================

@app.get("/")
def home():
    return redirect("/ui/select")


@app.get("/refresh")
def refresh():
    load_all(force=True)
    CACHE["runes_db"] = None
    CACHE["rune_overrides"] = None
    CACHE["rune_icon_map"] = None
    return redirect("/ui/select")


@app.get("/meta")
def meta():
    load_all()
    return jsonify({
        "title": APP_TITLE,
        "characters_cached": len(CACHE["chars"]),
        "last_refresh": CACHE["last_refresh"],
        "error": CACHE["error"],
        "source": CACHE["source"],
        "paths": {
            "characters_ko_dir": CHAR_KO_DIR,
            "runes_js": RUNES_JS,
            "rune_overrides": RUNE_OVERRIDES,
            "runes_icon_dir": RUNE_ICON_DIR,
        }
    })


@app.get("/zones/zone-nova/runes")
def api_runes():
    db = load_runes_db()
    return jsonify({
        "count": len(db),
        "source": CACHE["source"]["runes_js"],
        "runes": db,
    })


@app.get("/zones/zone-nova/characters")
def api_chars():
    load_all()
    return jsonify({
        "count": len(CACHE["chars"]),
        "last_refresh": CACHE["last_refresh"],
        "source": CACHE["source"]["characters"],
        "error": CACHE["error"],
        "characters": CACHE["chars"],
    })


@app.get("/zones/zone-nova/characters/<cid>")
def api_char_detail(cid: str):
    load_all()
    cid2 = slug_id(cid)

    detail = CACHE["details"].get(cid2)
    if not isinstance(detail, dict):
        detail_path = os.path.join(CHAR_KO_DIR, f"{cid2}.json")
        detail = safe_load_json(detail_path)

    if not isinstance(detail, dict):
        return jsonify({"ok": False, "error": f"characters_ko json not found: {cid2}.json"}), 404

    base = next((c for c in CACHE["chars"] if c.get("id") == cid2), None)
    if not base:
        overrides_names, overrides_factions = load_overrides()

        raw_name = normalize_char_name(detail.get("name") or cid2)
        display_name = overrides_names.get(raw_name, raw_name)

        raw_faction = str(detail.get("faction") or "-").strip() or "-"
        faction = overrides_factions.get(raw_faction, raw_faction)

        element = normalize_element(str(detail.get("element") or "-"))
        cls = str(detail.get("class") or "-").strip() or "-"

        base = {
            "id": cid2,
            "name": display_name,
            "raw_name": raw_name,
            "rarity": (detail.get("rarity") or "-").strip().upper(),
            "element": element,
            "faction": faction,
            "class": cls,
            "role": str(detail.get("role") or "-").strip() or "-",
            "image": None,
            "element_icon": element_icon_url(element),
            "class_icon": class_icon_url(cls),
            "runes": None,
        }

    try:
        rune_reco = recommend_runes(cid2, base, detail)
    except Exception as e:
        rune_reco = {"mode": "error", "error": str(e), "builds": []}

    return jsonify({
        "ok": True,
        "id": cid2,
        "character": base,
        "detail": detail,
        "detail_source": f"public/data/zone-nova/characters_ko/{cid2}.json",
        "rune_reco": rune_reco,
    })


@app.get("/ui/select")
def ui_select():
    load_all()
    return render_template(
        "select.html",
        title=APP_TITLE,
        cache_count=len(CACHE["chars"]),
        last_refresh=CACHE["last_refresh"] or "N/A",
        error=CACHE["error"],
        chars_json=json.dumps(CACHE["chars"], ensure_ascii=False),
    )


@app.route("/runes")
def runes_page():
    load_all()
    return render_template(
        "runes.html",
        title="룬 정보",
        last_refresh=CACHE["last_refresh"] or "",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    debug = os.getenv("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
