import os
import json
import re
import ast
from datetime import datetime, timezone
from typing import Optional, Any

from flask import Flask, jsonify, redirect, render_template, request
from collections import Counter

APP_TITLE = os.getenv("APP_TITLE", "Nova")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "public", "data", "zone-nova")
CHAR_KO_DIR = os.path.join(DATA_DIR, "characters_ko")

# ✅ name/faction override
OVERRIDE_NAMES = os.path.join(DATA_DIR, "overrides_names.json")
OVERRIDE_FACTIONS = os.path.join(DATA_DIR, "overrides_factions.json")

# ✅ runes
RUNES_JS = os.path.join(DATA_DIR, "runes.js")
RUNE_OVERRIDES = os.path.join(DATA_DIR, "rune_overrides.json")

# ✅ images
CHAR_IMG_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters")
ELEM_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "element")
CLASS_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "classes")
RUNE_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "runes")

VALID_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

ELEMENT_RENAME = {"Fire": "Blaze", "Wind": "Storm", "Ice": "Frost"}

app = Flask(__name__, static_folder="public", static_url_path="")

CACHE: dict[str, Any] = {
    "chars": [],
    "details": {},
    "last_refresh": None,
    "error": None,
    "runes_db": None,
    "rune_overrides": None,
    "rune_img_map": None,
}


# -------------------------
# Helpers: IO / normalize
# -------------------------

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


def safe_read_text(path: str) -> Optional[str]:
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


def find_file_by_stem(folder: str, stem: str) -> Optional[str]:
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


def element_icon_url(element: str) -> Optional[str]:
    if not element or element == "-":
        return None
    fn = find_file_by_stem(ELEM_ICON_DIR, element)
    return f"/images/games/zone-nova/element/{fn}" if fn else None


def class_icon_url(cls: str) -> Optional[str]:
    if not cls or cls == "-":
        return None
    fn = find_file_by_stem(CLASS_ICON_DIR, str(cls).strip())
    return f"/images/games/zone-nova/classes/{fn}" if fn else None


# -------------------------
# Character image mapping
# -------------------------

def build_character_image_map(folder: str) -> dict[str, str]:
    m: dict[str, str] = {}
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


def candidate_image_keys(cid: str, raw_name: str, display_name: str, image_hint: Optional[str]) -> list[str]:
    out: list[str] = []

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


# -------------------------
# Runes: parse runes.js + resolve icons
# -------------------------

# =========================
# Rune effect parsing (2pc/4pc text -> structured effects)
# =========================

def _parse_seconds(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*s", s.lower())
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

def _pct_from_text(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", s)
    if not m:
        return None
    try:
        return float(m.group(1)) / 100.0
    except Exception:
        return None

def parse_rune_effect_text(text: str) -> dict:
    """
    2pc/4pc 효과 문구를 '효과 벡터'로 변환.
    반환 예:
      {
        "mods": {"atk_pct":0.08, "crit_rate":0.06, "basic_dmg":0.30, ...},
        "cond": {"type":"hp_gt", "value":0.80} or {"type":"after_ultimate","dur":10},
        "target":"self|team",
        "notes":[...]
      }
    """
    out = {"mods": {}, "cond": None, "target": "self", "notes": []}
    if not isinstance(text, str) or not text.strip():
        return out

    t = text.strip()
    tl = t.lower()

    # --- condition parsing ---
    # When HP >80%: ...
    m = re.search(r"when\s+hp\s*([<>]=?)\s*(\d+(?:\.\d+)?)\s*%", tl)
    if m:
        op = m.group(1)
        val = float(m.group(2)) / 100.0
        out["cond"] = {"type": "hp_cond", "op": op, "value": val}
        # 조건 앞부분 제거 후 본문만 남겨 파싱 계속
        t = re.sub(r"(?i)when\s+hp\s*[<>]=?\s*\d+(?:\.\d+)?\s*%\s*:\s*", "", t).strip()
        tl = t.lower()

    # After ultimate / after dealing X
    if "after ultimate" in tl or "after ult" in tl or "궁극기" in t or "필살기" in t:
        dur = _parse_seconds(t)  # (10s)
        out["cond"] = {"type": "after_ultimate", "dur": dur}

    if "after dealing extra attack" in tl or "extra attack damage" in tl or "추가 공격" in t:
        dur = _parse_seconds(t)
        out["cond"] = {"type": "after_extra", "dur": dur}

    if "after dealing continuous damage" in tl or "continuous damage" in tl or "dot" in tl or "지속" in t:
        dur = _parse_seconds(t)
        out["cond"] = {"type": "after_dot", "dur": dur}

    if "battle start" in tl or "전투 시작" in t:
        out["cond"] = {"type": "battle_start", "dur": None}

    # stacking
    if "stacks up to" in tl or "stack" in tl or "중첩" in t:
        m2 = re.search(r"stacks\s+up\s+to\s+(\d+)", tl)
        if m2:
            out["notes"].append(f"stack_max={m2.group(1)}")

    # team target
    if "team" in tl or "파티" in t or "아군" in t:
        out["target"] = "team"

    # --- stat/damage mods parsing ---
    # Attack Power +8%
    if "attack power" in tl or "atk" in tl or "공격력" in t:
        p = _pct_from_text(t)
        if p is not None and ("attack power" in tl or "공격력" in t):
            out["mods"]["atk_pct"] = out["mods"].get("atk_pct", 0.0) + p

    # HP +8%
    if re.search(r"\bhp\b", tl) or "체력" in t or "생명력" in t:
        p = _pct_from_text(t)
        if p is not None and ("hp" in tl or "체력" in t or "생명력" in t):
            out["mods"]["hp_pct"] = out["mods"].get("hp_pct", 0.0) + p

    # Defense +12%
    if "defense" in tl or "def" in tl or "방어력" in t:
        p = _pct_from_text(t)
        if p is not None and ("defense" in tl or "방어력" in t):
            out["mods"]["def_pct"] = out["mods"].get("def_pct", 0.0) + p

    # Crit rate +6%
    if "critical hit rate" in tl or "crit rate" in tl or "치명타 확률" in t or "치명률" in t or "크리" in t:
        p = _pct_from_text(t)
        if p is not None:
            out["mods"]["crit_rate"] = out["mods"].get("crit_rate", 0.0) + p

    # Crit damage +24%
    if "critical hit damage" in tl or "crit damage" in tl or "치명타 피해" in t or "치피" in t or "크리피해" in t:
        p = _pct_from_text(t)
        if p is not None:
            out["mods"]["crit_dmg"] = out["mods"].get("crit_dmg", 0.0) + p

    # Basic Attack Damage +30%
    if "basic attack damage" in tl or "기본 공격" in t or "평타" in t:
        p = _pct_from_text(t)
        if p is not None and ("damage" in tl or "피해" in t):
            out["mods"]["basic_dmg"] = out["mods"].get("basic_dmg", 0.0) + p

    # Extra Attack damage +20%
    if "extra attack" in tl or "추가 공격" in t:
        p = _pct_from_text(t)
        if p is not None and ("damage" in tl or "피해" in t):
            out["mods"]["extra_dmg"] = out["mods"].get("extra_dmg", 0.0) + p

    # Continuous damage +20% (DOT)
    if "continuous damage" in tl or "dot" in tl or "지속" in t:
        p = _pct_from_text(t)
        if p is not None and ("damage" in tl or "피해" in t):
            out["mods"]["dot_dmg"] = out["mods"].get("dot_dmg", 0.0) + p

    # Team damage +10%
    if ("team" in tl and "damage" in tl) or ("파티" in t and "피해" in t):
        p = _pct_from_text(t)
        if p is not None:
            out["mods"]["team_dmg"] = out["mods"].get("team_dmg", 0.0) + p

    # Healing Effectiveness +10%
    if "healing effectiveness" in tl or "치유" in t or "회복" in t:
        p = _pct_from_text(t)
        if p is not None:
            out["mods"]["heal_eff"] = out["mods"].get("heal_eff", 0.0) + p

    # Shield Effectiveness +20%
    if "shield effectiveness" in tl or "보호막" in t or "실드" in t:
        p = _pct_from_text(t)
        if p is not None:
            out["mods"]["shield_eff"] = out["mods"].get("shield_eff", 0.0) + p

    # Energy gain at battle start
    if ("gain" in tl and "energy" in tl) or ("에너지" in t and ("획득" in t or "얻" in t)):
        out["mods"]["energy_start"] = 1.0

    return out

def rune_effects_enriched(rune_db: dict) -> dict:
    """
    rune_db_by_name() 결과에 2pc/4pc 파싱 결과를 붙인 사전 반환
    """
    out = {}
    for name, r in rune_db.items():
        two = parse_rune_effect_text(str(r.get("twoPiece") or ""))
        four = parse_rune_effect_text(str(r.get("fourPiece") or ""))
        out[name] = {**r, "_two": two, "_four": four}
    return out

def _strip_js_comments(s: str) -> str:
    s = re.sub(r"//.*?$", "", s, flags=re.M)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return s


def _extract_balanced(s: str, start: int) -> Optional[str]:
    """
    start 위치의 '{' 또는 '[' 에서 시작해 괄호를 밸런싱하며 리터럴을 추출한다.
    문자열/이스케이프를 고려한다.
    """
    if start < 0 or start >= len(s):
        return None
    opener = s[start]
    if opener not in "[{":
        return None
    closer = "]" if opener == "[" else "}"

    depth = 0
    i = start
    in_str = False
    quote = ""
    esc = False

    while i < len(s):
        ch = s[i]

        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
                quote = ""
            i += 1
            continue

        if ch in ("'", '"'):
            in_str = True
            quote = ch
            i += 1
            continue

        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
        i += 1

    return None


def _extract_js_literal(raw: str) -> Optional[str]:
    """
    runes.js에서 export default <literal or identifier> 형태를 최대한 복원
    """
    if not raw:
        return None

    s = _strip_js_comments(raw)

    # export default <literal or IDENT>
    m = re.search(r"export\s+default\s+([A-Za-z_][A-Za-z0-9_]*|\[|\{)", s)
    if not m:
        return None

    token = m.group(1)
    if token in ("[", "{"):
        start = m.start(1)
        return _extract_balanced(s, start)

    # export default IDENT;
    ident = token
    # const IDENT = <literal>;
    m2 = re.search(rf"\bconst\s+{re.escape(ident)}\s*=\s*(\[|\{{)", s)
    if m2:
        start = m2.start(1)
        return _extract_balanced(s, start)

    # let/var IDENT = <literal>;
    m2 = re.search(rf"\b(?:let|var)\s+{re.escape(ident)}\s*=\s*(\[|\{{)", s)
    if m2:
        start = m2.start(1)
        return _extract_balanced(s, start)

    return None


def _json_friendly(js: str) -> str:
    # JSON 파서 친화적으로 보정(마지막 시도용)
    s = js.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing comma
    s = re.sub(r"\bundefined\b", "null", s)
    # unquoted keys -> quoted keys
    s = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1"\2"\3:', s)
    # single quote -> double quote (best-effort)
    s = re.sub(r"'", r'"', s)
    return s


def _to_python_literal(js: str) -> str:
    """
    ast.literal_eval을 위한 Python 리터럴 변환(핵심: JS 키/값을 Python이 읽을 수 있게)
    """
    s = js.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing comma
    s = re.sub(r"\bnull\b", "None", s)
    s = re.sub(r"\btrue\b", "True", s, flags=re.I)
    s = re.sub(r"\bfalse\b", "False", s, flags=re.I)
    s = re.sub(r"\bundefined\b", "None", s)
    # unquoted keys -> quoted keys
    s = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1"\2"\3:', s)
    return s


def _norm_key(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\s\-_\.]+", "", s)
    s = re.sub(r"[^a-z0-9가-힣]", "", s)
    return s


def build_rune_image_map(folder: str) -> dict[str, str]:
    """
    public/images/games/zone-nova/runes 폴더를 스캔해서
    세트명 -> 실제 파일명으로 매핑(대소문자/공백/언더스코어/접미어 차이 흡수)
    """
    m: dict[str, str] = {}
    if not os.path.isdir(folder):
        return m

    for root, _, files in os.walk(folder):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in VALID_IMG_EXT:
                continue

            base = os.path.splitext(fn)[0]
            rel = os.path.relpath(os.path.join(root, fn), folder).replace("\\", "/")

            keys = {
                base.lower(),
                _norm_key(base),
                re.sub(r"\d+", "", base.lower()),
                _norm_key(re.sub(r"\d+", "", base)),
                re.sub(r"\brune\b", "", base, flags=re.I).strip().lower(),
                _norm_key(re.sub(r"\brune\b", "", base, flags=re.I).strip()),
            }

            for k in keys:
                if k:
                    m.setdefault(k, rel)
    return m


def get_rune_img_map(force: bool = False) -> dict[str, str]:
    if CACHE.get("rune_img_map") is not None and not force:
        return CACHE["rune_img_map"]
    CACHE["rune_img_map"] = build_rune_image_map(RUNE_ICON_DIR)
    return CACHE["rune_img_map"]


def resolve_rune_icon(set_name: str, rune_map: dict[str, str]) -> Optional[str]:
    if not set_name:
        return None

    candidates = [
        set_name,
        set_name.lower(),
        _norm_key(set_name),
        re.sub(r"\d+", "", set_name.lower()),
        _norm_key(re.sub(r"\d+", "", set_name)),
        f"{set_name} rune",
        f"rune {set_name}",
        _norm_key(f"{set_name} rune"),
        _norm_key(f"rune {set_name}"),
    ]

    for c in candidates:
        k1 = (c or "").strip().lower()
        if k1 in rune_map:
            return f"/images/games/zone-nova/runes/{rune_map[k1]}"
        k2 = _norm_key(c)
        if k2 in rune_map:
            return f"/images/games/zone-nova/runes/{rune_map[k2]}"
    return None


def load_rune_overrides(force: bool = False) -> dict:
    if CACHE["rune_overrides"] is not None and not force:
        return CACHE["rune_overrides"]
    data = safe_load_json(RUNE_OVERRIDES)
    CACHE["rune_overrides"] = data if isinstance(data, dict) else {}
    return CACHE["rune_overrides"]


# fallback only used when runes.js parsing fails
FALLBACK_RUNES = [
    {"name": "Alpha", "twoPiece": "Attack Power +8%", "fourPiece": "Basic Attack Damage +30%", "icon": None},
    {"name": "Beth", "twoPiece": "Critical Hit Rate +6%", "fourPiece": "When HP >80%: Critical Hit Damage +24%", "icon": None},
    {"name": "Zahn", "twoPiece": "HP +8%", "fourPiece": "After Ultimate: Take 5% less damage (10s)", "icon": None},
    {"name": "Shattered Foundation", "twoPiece": "Defense +12%", "fourPiece": "Shield Effectiveness +20%", "icon": None},
    {"name": "Daleth", "twoPiece": "Healing Effectiveness +10%", "fourPiece": "Battle Start: Gain 1 Energy immediately", "icon": None},
    {"name": "Epsilon", "twoPiece": "Attack Power +8%", "fourPiece": "After ultimate, team damage +10% (10s)", "icon": None, "note": "Same passive effect cannot stack"},
    {"name": "Hert", "twoPiece": "Extra Attack damage +20%", "fourPiece": "After dealing Extra Attack damage, Critical Hit Rate +15% (10s)", "icon": None, "note": "Guild raid only"},
    {"name": "Gimel", "twoPiece": "Continuous damage +20%", "fourPiece": "After dealing continuous damage, own attack power +2% (stacks up to 10, 5s)", "icon": None, "note": "Guild raid only"},
]


def load_runes_db(force: bool = False) -> list[dict]:
    if CACHE["runes_db"] is not None and not force:
        return CACHE["runes_db"]

    raw = safe_read_text(RUNES_JS)
    runes = None

    if raw:
        lit = _extract_js_literal(raw)
        if lit:
            # 1) pure json
            try:
                runes = json.loads(lit)
            except Exception:
                # 2) python literal eval (single quote / trailing comma robust)
                try:
                    runes = ast.literal_eval(_to_python_literal(lit))
                except Exception:
                    # 3) json-friendly best-effort
                    try:
                        runes = json.loads(_json_friendly(lit))
                    except Exception:
                        runes = None

    if not isinstance(runes, list):
        runes = FALLBACK_RUNES

    rune_img_map = get_rune_img_map()

    norm: list[dict] = []
    for r in runes:
        if not isinstance(r, dict):
            continue

        name = str(r.get("name") or r.get("title") or "").strip()
        if not name:
            continue

        # NOTE: runes.js가 한글을 포함하면 그대로 전달(두/네 세트 효과)
        two_piece = r.get("twoPiece") or r.get("two_piece") or r.get("2pc") or r.get("two") or r.get("twoSet") or ""
        four_piece = r.get("fourPiece") or r.get("four_piece") or r.get("4pc") or r.get("four") or r.get("fourSet") or ""

        icon = r.get("icon")
        img = r.get("image") or r.get("img") or r.get("jpg") or r.get("file") or r.get("iconFile")

        if not icon and isinstance(img, str) and img:
            icon = f"/images/games/zone-nova/runes/{img.strip().lstrip('/')}"

        if isinstance(icon, str) and icon and not icon.startswith("/"):
            icon = f"/images/games/zone-nova/runes/{icon.strip().lstrip('/')}"

        if not icon:
            icon = resolve_rune_icon(name, rune_img_map)

        norm.append({
            "name": name,
            "twoPiece": two_piece,
            "fourPiece": four_piece,
            "note": r.get("note") or "",
            "classRestriction": r.get("classRestriction") or r.get("class_restriction") or [],
            "teamConflict": r.get("teamConflict") or r.get("team_conflict") or [],
            "icon": icon if isinstance(icon, str) and icon else None,
        })

    CACHE["runes_db"] = norm
    return norm


def rune_db_by_name() -> dict[str, dict]:
    db = load_runes_db()
    return {str(r.get("name")): r for r in db if isinstance(r, dict) and r.get("name")}




# -------------------------
# Rune recommendation logic (V2) — role-based objective + exhaustive 4+2 optimization
# -------------------------

# NOTE:
# - Buffer/Debuffer/Healer/Tank/DPS 역할별로 목적함수를 분리하여 "팀 기여" 중심 추천을 우선한다.
# - 모든 룬을 동일한 후보로 보고(획득 난이도/희귀도 패널티 없음), 룬 세트(2/4) 효과를 태그화해 점수화한다.
# - 6슬롯 기준 4+2 조합을 전수 탐색하여 상위 조합을 추천한다.

# -------------------------
# Skill/stat text extraction & parsing
# -------------------------

_KW_TEAM = ["team", "all allies", "allied", "party", "all units", "아군", "파티", "팀", "전체 아군", "모든 아군"]
_KW_BUFF = ["increase", "increases", "up", "gain", "gains", "boost", "enhance", "강화", "증가", "상승", "획득"]
_KW_DEBUFF = ["decrease", "decreases", "reduce", "reduces", "weaken", "vulnerability", "받는 피해", "감소", "약화", "취약", "방깎", "저항 감소"]
_KW_ENERGY = ["energy", "energy recovery", "energy gain", "에너지", "기력"]
_KW_HEAL = ["heal", "healing", "restore", "recovery", "회복", "치유", "힐"]
_KW_SHIELD = ["shield", "barrier", "보호막", "실드"]
_KW_DOT = ["continuous damage", "dot", "burn", "bleed", "poison", "지속", "지속 피해", "도트", "중독", "출혈", "화상"]
_KW_EXTRA = ["extra attack", "follow-up", "counter", "추가 공격", "추격", "반격"]
_KW_ULT = ["ultimate", "ult", "burst", "궁극기", "필살기", "대기술"]

def _walk_texts_by_keys(obj, key_hint_re: re.Pattern) -> list[str]:
    out: list[str] = []

    def walk(v, parent_key: str = ""):
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
                walk(it, parent_key)
            return
        if isinstance(v, dict):
            for k, vv in v.items():
                kk = str(k)
                if key_hint_re.search(kk):
                    walk(vv, kk)
                else:
                    # even if key not matched, still go deeper one level to catch nested structures
                    if isinstance(vv, (dict, list)):
                        walk(vv, kk)
            return

    walk(obj)
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq

def extract_skill_texts(detail: dict) -> list[str]:
    # 다양한 json 구조(번역/표기 편차)를 흡수: skill/ability/desc/effect/... 키 중심으로만 수집
    if not isinstance(detail, dict):
        return []
    key_hint_re = re.compile(r"(skill|skills|ability|abilities|desc|description|effect|passive|ultimate|burst|normal|basic|auto|talent|team|각성|스킬|설명|효과|패시브|궁극)", re.I)
    return _walk_texts_by_keys(detail, key_hint_re)

def _scale_hits(text: str, stat_keys: list[str]) -> list[float]:
    # 스킬 스케일링 근거를 점수화하기 위한 히트 추출
    # - % 기반: 120% ATK, 공격력의 120%
    # - 배수 기반: 1.6x ATK, ATK x1.6
    # - based on / scales with 기반: 'based on DEF' 같이 수치가 없더라도 약한 힌트로 반영
    hits: list[float] = []
    tl = text.lower()

    # (1) percent: "120% attack power", "공격력의 120%"
    for k in stat_keys:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*[^%\n]{0,28}\b" + re.escape(k) + r"\b", tl):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

        for m in re.finditer(re.escape(k) + r"\s*의\s*(\d+(?:\.\d+)?)\s*%", text):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    # (2) multiplier: "1.6x ATK", "ATK x1.6"
    for k in stat_keys:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[x×]\s*\b" + re.escape(k) + r"\b", tl):
            try:
                hits.append(float(m.group(1)) * 100.0)
            except Exception:
                pass
        for m in re.finditer(r"\b" + re.escape(k) + r"\b\s*[x×]\s*(\d+(?:\.\d+)?)", tl):
            try:
                hits.append(float(m.group(1)) * 100.0)
            except Exception:
                pass

    # (3) based on / scales with: weak hint
    for k in stat_keys:
        if re.search(r"(based on|scale(?:s)? with|proportional to)\s+[^\n]{0,24}\b" + re.escape(k) + r"\b", tl):
            hits.append(60.0)

    return hits

def detect_no_crit(detail: dict) -> bool:
    # '치명타가 아예 없는 캐릭터' 판정:
    # - critRate=0 같은 수치만으로는 확정하지 않음(데이터 편차가 많음)
    # - 명시 문구/플래그(치명타 불가/ cannot crit 등)가 있을 때만 True
    if not isinstance(detail, dict):
        return False

    for k in ["noCrit", "no_crit", "cannotCrit", "cannot_crit", "critDisabled", "crit_disabled"]:
        if detail.get(k) is True:
            return True

    texts = extract_skill_texts(detail)
    for t in texts:
        tl = t.lower()
        if "cannot crit" in tl or "can't crit" in tl or "no critical" in tl or "치명타 불가" in t or "크리티컬 불가" in t:
            return True

    return False

def _detect_profile(detail: dict, base: dict) -> dict:
    texts = extract_skill_texts(detail or {})
    atk_hits: list[float] = []
    hp_hits: list[float] = []
    def_hits: list[float] = []

    heal_cnt = shield_cnt = dot_cnt = extra_cnt = ult_cnt = 0
    team_buff_cnt = team_debuff_cnt = 0
    energy_cnt = 0

    sample = {"ATK": None, "HP": None, "DEF": None}

    for t in texts:
        a = _scale_hits(t, ["attack power", "atk", "공격력", "공격"])
        h = _scale_hits(t, ["max hp", "hp", "체력", "생명", "생명력"])
        d = _scale_hits(t, ["defense", "def", "방어력", "방어"])

        if a and sample["ATK"] is None:
            sample["ATK"] = t
        if h and sample["HP"] is None:
            sample["HP"] = t
        if d and sample["DEF"] is None:
            sample["DEF"] = t

        atk_hits += a
        hp_hits += h
        def_hits += d

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
        if any(k in tl for k in _KW_ENERGY):
            energy_cnt += 1

        if any(k in tl for k in _KW_TEAM) and any(k in tl for k in _KW_BUFF):
            team_buff_cnt += 1
        if any(k in tl for k in _KW_TEAM) and any(k in tl for k in _KW_DEBUFF):
            team_debuff_cnt += 1

    def score(hits: list[float]) -> float:
        if not hits:
            return 0.0
        return len(hits) * 10.0 + (sum(hits) / max(len(hits), 1))

    atk_s = score(atk_hits)
    hp_s = score(hp_hits)
    def_s = score(def_hits)

    best = max(atk_s, hp_s, def_s)
    if best <= 0:
        scaling = "MIX"
    else:
        scaling = "ATK" if best == atk_s else ("HP" if best == hp_s else "DEF")

    cls = str((base or {}).get("class") or "-").strip()
    role = str((base or {}).get("role") or "-").strip()
    cls_l = cls.lower()
    role_l = role.lower()

    if cls_l == "healer" or role_l == "healer":
        archetype = "healer"
    elif cls_l == "guardian" or role_l == "tank":
        archetype = "tank"
    elif cls_l == "debuffer" or role_l == "debuffer":
        archetype = "debuffer"
    elif cls_l == "buffer" or role_l == "buffer" or "support" in cls_l or "support" in role_l:
        archetype = "buffer"
    else:
        archetype = "dps"

    total_sig = max(heal_cnt + shield_cnt + dot_cnt + extra_cnt + ult_cnt + team_buff_cnt + team_debuff_cnt + energy_cnt, 1)
    extra_share = extra_cnt / total_sig
    dot_share = dot_cnt / total_sig
    team_share = (team_buff_cnt + team_debuff_cnt) / total_sig
    ult_importance = min(1.0, (ult_cnt + energy_cnt) / max(total_sig, 1))

    healer_hybrid = bool(archetype == "healer" and atk_s >= 18.0 and (atk_s >= hp_s or atk_s >= def_s))
    buffer_hybrid = bool(archetype == "buffer" and atk_s >= 22.0 and team_share <= 0.25)
    debuffer_support = bool(archetype == "debuffer" and team_buff_cnt > 0)

    return {
        "scaling": scaling,
        "atk_score": atk_s,
        "hp_score": hp_s,
        "def_score": def_s,
        "heal_cnt": heal_cnt,
        "shield_cnt": shield_cnt,
        "dot_cnt": dot_cnt,
        "extra_cnt": extra_cnt,
        "ult_cnt": ult_cnt,
        "energy_cnt": energy_cnt,
        "team_buff_cnt": team_buff_cnt,
        "team_debuff_cnt": team_debuff_cnt,
        "extra_share": extra_share,
        "dot_share": dot_share,
        "team_share": team_share,
        "ult_importance": ult_importance,
        "archetype": archetype,
        "healer_hybrid": healer_hybrid,
        "buffer_hybrid": buffer_hybrid,
        "debuffer_support": debuffer_support,
        "sample_text": sample.get(scaling),
    }

def _norm_effect(s: str) -> str:
    return (s or "").strip().lower()

def rune_effect_tags(two_piece: str, four_piece: str) -> set[str]:
    t2 = _norm_effect(two_piece)
    t4 = _norm_effect(four_piece)
    tags: set[str] = set()

    # 2pc stats
    if "attack power" in t2 or "공격력" in t2:
        tags.add("ATK_2")
    if ("hp" in t2 and "+" in t2) or "체력" in t2 or "생명" in t2:
        tags.add("HP_2")
    if "defense" in t2 or "방어" in t2:
        tags.add("DEF_2")
    if "healing" in t2 or "치유" in t2 or "회복" in t2:
        tags.add("HEAL_2")
    if "critical hit rate" in t2 or "crit rate" in t2 or "치명타 확률" in t2 or "치확" in t2:
        tags.add("CRIT_RATE_2")

    # 4pc team/energy/heal/survive
    if any(k in t4 for k in ["team damage", "team dmg", "파티 피해", "팀 피해", "아군 피해"]) and ("+" in t4 or "increase" in t4 or "증가" in t4):
        tags.add("TEAM_DMG_BUFF_4")

    if any(k in t4 for k in ["energy", "에너지"]):
        if any(k in t4 for k in ["team", "all allies", "party", "파티", "팀", "아군", "전체"]):
            tags.add("TEAM_ENERGY_4")
        if "battle start" in t4 or "전투 시작" in t4:
            tags.add("START_ENERGY_4")

    if any(k in t4 for k in ["healing", "치유", "회복"]):
        tags.add("HEAL_4")
    if any(k in t4 for k in ["shield", "barrier", "보호막", "실드"]):
        tags.add("SHIELD_4")
    if any(k in t4 for k in ["less damage", "damage reduction", "피해 감소", "받는 피해 감소"]):
        tags.add("SURVIVE_4")

    # 4pc self-dmg
    if any(k in t4 for k in ["critical hit rate", "crit rate", "치명타 확률", "치확"]):
        tags.add("CRIT_RATE_4")
    if any(k in t4 for k in ["critical hit damage", "crit dmg", "치명타 피해", "치피"]):
        tags.add("CRIT_DMG_4")
    if any(k in t4 for k in ["basic attack damage", "기본 공격 피해", "기본공격 피해"]):
        tags.add("BASIC_DMG_4")
    if any(k in t4 for k in ["extra attack", "follow-up", "추가 공격", "추가공격"]):
        tags.add("EXTRA_SYNERGY_4")
    if any(k in t4 for k in ["continuous damage", "dot", "지속 피해", "지속피해"]):
        tags.add("DOT_SYNERGY_4")
    if any(k in t4 for k in ["vulnerability", "받는 피해", "피해 증가", "받는피해"]) and ("+" in t4 or "increase" in t4 or "증가" in t4):
        tags.add("VULN_DEBUFF_4")

    return tags

def role_tag_weights(profile: dict, no_crit: bool) -> dict[str, float]:
    archetype = profile.get("archetype", "dps")
    scaling = profile.get("scaling", "MIX")
    extra_share = float(profile.get("extra_share") or 0.0)
    dot_share = float(profile.get("dot_share") or 0.0)
    ult_imp = float(profile.get("ult_importance") or 0.0)
    team_share = float(profile.get("team_share") or 0.0)
    healer_hybrid = bool(profile.get("healer_hybrid"))
    buffer_hybrid = bool(profile.get("buffer_hybrid"))
    debuffer_support = bool(profile.get("debuffer_support"))

    w: dict[str, float] = {}

    def setw(tag: str, val: float):
        w[tag] = float(val)

    # default 2pc baseline
    setw("HP_2", 25.0)
    setw("DEF_2", 22.0)
    setw("ATK_2", 18.0)
    setw("HEAL_2", 18.0)
    setw("CRIT_RATE_2", 18.0 if not no_crit else -999.0)

    if archetype == "tank":
        setw("SURVIVE_4", 120.0)
        setw("SHIELD_4", 90.0)
        setw("HP_2", 55.0)
        setw("DEF_2", 55.0)
        setw("START_ENERGY_4", 20.0)
        setw("TEAM_ENERGY_4", 25.0)

    elif archetype == "healer":
        setw("HEAL_4", 120.0)
        setw("START_ENERGY_4", 70.0 + 20.0 * ult_imp)
        setw("TEAM_ENERGY_4", 60.0 + 20.0 * ult_imp)
        setw("SURVIVE_4", 55.0)
        setw("HP_2", 45.0)
        setw("DEF_2", 35.0)
        setw("TEAM_DMG_BUFF_4", 35.0)
        if healer_hybrid:
            setw("ATK_2", 28.0)
            setw("BASIC_DMG_4", 25.0)
            setw("CRIT_RATE_4", 25.0 if not no_crit else -999.0)
            setw("CRIT_DMG_4", 25.0 if not no_crit else -999.0)

    elif archetype == "buffer":
        setw("TEAM_DMG_BUFF_4", 130.0 + 30.0 * team_share)
        setw("TEAM_ENERGY_4", 120.0 + 30.0 * ult_imp)
        setw("START_ENERGY_4", 95.0 + 20.0 * ult_imp)
        setw("SURVIVE_4", 45.0)
        setw("SHIELD_4", 40.0)
        setw("HP_2", 55.0)
        setw("DEF_2", 45.0)
        setw("VULN_DEBUFF_4", 55.0)
        setw("BASIC_DMG_4", 10.0)
        setw("CRIT_RATE_4", 12.0 if not no_crit else -999.0)
        setw("CRIT_DMG_4", 12.0 if not no_crit else -999.0)
        setw("EXTRA_SYNERGY_4", 10.0 + (35.0 if extra_share >= 0.35 else 0.0))
        setw("DOT_SYNERGY_4", 10.0 + (35.0 if dot_share >= 0.35 else 0.0))
        if buffer_hybrid:
            setw("BASIC_DMG_4", 25.0)
            setw("ATK_2", 35.0)

    elif archetype == "debuffer":
        setw("VULN_DEBUFF_4", 130.0)
        setw("TEAM_DMG_BUFF_4", 80.0 if debuffer_support else 45.0)
        setw("TEAM_ENERGY_4", 70.0 + 20.0 * ult_imp)
        setw("START_ENERGY_4", 55.0 + 10.0 * ult_imp)
        setw("SURVIVE_4", 40.0)
        setw("HP_2", 35.0)
        setw("DEF_2", 30.0)
        setw("DOT_SYNERGY_4", 30.0 + (70.0 if dot_share >= 0.35 else 0.0))
        setw("EXTRA_SYNERGY_4", 25.0 + (55.0 if extra_share >= 0.35 else 0.0))
        setw("BASIC_DMG_4", 20.0)
        setw("CRIT_RATE_4", 20.0 if not no_crit else -999.0)
        setw("CRIT_DMG_4", 20.0 if not no_crit else -999.0)

    else:
        # DPS
        setw("BASIC_DMG_4", 85.0)
        setw("CRIT_RATE_4", 75.0 if not no_crit else -999.0)
        setw("CRIT_DMG_4", 70.0 if not no_crit else -999.0)
        setw("TEAM_DMG_BUFF_4", 30.0)
        setw("VULN_DEBUFF_4", 20.0)
        setw("TEAM_ENERGY_4", 20.0)
        setw("START_ENERGY_4", 18.0)
        setw("DOT_SYNERGY_4", 35.0 + (120.0 if dot_share >= 0.30 else 0.0))
        setw("EXTRA_SYNERGY_4", 35.0 + (120.0 if extra_share >= 0.30 else 0.0))
        if scaling == "HP":
            setw("HP_2", 45.0)
        if scaling == "DEF":
            setw("DEF_2", 45.0)

    return w

def _score_set_entry(set_name: str, pieces: int, rune: dict, weights: dict[str, float]) -> tuple[float, list[str]]:
    two_piece = str(rune.get("twoPiece") or "")
    four_piece = str(rune.get("fourPiece") or "")
    tags = rune_effect_tags(two_piece, four_piece)

    score = 0.0
    reasons: list[str] = []

    if pieces == 4:
        for tag in tags:
            if tag.endswith("_4"):
                score += weights.get(tag, 0.0)
    else:
        for tag in tags:
            if tag.endswith("_2"):
                score += weights.get(tag, 0.0)

    used = []
    for tag in tags:
        if pieces == 4 and tag.endswith("_4"):
            used.append((weights.get(tag, 0.0), tag))
        if pieces == 2 and tag.endswith("_2"):
            used.append((weights.get(tag, 0.0), tag))
    used.sort(reverse=True, key=lambda x: x[0])
    for w, tag in used[:2]:
        if abs(w) >= 1e-6:
            reasons.append(f"{tag}:{w:g}")

    return score, reasons

def _optimize_4_plus_2(profile: dict, no_crit: bool, rune_db: dict[str, dict]) -> list[dict]:
    # 모든 룬을 대상으로 (4세트 + 2세트) 조합을 전수 탐색
    weights = role_tag_weights(profile, no_crit)
    names = [n for n in rune_db.keys()]

    score4, reason4, score2, reason2 = {}, {}, {}, {}
    for n in names:
        r = rune_db[n]
        s4, rs4 = _score_set_entry(n, 4, r, weights)
        s2, rs2 = _score_set_entry(n, 2, r, weights)
        score4[n], reason4[n] = s4, rs4
        score2[n], reason2[n] = s2, rs2

    combos: list[tuple[float, str, str, list[str]]] = []
    for a in names:
        for b in names:
            if a == b:
                continue
            sc = score4[a] + score2[b]

            tags_a = rune_effect_tags(str(rune_db[a].get("twoPiece") or ""), str(rune_db[a].get("fourPiece") or ""))
            tags_b = rune_effect_tags(str(rune_db[b].get("twoPiece") or ""), str(rune_db[b].get("fourPiece") or ""))

            # support synergy: team dmg + energy/ult uptime
            if profile.get("archetype") in ("buffer", "debuffer", "healer"):
                if "TEAM_DMG_BUFF_4" in tags_a and ("TEAM_ENERGY_4" in tags_a or "START_ENERGY_4" in tags_a or "TEAM_ENERGY_4" in tags_b):
                    sc += 18.0
                if "TEAM_ENERGY_4" in tags_a and ("START_ENERGY_4" in tags_a or "START_ENERGY_4" in tags_b):
                    sc += 12.0

            # reduce over-trigger for DPS if dot/extra share is low
            if profile.get("archetype") == "dps":
                if "DOT_SYNERGY_4" in tags_a and float(profile.get("dot_share") or 0.0) < 0.25:
                    sc -= 20.0
                if "EXTRA_SYNERGY_4" in tags_a and float(profile.get("extra_share") or 0.0) < 0.25:
                    sc -= 20.0

            # hard-penalty for crit sets if no_crit
            if no_crit and ("CRIT_RATE_4" in tags_a or "CRIT_DMG_4" in tags_a):
                sc -= 999.0
            if no_crit and ("CRIT_RATE_2" in tags_b):
                sc -= 999.0

            reasons = []
            if reason4[a]:
                reasons.append(f"4pc {a}({', '.join(reason4[a])})")
            if reason2[b]:
                reasons.append(f"2pc {b}({', '.join(reason2[b])})")

            combos.append((sc, a, b, reasons))

    combos.sort(reverse=True, key=lambda x: x[0])

    out = []
    for sc, a, b, rs in combos[:8]:
        out.append({
            "score": round(sc, 2),
            "setPlan": [{"set": a, "pieces": 4}, {"set": b, "pieces": 2}],
            "reasons": rs,
        })
    return out

def _element_damage_label(element: str) -> str:
    e = normalize_element(element or "-")
    if e in ("Storm", "Blaze", "Frost", "Holy", "Chaos"):
        return f"{e} Attribute Damage (%)"
    return "Element Attribute Damage (%)"

def _slot_plan_for(archetype: str, scaling: str, element: str, no_crit: bool) -> dict:
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
        plan["4"] = ["Defense (%)", "HP (%)"] if scaling == "DEF" else ["HP (%)", "Defense (%)"]
        plan["5"] = ["Defense (%)", "HP (%)"] if scaling == "DEF" else ["HP (%)", "Defense (%)"]
        plan["6"] = ["Defense (%)", "HP (%)"] if scaling == "DEF" else ["HP (%)", "Defense (%)"]
        return plan

    if archetype == "buffer":
        plan["4"] = ["HP (%)", "Defense (%)", "Healing Effectiveness (%) (있을 때)"]
        plan["5"] = ["HP (%)", "Defense (%)", _element_damage_label(element) + " (부옵 수준)"]
        plan["6"] = ["HP (%)", "Defense (%)"]
        return plan

    if archetype == "debuffer":
        if no_crit:
            plan["4"] = ["Attack Penetration (%)", "Attack (%)", "HP (%) (생존)"]
        else:
            plan["4"] = ["Attack Penetration (%)", "Critical Rate (%)", "Attack (%)"]
        plan["5"] = [_element_damage_label(element), "HP (%)", "Defense (%)", "Attack (%)"]
        plan["6"] = ["HP (%)", "Defense (%)", "Attack (%)"]
        return plan

    # dps
    if no_crit:
        plan["4"] = ["Attack Penetration (%)", "Attack (%)", "HP (%) (생존)"]
        plan["5"] = [_element_damage_label(element), "Attack (%)", "HP (%) (생존)"]
        plan["6"] = ["Attack (%)", "HP (%) (생존)", "Defense (%) (생존)"]
    else:
        plan["4"] = ["Critical Rate (%)", "Attack Penetration (%)", "Critical Damage (%)", "Attack (%)"]
        plan["5"] = [_element_damage_label(element), "Attack (%)", "HP (%)", "Defense (%)"]
        plan["6"] = ["Attack (%)", "HP (%)", "Defense (%)"]

    return plan

def _substats_for(archetype: str, scaling: str, no_crit: bool) -> list[str]:
    if archetype == "healer":
        return [
            "Healing Effectiveness (%)",
            "HP (%)",
            "Defense (%)",
            "Attack (%) (하이브리드일 때만)",
            "Flat HP / Flat DEF",
        ]
    if archetype == "tank":
        return [
            "HP (%)",
            "Defense (%)",
            "Flat HP / Flat DEF",
            "Damage Reduction / RES (존재 시)",
        ]
    if archetype == "buffer":
        return [
            "HP (%)",
            "Defense (%)",
            "Energy-related stat (존재 시)",
            "Effectiveness/RES (존재 시)",
            "Flat HP / Flat DEF",
        ]
    if archetype == "debuffer":
        out = [
            "Attack Penetration (%)",
            "Attack (%)",
            "Element Attribute Damage (%)",
            "HP (%) / Defense (%) (생존)",
        ]
        if not no_crit:
            out.insert(0, "Critical Rate (%) (가능/유효 시)")
            out.insert(1, "Critical Damage (%) (가능/유효 시)")
        if scaling in ("HP", "DEF"):
            out.insert(2, f"{scaling} (%) (스킬 스케일링 기반)")
        return out

    if no_crit:
        out = [
            "Attack (%)",
            "Attack Penetration (%)",
            "Element Attribute Damage (%)",
            "Flat Attack",
            "HP (%) / Defense (%) (생존)",
        ]
        if scaling in ("HP", "DEF"):
            out.insert(0, f"{scaling} (%) (스킬 스케일링 기반)")
        return out

    out = [
        "Critical Rate (%)",
        "Critical Damage (%)",
        "Attack (%)",
        "Attack Penetration (%)",
        "Flat Attack",
        "HP (%) / Defense (%) (생존)",
    ]
    if scaling in ("HP", "DEF"):
        out.insert(2, f"{scaling} (%) (스킬 스케일링 기반)")
    return out

def recommend_runes(cid: str, base: dict, detail: dict) -> dict:
    overrides = load_rune_overrides()
    rune_db = rune_db_by_name()

    ov = overrides.get(cid)
    if isinstance(ov, dict) and ov.get("builds"):
        builds = []
        for b in ov.get("builds") or []:
            if not isinstance(b, dict):
                continue
            sp = []
            for it in b.get("setPlan") or []:
                if not isinstance(it, dict):
                    continue
                sname = str(it.get("set") or "").strip()
                r = rune_db.get(sname) or {}
                sp.append({
                    "set": sname,
                    "pieces": int(it.get("pieces") or 0),
                    "icon": r.get("icon"),
                    "twoPiece": r.get("twoPiece", ""),
                    "fourPiece": r.get("fourPiece", ""),
                    "note": r.get("note", ""),
                })
            builds.append({
                "title": str(b.get("title") or "Override"),
                "setPlan": sp,
                "slots": b.get("slots") or {},
                "substats": b.get("substats") or [],
                "notes": b.get("notes") or [],
                "rationale": b.get("rationale") or ["rune_overrides.json 수동 오버라이드 적용"],
            })
        return {"mode": "override", "profile": {"note": "rune_overrides.json 적용"}, "builds": builds}

    profile = _detect_profile(detail or {}, base or {})
    no_crit = detect_no_crit(detail or {})

    ranked = _optimize_4_plus_2(profile, no_crit, rune_db)

    rationale: list[str] = []
    rationale.append(f"역할 판정: {profile.get('archetype')} (class={base.get('class')}, role={base.get('role')})")
    rationale.append(f"스케일링 판정: {profile.get('scaling')} (ATK={profile.get('atk_score'):.1f}, HP={profile.get('hp_score'):.1f}, DEF={profile.get('def_score'):.1f})")

    st = profile.get("sample_text")
    if isinstance(st, str) and st.strip():
        rationale.append(f"스케일링 근거 문구: '{st[:140]}'")

    if no_crit:
        rationale.append("치명타 불가/제한 문구를 감지 → 치명타(치확/치피) 계열 세트/옵션을 제외.")

    def mk_build(title: str, setplan: list[dict], reasons: list[str]) -> dict:
        sp = []
        for x in setplan:
            sname = x["set"]
            r = rune_db.get(sname) or {}
            sp.append({
                "set": sname,
                "pieces": x["pieces"],
                "icon": r.get("icon"),
                "twoPiece": r.get("twoPiece", ""),
                "fourPiece": r.get("fourPiece", ""),
                "note": r.get("note", ""),
            })
        archetype = profile.get("archetype", "dps")
        return {
            "title": title,
            "setPlan": sp,
            "slots": _slot_plan_for(archetype, profile.get("scaling"), base.get("element"), no_crit),
            "substats": _substats_for(archetype, profile.get("scaling"), no_crit),
            "notes": [],
            "rationale": rationale + (reasons or []),
        }

    builds = []
    if ranked:
        builds.append(mk_build("추천(자동)", ranked[0]["setPlan"], ranked[0].get("reasons") or []))
        for idx, cand in enumerate(ranked[1:4], start=1):
            builds.append(mk_build(f"대체안 {idx}", cand["setPlan"], cand.get("reasons") or []))
    else:
        builds.append(mk_build("추천(자동)", [], ["룬 데이터 파싱 실패/비정상 → 룬 추천을 생성하지 못했습니다."]))

    notes = []
    for b in builds:
        for s in b.get("setPlan") or []:
            nm = s.get("set")
            r = rune_db.get(nm) or {}
            cr = r.get("classRestriction") or []
            if cr:
                notes.append(f"{nm} 4세트는 클래스 제한이 있습니다: {', '.join(map(str, cr))}")
            if r.get("note"):
                notes.append(f"{nm}: {r.get('note')}")
            if r.get("teamConflict"):
                notes.append(f"{nm}: 팀 세트 상충 주의 ({', '.join(map(str, r.get('teamConflict')))} )")

    seen, uniq_notes = set(), []
    for n in notes:
        if n not in seen:
            seen.add(n)
            uniq_notes.append(n)
    if uniq_notes:
        for b in builds:
            b["notes"] = uniq_notes

    return {"mode": "auto", "profile": {**profile, "no_crit": no_crit}, "builds": builds}

def rune_summary_for_list(cid: str, base: dict, detail: dict) -> Optional[dict]:
    reco = recommend_runes(cid, base, detail)
    builds = reco.get("builds") or []
    if not builds:
        return None
    b0 = builds[0]
    return {"mode": reco.get("mode"), "sets": [{"set": s.get("set"), "pieces": s.get("pieces"), "icon": s.get("icon")} for s in (b0.get("setPlan") or [])]}

# -------------------------
# Load characters
# -------------------------

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
                "element_icon": element_icon_url(element),
                "class_icon": class_icon_url(cls),
            }

            # 리스트용 룬 미니 요약
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


# -------------------------
# Routes
# -------------------------

@app.get("/")
def home():
    return redirect("/ui/select")


@app.get("/refresh")
def refresh():
    load_all(force=True)
    CACHE["runes_db"] = None
    CACHE["rune_overrides"] = None
    CACHE["rune_img_map"] = None
    return redirect("/ui/select")


@app.get("/meta")
def meta():
    load_all()
    return jsonify({
        "title": APP_TITLE,
        "characters_cached": len(CACHE["chars"]),
        "last_refresh": CACHE["last_refresh"],
        "error": CACHE["error"],
        "paths": {
            "characters_ko": CHAR_KO_DIR,
            "runes_js": RUNES_JS,
            "rune_overrides": RUNE_OVERRIDES,
        }
    })


@app.get("/zones/zone-nova/runes")
def api_runes():
    db = load_runes_db()
    return jsonify({"count": len(db), "runes": db})


@app.get("/zones/zone-nova/characters")
def api_chars():
    load_all()
    return jsonify({
        "count": len(CACHE["chars"]),
        "last_refresh": CACHE["last_refresh"],
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
        "rune_reco": rune_reco,
        "detail_source": f"public/data/zone-nova/characters_ko/{cid2}.json",
    })


@app.post("/zones/zone-nova/recommend")
def api_recommend_party():
    """
    파티 추천 API
    - owned: 보유 캐릭터 id 리스트
    - required: 필수 포함 캐릭터 id 리스트
    - required_classes: 포함되어야 하는 클래스 리스트(최대 4)
    - rank_map: 등급표 기반 점수 맵 (cid -> 0~4 등)
    - party_size: 기본 4
    - top_k: 상위 k개 결과(기본 1)
    - require_combo: 콤보(같은 속성 2+ 또는 같은 특성 2+) 강제 여부(기본 True)
    """
    try:
        payload = request.get_json(silent=True) or {}

        owned = payload.get("owned") or []
        required = payload.get("required") or []
        required_classes = payload.get("required_classes") or []
        rank_map = payload.get("rank_map") or {}
        party_size = payload.get("party_size") or 4
        top_k = payload.get("top_k") or 1
        require_combo = payload.get("require_combo")
        if not isinstance(require_combo, bool):
            require_combo = True

        if not isinstance(rank_map, dict):
            rank_map = {}

        res = recommend_best_party(
            owned_ids=owned if isinstance(owned, list) else [],
            required_ids=required if isinstance(required, list) else [],
            required_classes=required_classes if isinstance(required_classes, list) else [],
            rank_map=rank_map,
            party_size=int(party_size) if str(party_size).isdigit() else 4,
            top_k=int(top_k) if str(top_k).isdigit() else 1,
            require_combo=bool(require_combo),
        )

        code = 200 if res.get("ok") else 400
        return jsonify(res), code

    except Exception as e:
        # 프론트는 JSON을 기대하므로, 어떤 내부 에러도 JSON으로 반환
        debug = os.getenv("FLASK_DEBUG") == "1"
        err = f"server_error: {type(e).__name__}: {e}"
        if debug:
            import traceback
            err = err + "\n" + traceback.format_exc()
        return jsonify({"ok": False, "error": err}), 500


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


@app.get("/runes")
def runes_page():
    return render_template(
        "runes.html",
        title="룬 정보",
        last_refresh=CACHE.get("last_refresh") or "",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    debug = os.getenv("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
