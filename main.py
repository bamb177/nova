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
# Rune recommendation logic
# -------------------------

def _to_float(x, default=None):
    if x is None:
        return default
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.replace("%","").strip())
        except Exception:
            return default
    return default

def _get_base_crit(detail: dict) -> tuple[float, float]:
    """
    캐릭터 base critRate(0~1), critDmg(보너스, 예: 0.50=+50%)
    JSON 구조가 다를 수 있으니 보수적 기본값 사용.
    """
    st = _extract_canonical_stats(detail or {})
    cr = _to_float(st.get("CRIT_RATE"), None)
    cd = _to_float(st.get("CRIT_DMG"), None)

    # 일부 데이터가 0~100(%)로 들어올 수 있으므로 보정
    if cr is not None and cr > 1.0:
        cr = cr / 100.0
    if cd is not None and cd > 2.0:
        cd = cd / 100.0

    # 기본값(게임마다 다르지만 비교용)
    if cr is None:
        cr = 0.05
    if cd is None:
        cd = 0.50
    return max(0.0, min(1.0, cr)), max(0.0, cd)

def _stat_weight_by_scaling(scaling: str) -> dict:
    # 스킬 스케일링 기반으로 “공격/체력/방어 %”가 딜에 기여하는 비중을 러프하게 가중
    scaling = (scaling or "MIX").upper()
    if scaling == "ATK":
        return {"atk_pct": 1.00, "hp_pct": 0.25, "def_pct": 0.25}
    if scaling == "HP":
        return {"atk_pct": 0.35, "hp_pct": 1.00, "def_pct": 0.25}
    if scaling == "DEF":
        return {"atk_pct": 0.35, "hp_pct": 0.25, "def_pct": 1.00}
    return {"atk_pct": 0.60, "hp_pct": 0.35, "def_pct": 0.35}

def _estimate_uptime(cond: Optional[dict], profile: dict, detail: dict, burst_window_s: float = 20.0) -> float:
    """
    조건부 4세트의 업타임을 0~1로 추정.
    - 제한시간(버스트) 컨텐츠 기준: burst_window_s를 짧게 잡을수록 “초반 즉발/짧은 버프”가 유리해짐
    """
    if not cond:
        return 1.0

    ctype = cond.get("type")
    dur = cond.get("dur")
    dur = float(dur) if isinstance(dur, (int, float)) else None

    if ctype == "hp_cond":
        # HP>80 유지 가능성: 자해/HP소모 문구 있으면 하락
        texts = _collect_texts((detail or {}).get("skills"))
        blob = "\n".join([t.lower() for t in texts])
        bad = ["consume hp", "lose hp", "hp cost", "sacrifice", "self damage", "체력 소모", "자해", "희생", "체력 감소"]
        if any(k in blob for k in bad):
            return 0.55
        return 0.80

    if ctype == "battle_start":
        # 시작 즉발은 버스트에서 항상 강함
        return 1.0

    # 트리거형: 빈도(키워드 카운트)로 대략적 발생률 추정 후, duration/burst_window로 업타임 계산
    if ctype in ("after_ultimate", "after_extra", "after_dot"):
        cnt_key = {"after_ultimate": "ult_cnt", "after_extra": "extra_cnt", "after_dot": "dot_cnt"}[ctype]
        c = float(profile.get(cnt_key) or 0.0)

        # 발생률(0~1): 카운트가 늘수록 증가, 과대평가 방지
        freq = min(1.0, 0.20 * c)

        if dur is None or dur <= 0:
            # duration 정보 없으면 보수적으로 절반만
            return 0.50 * freq

        # 업타임 ≈ 발생률 * (버프 지속 / 제한시간)
        return min(1.0, freq * (dur / max(1.0, burst_window_s)))

    return 0.60

def _expected_crit_gain(delta_cr: float, delta_cd: float, base_cr: float, base_cd: float) -> float:
    """
    기대 딜 증가를 단순화:
      기대배율 = 1 + CR * CD
    이때 CR/CD 변화의 1차 근사 증가량:
      d( CR*CD ) ≈ delta_cr*base_cd + base_cr*delta_cd
    """
    return (max(0.0, delta_cr) * max(0.0, base_cd)) + (max(0.0, base_cr) * max(0.0, delta_cd))

def score_rune_piece(effect: dict, profile: dict, detail: dict, no_crit: bool, burst_window_s: float = 20.0) -> float:
    """
    effect: parse_rune_effect_text() 결과(2pc 또는 4pc)
    반환: 비교용 점수(높을수록 딜/기여 증가)
    """
    mods = (effect or {}).get("mods") or {}
    cond = (effect or {}).get("cond")
    uptime = _estimate_uptime(cond, profile, detail, burst_window_s=burst_window_s)

    scaling = profile.get("scaling") or "MIX"
    archetype = profile.get("archetype") or "dps"

    w = _stat_weight_by_scaling(scaling)

    base_cr, base_cd = _get_base_crit(detail or {})
    if no_crit:
        base_cr = 0.0
        base_cd = 0.0

    # --- 딜 관련 가중치(버스트 기준) ---
    # “추가공격/도트 비중”은 profile 카운트로 러프 추정
    extra_share = min(0.55, 0.12 * float(profile.get("extra_cnt") or 0))
    dot_share = min(0.55, 0.12 * float(profile.get("dot_cnt") or 0))
    basic_share = 0.30  # 기본 공격 비중 기본값(데이터 없을 때)

    score = 0.0

    # 1) 스탯% (스케일링 반영)
    score += uptime * (mods.get("atk_pct", 0.0) * w["atk_pct"])
    score += uptime * (mods.get("hp_pct", 0.0) * w["hp_pct"])
    score += uptime * (mods.get("def_pct", 0.0) * w["def_pct"])

    # 2) 크리 관련 (no_crit이면 자동 0)
    if not no_crit:
        dcr = mods.get("crit_rate", 0.0)
        dcd = mods.get("crit_dmg", 0.0)
        score += uptime * _expected_crit_gain(dcr, dcd, base_cr, base_cd)

    # 3) 피해 계열(공격 타입별 비중 반영)
    score += uptime * (mods.get("basic_dmg", 0.0) * basic_share)
    score += uptime * (mods.get("extra_dmg", 0.0) * extra_share)
    score += uptime * (mods.get("dot_dmg", 0.0) * dot_share)

    # 4) 팀 피해(딜러에게도 제한시간에서 유효하지만, 개인딜보다 낮게)
    score += uptime * (mods.get("team_dmg", 0.0) * 0.60)

    # 5) 힐/실드(딜 최적화 기준에서는 낮게. 단 archetype별로 보정 가능)
    if archetype == "healer":
        score += uptime * (mods.get("heal_eff", 0.0) * 0.80)
    else:
        score += uptime * (mods.get("heal_eff", 0.0) * 0.05)

    if archetype == "tank":
        score += uptime * (mods.get("shield_eff", 0.0) * 0.60)
    else:
        score += uptime * (mods.get("shield_eff", 0.0) * 0.05)

    # 6) 전투 시작 에너지: 제한시간(버스트)에서 강력. 궁극기 빈도가 높을수록 점수↑
    if mods.get("energy_start"):
        ult = float(profile.get("ult_cnt") or 0.0)
        score += 0.12 + min(0.10, 0.02 * ult)

    return score


_KW_HEAL = ["heal", "healing", "restore", "recovery", "회복", "치유", "힐"]
_KW_SHIELD = ["shield", "barrier", "보호막", "실드"]
_KW_DOT = ["continuous damage", "dot", "burn", "bleed", "poison", "지속", "지속 피해", "도트"]
_KW_EXTRA = ["extra attack", "follow-up", "추가 공격", "추격", "연속 공격"]
_KW_ULT = ["ultimate", "ult", "궁극기", "필살기"]

# ✅ no-crit 텍스트 힌트(데이터에 스탯 키가 없을 때 보완)
_KW_NO_CRIT = [
    "cannot crit", "can't crit", "no critical", "non-critical",
    "critical hit cannot", "critical cannot",
    "치명타 불가", "치명타가 발생하지", "크리티컬 불가", "크리티컬이 발생하지",
]

# =========================
# Skill/Stat parsing 강화
# =========================

_SKILL_TYPE_ALIASES = {
    "normal": [
        "normal", "basic", "basic attack", "normal attack", "auto attack",
        "일반", "기본", "평타", "통상", "일반공격", "기본공격",
    ],
    "auto": [
        "auto", "active", "skill", "special", "combat skill",
        "자동", "액티브", "스킬", "특수", "전투스킬",
    ],
    "ultimate": [
        "ultimate", "ult", "burst", "finisher",
        "궁극", "필살", "궁극기", "필살기", "버스트",
    ],
    "passive": [
        "passive", "talent", "trait", "aura",
        "패시브", "특성", "재능", "오라",
    ],
    "team": [
        "team", "teamskill", "team skill",
        "팀", "팀스킬", "파티", "연계",
    ],
}

# 스킬 타입별 가중치(스케일링 판정에 반영)
_SKILL_TYPE_WEIGHT = {
    "ultimate": 1.45,
    "auto": 1.20,
    "normal": 1.00,
    "passive": 0.85,
    "team": 1.05,
    "unknown": 1.00,
}

# stats 키 후보(영/한/약어/번역 흔들림 대비)
_STAT_KEY_CANDIDATES = {
    "HP": [
        "hp", "maxhp", "max_hp", "health", "maxhealth", "life", "vitality",
        "체력", "최대체력", "생명력", "최대생명력",
    ],
    "ATK": [
        "atk", "attack", "attackpower", "attack_power", "power",
        "공격", "공격력", "공격력증가",
    ],
    "DEF": [
        "def", "defense", "defence", "armor", "armour",
        "방어", "방어력", "방어도",
    ],
    "CRIT_RATE": [
        "critrate", "crit_rate", "criticalrate", "critical_rate",
        "criticalhitrate", "critical_hit_rate", "crit", "cr",
        "치명", "치명률", "치명타", "치명타확률", "치명타확률증가",
        "크리", "크리확률", "크리티컬확률",
    ],
    "CRIT_DMG": [
        "critdmg", "crit_dmg", "criticaldmg", "critical_dmg", "criticaldamage", "critical_damage",
        "criticalhitdamage", "critical_hit_damage", "cd",
        "치명타피해", "치피", "크리피해", "크리티컬피해",
    ],
    "CAN_CRIT": [
        "cancrit", "can_crit", "criticalenabled", "critical_enabled",
        "cannotcrit", "cannot_crit", "critdisabled", "crit_disabled",
        "치명타가능", "치명타불가", "크리불가",
    ],
}

# 스케일링 탐지용 키워드(문장 내 표기)
_SCALE_KEYS = {
    "ATK": ["attack power", "attack", "atk", "공격력", "공격"],
    "HP": ["max hp", "hp", "health", "life", "체력", "생명력", "최대체력", "최대 생명력"],
    "DEF": ["defense", "defence", "def", "armor", "방어력", "방어"],
}

def _norm_k(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"[\s\-_]+", "", s)
    s = re.sub(r"[^a-z0-9가-힣]", "", s)
    return s

def _guess_skill_type_from_text(text: str) -> str:
    tl = (text or "").strip().lower()
    for typ, kws in _SKILL_TYPE_ALIASES.items():
        for k in kws:
            if k in tl:
                return typ
    return "unknown"

def _guess_skill_type(skill_obj: dict, fallback_text: str = "") -> str:
    """
    skill dict 내부 키/값을 보고 normal/auto/ultimate/passive/team 추정.
    """
    if not isinstance(skill_obj, dict):
        return _guess_skill_type_from_text(fallback_text)

    # 우선적으로 type/kind/category 같은 키를 확인
    for key in ["type", "kind", "category", "slot", "skillType", "skill_type", "tag", "group"]:
        v = skill_obj.get(key)
        if isinstance(v, str) and v.strip():
            t = _guess_skill_type_from_text(v)
            if t != "unknown":
                return t

    # 이름/제목도 힌트가 됨
    for key in ["name", "title", "label"]:
        v = skill_obj.get(key)
        if isinstance(v, str) and v.strip():
            t = _guess_skill_type_from_text(v)
            if t != "unknown":
                return t

    # 그래도 없으면 fallback_text
    return _guess_skill_type_from_text(fallback_text)

def _collect_texts(x) -> list[str]:
    """
    (기존 함수 대체) 문자열을 깊게 수집.
    """
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
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq

def _collect_skill_texts(detail: dict) -> list[tuple[str, str]]:
    """
    skills / teamSkill 구조가 리스트/딕셔너리/중첩이든 간에
    가능한 한 (skill_type, text) 형태로 수집한다.
    """
    pairs: list[tuple[str, str]] = []

    if not isinstance(detail, dict):
        return pairs

    def push(typ: str, txt: str):
        t = (txt or "").strip()
        if t:
            pairs.append((typ, t))

    # 1) skills
    skills = detail.get("skills")
    if skills is None:
        # 흔한 변형 키
        for k in ["skill", "Skill", "abilities", "ability", "combatSkills", "combat_skills"]:
            if k in detail:
                skills = detail.get(k)
                break

    def walk_skill_obj(obj, forced_type: Optional[str] = None):
        if obj is None:
            return
        if isinstance(obj, str):
            push(forced_type or "unknown", obj)
            return
        if isinstance(obj, list):
            for it in obj:
                walk_skill_obj(it, forced_type)
            return
        if isinstance(obj, dict):
            # 스킬 객체: description 계열 우선
            typ = forced_type or _guess_skill_type(obj)
            for k in ["description", "desc", "effect", "text", "detail", "tooltip", "summary"]:
                v = obj.get(k)
                if isinstance(v, str):
                    push(typ, v)
                elif isinstance(v, (dict, list)):
                    for s in _collect_texts(v):
                        push(typ, s)

            # 각 레벨/단계 효과도 포함
            for k in ["levels", "level", "rank", "ranks", "effects"]:
                v = obj.get(k)
                if isinstance(v, (list, dict)):
                    for s in _collect_texts(v):
                        push(typ, s)

            # 남은 필드도 한번 훑되, 과도한 노이즈 방지 위해 문자열만
            for kk, vv in obj.items():
                if kk in ("description","desc","effect","text","detail","tooltip","summary","levels","level","rank","ranks","effects"):
                    continue
                if isinstance(vv, str) and len(vv.strip()) >= 8:
                    # 짧은 라벨은 노이즈가 많아 길이 제한
                    push(typ, vv)

            return

    # skills 구조 처리
    if isinstance(skills, list):
        for it in skills:
            walk_skill_obj(it, None)
    elif isinstance(skills, dict):
        # {"normal": {...}, "ultimate": {...}} 같은 케이스
        for k, v in skills.items():
            forced = _guess_skill_type_from_text(str(k))
            walk_skill_obj(v, forced if forced != "unknown" else None)

    # 2) teamSkill
    team = detail.get("teamSkill") or detail.get("team_skill") or detail.get("team")
    if team is not None:
        # teamSkill은 타입을 team으로 강제
        for s in _collect_texts(team):
            push("team", s)

    # 중복 제거(타입+텍스트)
    seen = set()
    uniq: list[tuple[str, str]] = []
    for typ, txt in pairs:
        key = (typ, txt)
        if key not in seen:
            seen.add(key)
            uniq.append(key)
    return uniq

def _try_parse_percent(x: str) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _pct_hits(text: str, keys: list[str]) -> list[float]:
    """
    (기존 함수 교체) 스케일링 %를 더 촘촘하게 탐지.
    반환값은 '퍼센트 수치'로 통일(예: 1.2배는 120으로 변환).
    """
    hits: list[float] = []
    if not text:
        return hits

    t = text.lower()

    # 0) ATK×1.2 / ATK*1.2 / 1.2*ATK / ATK x 1.2
    #    -> 120%로 환산
    for k in keys:
        kk = re.escape(k.lower())
        for m in re.finditer(rf"\b{kk}\b\s*[\*x×]\s*(\d+(?:\.\d+)?)", t):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v * 100.0)
        for m in re.finditer(rf"(\d+(?:\.\d+)?)\s*[\*x×]\s*\b{kk}\b", t):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v * 100.0)

    # 1) "120% attack power" / "120% of ATK" / "120% ATK"
    for k in keys:
        kk = re.escape(k.lower())
        # "120% ... atk"
        for m in re.finditer(rf"(\d+(?:\.\d+)?)\s*%\s*[^%\n]{{0,40}}\b{kk}\b", t):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v)

        # "120% of atk"
        for m in re.finditer(rf"(\d+(?:\.\d+)?)\s*%\s*(?:of|based\s+on|equal\s+to)\s*[^%\n]{{0,20}}\b{kk}\b", t):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v)

        # "atk 120%"
        for m in re.finditer(rf"\b{kk}\b\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%", t):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v)

    # 2) 한국어: "공격력의 120%" / "공격력 120%의 피해" / "최대 체력의 10%"
    for k in keys:
        # k는 이미 한글 포함 가능, 원문(text)로도 체크
        for m in re.finditer(re.escape(k) + r"\s*의\s*(\d+(?:\.\d+)?)\s*%", text):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v)
        for m in re.finditer(re.escape(k) + r"\s*(\d+(?:\.\d+)?)\s*%\s*의", text):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v)

    # 3) "Deals damage equal to 120% of Max HP" 같은 장문 패턴
    for k in keys:
        kk = re.escape(k.lower())
        for m in re.finditer(rf"(?:equal\s+to|deals|deal|damage)\s*[^%\n]{{0,60}}(\d+(?:\.\d+)?)\s*%\s*[^%\n]{{0,30}}\b{kk}\b", t):
            v = _try_parse_percent(m.group(1))
            if v is not None:
                hits.append(v)

    return hits

def _extract_canonical_stats(detail: dict) -> dict[str, Any]:
    """
    detail 내부 stats/attributes 등에서 hp/atk/def/critRate/critDmg/canCrit을 최대한 회수.
    값 형식은 원 데이터가 섞여있으니 여기서는 원값을 유지하고, 숫자 변환은 필요 시만.
    """
    out = {
        "HP": None,
        "ATK": None,
        "DEF": None,
        "CRIT_RATE": None,
        "CRIT_DMG": None,
        "CAN_CRIT": None,
        "_found_keys": [],
    }

    if not isinstance(detail, dict):
        return out

    containers = []
    for k in ["stats", "stat", "attributes", "attribute", "baseStats", "base_stats", "params", "param"]:
        v = detail.get(k)
        if v is not None:
            containers.append(v)

    def try_set(canon: str, key: str, val: Any):
        if out.get(canon) is None and val is not None:
            out[canon] = val
            out["_found_keys"].append(key)

    def scan_obj(obj):
        if obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = _norm_k(str(k))
                # CAN_CRIT은 false/true가 많아 우선 처리
                if nk in [_norm_k(x) for x in _STAT_KEY_CANDIDATES["CAN_CRIT"]]:
                    try_set("CAN_CRIT", str(k), v)
                    continue
                for canon, cands in _STAT_KEY_CANDIDATES.items():
                    if canon == "CAN_CRIT":
                        continue
                    if nk in [_norm_k(x) for x in cands]:
                        try_set(canon, str(k), v)
                        break
                # deeper
                if isinstance(v, (dict, list)):
                    scan_obj(v)
        elif isinstance(obj, list):
            for it in obj:
                scan_obj(it)

    # 1) 후보 컨테이너 먼저 스캔
    for c in containers:
        scan_obj(c)

    # 2) 그래도 부족하면 전체를 얕게 한 번 더(과탐 방지: 키 매칭만)
    scan_obj(detail)

    return out



# --- v2: robust stat multiplier parser (ATK/HP/DEF/CRIT) ---

_STAT_ALIASES = {
    # 공격 계열
    "ATK": [
        "attack power", "attack", "atk", "atkp", "attack stat",
        "공격력", "공격", "공격 수치",
    ],
    # 방어 계열
    "DEF": [
        "defense", "def", "defence",
        "방어력", "방어", "방어 수치",
    ],
    # 체력 계열
    "HP": [
        "max hp", "maximum hp", "hp", "health", "life",
        "최대 체력", "최대체력", "체력", "생명", "생명력",
    ],
    # 치확/치피 (스케일링이 아니라 “키워드 탐지/카운트” 목적도 포함)
    "CRIT_RATE": [
        "critical hit rate", "critical rate", "crit rate", "critical chance", "crit chance",
        "치명타 확률", "치명 확률", "치명률", "치확",
        "크리 확률", "크리티컬 확률", "크리율",
    ],
    "CRIT_DMG": [
        "critical hit damage", "critical damage", "crit damage", "crit dmg",
        "치명타 피해", "치명 피해", "치명피해", "치피",
        "크리 피해", "크리티컬 피해",
    ],
}

def _term_to_pat(term: str) -> str:
    """
    용어를 regex-friendly 패턴으로 변환:
    - 공백은 \s* 로 흡수
    - 나머지는 escape
    """
    t = (term or "").strip()
    if not t:
        return ""
    t = re.escape(t)
    t = t.replace(r"\ ", r"\s*")
    return t

def _alt_pat(stat_code: str) -> str:
    terms = _STAT_ALIASES.get(stat_code, [])
    pats = [_term_to_pat(x) for x in terms if x]
    pats = [p for p in pats if p]
    if not pats:
        return r"(?:\b__NO_MATCH__\b)"
    return r"(?:%s)" % "|".join(pats)

# 캐시(컴파일 비용 절감)
_STAT_REGEX_CACHE: dict[str, list[re.Pattern]] = {}

def _compile_stat_regexes(stat_code: str) -> list[re.Pattern]:
    if stat_code in _STAT_REGEX_CACHE:
        return _STAT_REGEX_CACHE[stat_code]

    alt = _alt_pat(stat_code)

    # 숫자 퍼센트: 120% / 120.5%
    num_pct = r"(?P<pct>\d+(?:\.\d+)?)\s*%"

    # 숫자 배수: 1.2x / 1.2× / 1.2 *  (ATK/DEF/HP)
    num_mul = r"(?P<mul>\d+(?:\.\d+)?)\s*(?:x|×|\*)"

    # “자신/시전자/사용자” 수식어(영/한 혼합)
    owner = r"(?:the\s+)?(?:caster's|user's|own|self|자신의|사용자의|시전자의)?\s*"

    regs: list[re.Pattern] = []

    # 1) "Deals damage equal to 120% attack power" / "120% of ATK"
    regs.append(re.compile(
        rf"{num_pct}\s*(?:of\s*)?{owner}\s*{alt}\b",
        flags=re.I
    ))

    # 2) "attack power 120%" (퍼센트가 뒤에 붙는 영어/혼합 표기)
    regs.append(re.compile(
        rf"{owner}\s*{alt}\s*[:\-]?\s*{num_pct}",
        flags=re.I
    ))

    # 3) 한국어: "공격력의 120%" / "방어력의 80%"
    regs.append(re.compile(
        rf"{alt}\s*(?:의|기준)\s*(?P<pct>\d+(?:\.\d+)?)\s*%",
        flags=re.I
    ))

    # 4) "1.2x ATK" / "1.2× DEF"
    regs.append(re.compile(
        rf"{num_mul}\s*{alt}\b",
        flags=re.I
    ))

    # 5) "ATK x1.2" / "DEF×1.2"
    regs.append(re.compile(
        rf"{alt}\s*(?:x|×|\*)\s*(?P<mul>\d+(?:\.\d+)?)\b",
        flags=re.I
    ))

    # 6) 한국어 배수: "공격력 1.2배" / "방어력의 1.2배"
    regs.append(re.compile(
        rf"{alt}\s*(?:의\s*)?(?P<mul>\d+(?:\.\d+)?)\s*배",
        flags=re.I
    ))
    regs.append(re.compile(
        rf"(?P<mul>\d+(?:\.\d+)?)\s*배\s*{alt}\b",
        flags=re.I
    ))

    _STAT_REGEX_CACHE[stat_code] = regs
    return regs

def _stat_hits(text: str, stat_code: str) -> list[float]:
    """
    텍스트에서 stat_code(ATK/HP/DEF/CRIT_RATE/CRIT_DMG)와 결합된
    스케일(%) 혹은 배수(x/배)를 찾아서 '퍼센트' 기준으로 반환.

    - 120% -> 120.0
    - 1.2x / 1.2배 -> 120.0 로 환산
    """
    if not text:
        return []
    t = text.strip()
    if not t:
        return []

    regs = _compile_stat_regexes(stat_code)
    hits: list[float] = []

    for rgx in regs:
        for m in rgx.finditer(t):
            try:
                if m.groupdict().get("pct") is not None:
                    hits.append(float(m.group("pct")))
                elif m.groupdict().get("mul") is not None:
                    hits.append(float(m.group("mul")) * 100.0)
            except Exception:
                pass

    return hits



def detect_no_crit(detail: dict) -> bool:
    """
    '크리티컬 비활성/불가' 탐지 강화.

    우선순위:
    1) 명시적 플래그( noCrit / cannotCrit / canCrit=false )
    2) stats에서 critRate & critDmg가 둘 다 0으로 명시된 경우(과탐 방지)
    3) 스킬 텍스트에 "cannot crit/치명타 불가" 문구가 있는 경우
    """
    if not isinstance(detail, dict):
        return False

    # 1) 명시적 플래그(상위 키)
    for k in ["noCrit", "no_crit", "cannotCrit", "cannot_crit", "critDisabled", "crit_disabled"]:
        v = detail.get(k)
        if v is True:
            return True

    for k in ["canCrit", "can_crit", "criticalEnabled", "critical_enabled"]:
        v = detail.get(k)
        if isinstance(v, bool) and v is False:
            return True

    st = _extract_canonical_stats(detail)
    can_crit = st.get("CAN_CRIT")
    if isinstance(can_crit, bool) and can_crit is False:
        return True

    def to_num(x):
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            try:
                return float(x.strip())
            except Exception:
                return None
        return None

    cr = to_num(st.get("CRIT_RATE"))
    cd = to_num(st.get("CRIT_DMG"))

    # 2) crit 관련 키가 실제로 존재했고, 둘 다 0이면 no-crit로 판정
    if (st.get("CRIT_RATE") is not None or st.get("CRIT_DMG") is not None) and (cr is not None and cd is not None):
        if cr <= 0 and cd <= 0:
            return True

    # 3) 스킬 문구 기반
    pairs = _collect_skill_texts(detail)
    blob = "\n".join([txt.lower() for _, txt in pairs])
    if any(k in blob for k in _KW_NO_CRIT):
        return True

    return False



def _detect_profile(detail: dict, base: dict) -> dict:
    """
    스킬/스탯 문구를 기반으로 스케일링(ATK/HP/DEF)과 전투 아키타입(dps/tank/healer/debuffer)을 추정한다.
    - skills/teamSkill 구조가 흔들려도 최대한 텍스트를 수집
    - 스킬 타입(ultimate/auto/normal/passive/team)에 따라 가중치 반영
    """
    pairs = _collect_skill_texts(detail or {})
    texts_only = [t for _, t in pairs]

    atk_hits: list[float] = []
    hp_hits: list[float] = []
    def_hits: list[float] = []

    heal_cnt = shield_cnt = dot_cnt = extra_cnt = ult_cnt = 0

    sample = {"ATK": None, "HP": None, "DEF": None}

    def weighted_extend(target: list[float], values: list[float], w: float):
        if not values:
            return
        for v in values:
            # hits 자체에 가중치 반영(빈도 + 평균 방식이 자연스럽게 커짐)
            target.append(v * w)

    for typ, t in pairs:
        w = _SKILL_TYPE_WEIGHT.get(typ, 1.0)

        a = _pct_hits(t, _SCALE_KEYS["ATK"])
        h = _pct_hits(t, _SCALE_KEYS["HP"])
        d = _pct_hits(t, _SCALE_KEYS["DEF"])

        if a and sample["ATK"] is None:
            sample["ATK"] = t
        if h and sample["HP"] is None:
            sample["HP"] = t
        if d and sample["DEF"] is None:
            sample["DEF"] = t

        weighted_extend(atk_hits, a, w)
        weighted_extend(hp_hits, h, w)
        weighted_extend(def_hits, d, w)

        tl = t.lower()
        if any(k in tl for k in _KW_HEAL):
            heal_cnt += 1
        if any(k in tl for k in _KW_SHIELD):
            shield_cnt += 1
        if any(k in tl for k in _KW_DOT):
            dot_cnt += 1
        if any(k in tl for k in _KW_EXTRA):
            extra_cnt += 1
        if typ == "ultimate" or any(k in tl for k in _KW_ULT):
            ult_cnt += 1

    def score(hits: list[float]) -> float:
        if not hits:
            return 0.0
        return len(hits) * 10.0 + (sum(hits) / len(hits))

    atk_s = score(atk_hits)
    hp_s = score(hp_hits)
    def_s = score(def_hits)

    best = max(atk_s, hp_s, def_s)
    if best <= 0:
        scaling = "MIX"
    else:
        scaling = "ATK" if best == atk_s else ("HP" if best == hp_s else "DEF")

    cls = str(base.get("class") or "-").strip()
    role = str(base.get("role") or "-").strip()
    cls_l = cls.lower()
    role_l = role.lower()

    # class/role 한글/변형도 커버
    def is_healer(s: str) -> bool:
        return any(x in s for x in ["healer", "heal", "힐러", "치유", "회복"])

    def is_tank(s: str) -> bool:
        return any(x in s for x in ["guardian", "tank", "defender", "탱", "탱커", "수호", "가디언", "방어"])

    def is_debuffer(s: str) -> bool:
        return any(x in s for x in ["debuffer", "debuff", "서포트", "지원", "약화", "디버프"])

    if is_healer(cls_l) or is_healer(role_l):
        archetype = "healer"
    elif is_tank(cls_l) or is_tank(role_l):
        archetype = "tank"
    elif is_debuffer(cls_l) or is_debuffer(role_l):
        archetype = "debuffer"
    else:
        archetype = "dps"

    # healer hybrid: healer지만 ATK 스케일이 강하게 잡히는 경우
    healer_hybrid = bool(archetype == "healer" and atk_s >= 15.0 and (atk_s >= hp_s or atk_s >= def_s))

    # ✅ DEF/HP 스케일링이 확실하면 dps라도 tank로 승격(과소분류 방지: Apep 류)
    if archetype == "dps":
        if scaling == "DEF" and def_s > 0 and def_s >= max(atk_s, hp_s) + 5.0:
            archetype = "tank"
        elif scaling == "HP" and hp_s > 0 and hp_s >= atk_s + 5.0 and shield_cnt > 0:
            archetype = "tank"

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
        "archetype": archetype,
        "healer_hybrid": healer_hybrid,
        "sample_text": sample.get(scaling) if scaling in sample else None,
        "skill_text_count": len(texts_only),
    }

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
        if scaling == "DEF":
            plan["4"] = ["Defense (%)", "HP (%)"]
            plan["5"] = ["Defense (%)", "HP (%)"]
            plan["6"] = ["Defense (%)", "HP (%)"]
        else:
            plan["4"] = ["HP (%)", "Defense (%)"]
            plan["5"] = ["HP (%)", "Defense (%)"]
            plan["6"] = ["HP (%)", "Defense (%)"]
        return plan

    # dps/debuffer
    if no_crit:
        plan["4"] = ["Attack Penetration (%)", "Attack (%)", "HP (%) (생존)"]
        plan["5"] = [_element_damage_label(element), "Attack (%)", "HP (%) (생존)"]
        plan["6"] = ["Attack (%)", "HP (%) (생존)", "Defense (%) (생존)"]
    else:
        # DEF/HP 스케일링 딜러라도 '크리 가능'이면 치확이 의미 있을 수 있어 기본은 유지
        # (다만 Apep 같은 DEF 탱커는 위 archetype 보정으로 여기로 내려오지 않게 한다)
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


def _pick_sets(profile: dict, base: dict, no_crit: bool, detail: dict = None) -> tuple[list[dict], list[list[dict]], list[str]]:
    """
    ✅ 모든 룬을 대상으로 4pc 후보 + 2pc 후보를 전수 평가.
    - 점수는 2pc 효과 + 4pc 효과를 모두 반영
    - 제한시간 버스트 컨텐츠를 기본 가정(burst_window_s=20). 필요 시 조정 가능.
    """
    rationale: list[str] = []
    alternates: list[list[dict]] = []

    rune_db = rune_db_by_name()
    enriched = rune_effects_enriched(rune_db)

    # 버스트 시간(제한시간 내 최고딜) 기본값
    burst_window_s = 20.0

    # 4pc 가능한 룬만
    candidates_4 = []
    candidates_2 = []

    for name, r in enriched.items():
        # 2pc/4pc 텍스트가 비어있지 않으면 후보
        if str(r.get("twoPiece") or "").strip():
            candidates_2.append(name)
        if str(r.get("fourPiece") or "").strip():
            candidates_4.append(name)

    if not candidates_4:
        return [{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}], alternates, ["4세트 후보가 없어 기본값 적용"]

    best = None  # (score, set4, set2)
    scored_list = []

    for s4 in candidates_4:
        e4 = enriched[s4]["_four"]
        e4_score = score_rune_piece(e4, profile, detail or {}, no_crit, burst_window_s=burst_window_s)
        # 4세트는 “2pc도 동시에 적용될 수 있음” 가정(게임 룰에 따라 다르면 조정)
        # 만약 4세트 장착 시 2pc도 같이 활성화라면, 아래처럼 2pc 점수도 더하는 것이 합리적
        e4_score += score_rune_piece(enriched[s4]["_two"], profile, detail or {}, no_crit, burst_window_s=burst_window_s)

        for s2 in candidates_2:
            if s2 == s4:
                continue
            e2 = enriched[s2]["_two"]
            e2_score = score_rune_piece(e2, profile, detail or {}, no_crit, burst_window_s=burst_window_s)

            total = e4_score + e2_score
            scored_list.append((total, s4, s2))

    scored_list.sort(reverse=True, key=lambda x: x[0])

    if not scored_list:
        return [{"set": candidates_4[0], "pieces": 4}, {"set": candidates_2[0] if candidates_2 else candidates_4[0], "pieces": 2}], alternates, ["후보 평가 실패로 임의 선택"]

    best_total, best4, best2 = scored_list[0]

    primary = [{"set": best4, "pieces": 4}, {"set": best2, "pieces": 2}]

    # 대체안(상위 2~4개)
    for i in range(1, min(4, len(scored_list))):
        _, a4, a2 = scored_list[i]
        alternates.append([{"set": a4, "pieces": 4}, {"set": a2, "pieces": 2}])

    # 근거(요약)
    rationale.append(f"버스트(제한시간) 기대값 점수 기반 전수평가: 1위 {best4}4 + {best2}2 (score={best_total:.4f})")
    rationale.append(f"판정 요약: scaling={profile.get('scaling')}, no_crit={no_crit}, extra_cnt={profile.get('extra_cnt')}, dot_cnt={profile.get('dot_cnt')}, ult_cnt={profile.get('ult_cnt')}")

    sample_text = profile.get("sample_text")
    if sample_text:
        rationale.append(f"스케일링 근거 문구: '{sample_text[:120]}'")

    if no_crit:
        rationale.append("크리티컬 불가/0 탐지 → 치확/치피 관련 효과는 점수에서 자동 무효 처리.")

    return primary, alternates, rationale

def recommend_runes(cid: str, base: dict, detail: dict) -> dict:
    overrides = load_rune_overrides()
    rune_db = rune_db_by_name()

    # manual override 우선
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

    # auto
    profile = _detect_profile(detail or {}, base or {})
    no_crit = detect_no_crit(detail or {})
    primary, alternates, rationale = _pick_sets(profile, base or {}, no_crit)

    sample_text = profile.get("sample_text")
    if sample_text:
        rationale = rationale + [f"스케일링 판정({profile.get('scaling')}): '{sample_text[:120]}'"]

    if no_crit:
        rationale = rationale + ["크리티컬 비활성/불가로 탐지됨 → 치확/치피 추천을 제외."]

    def mk_build(title: str, setplan: list[dict]) -> dict:
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
        return {
            "title": title,
            "setPlan": sp,
            "slots": _slot_plan_for(profile["archetype"], profile["scaling"], base.get("element"), no_crit),
            "substats": _substats_for(profile["archetype"], profile["scaling"], no_crit),
            "notes": [],
            "rationale": rationale,
        }

    builds = [mk_build("추천(자동)", primary)]
    for idx, alt in enumerate(alternates[:3], start=1):
        builds.append(mk_build(f"대체안 {idx}", alt))

    # rune DB 기반 제약/노트 표기
    notes = []
    for b in builds:
        for s in b["setPlan"]:
            nm = s["set"]
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
    # 리스트에는 가볍게 세트만 노출(효과는 상세에서)
    return {"mode": reco.get("mode"), "sets": [{"set": s.get("set"), "pieces": s.get("pieces"), "icon": s.get("icon")} for s in (b0.get("setPlan") or [])]}




# -------------------------
# Party recommendation (AI)
# -------------------------

def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if not s:
            return None
        # "12.3%" -> 12.3
        if s.endswith("%"):
            s = s[:-1].strip()
        try:
            return float(s)
        except Exception:
            return None
    return None


def _rarity_weight(r: str) -> float:
    rr = (r or "-").strip().upper()
    if rr == "SSR":
        return 3.0
    if rr == "SR":
        return 1.5
    if rr == "R":
        return 0.5
    return 0.0


def _rank_weight(rank_map: dict, cid: str) -> float:
    try:
        v = rank_map.get(cid)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.strip():
            return float(v)
    except Exception:
        pass
    return 0.0


def _norm_class_name(s: str) -> str:
    return (s or "-").strip().lower().replace(" ", "")


def _party_combo_bonus(members: list[dict]) -> tuple[float, dict]:
    """원소/특성 콤보 보너스 (같은 속성 2+ 또는 같은 특성 2+)."""
    from collections import Counter

    elems = [str(m.get("element") or "-") for m in members]
    facs = [str(m.get("faction") or "-") for m in members]

    ce = Counter([e for e in elems if e and e != "-"])
    cf = Counter([f for f in facs if f and f != "-"])

    bonus = 0.0
    detail = {"element": dict(ce), "faction": dict(cf), "element_hits": [], "faction_hits": []}

    for e, n in ce.items():
        if n >= 2:
            bonus += 12.0
            detail["element_hits"].append(e)
        if n >= 3:
            bonus += 6.0
    for f, n in cf.items():
        if n >= 2:
            bonus += 12.0
            detail["faction_hits"].append(f)
        if n >= 3:
            bonus += 6.0

    return bonus, detail


def _score_member(base: dict, detail: dict, rank_map: dict) -> dict:
    cid = str(base.get("id") or "")
    profile = _detect_profile(detail or {}, base or {})
    no_crit = detect_no_crit(detail or {})

    st = _extract_canonical_stats(detail or {})
    hp = _to_float(st.get("HP")) or 0.0
    atk = _to_float(st.get("ATK")) or 0.0
    df = _to_float(st.get("DEF")) or 0.0
    cr = _to_float(st.get("CRIT_RATE")) or 0.0

    # stats score: 규모 보정(대략적인 밸런스용)
    stats_score = (atk / 55.0) + (hp / 650.0) + (df / 35.0) + (cr / 4.0)

    # archetype bias (과도한 힐/탱 몰림 방지: 딜러 가중 강화)
    arch = profile.get("archetype") or "dps"
    arch_bonus = 0.0

    heal_cnt = float(profile.get("heal_cnt") or 0)
    shield_cnt = float(profile.get("shield_cnt") or 0)

    if arch == "healer":
        arch_bonus += 10.0 + min(8.0, heal_cnt * 1.5)
    elif arch == "tank":
        arch_bonus += 10.0 + min(8.0, shield_cnt * 1.5)
    elif arch == "debuffer":
        arch_bonus += 8.0
    else:
        arch_bonus += 14.0

    # scaling 보정: ATK 스케일은 딜 기여로 우선
    scaling = (profile.get("scaling") or "").upper()
    if scaling == "ATK":
        arch_bonus += 3.0
    elif scaling == "DEF":
        arch_bonus += 1.5
    elif scaling == "HP":
        arch_bonus += 1.0

    if no_crit:
        # 크리 불가 캐릭터는 치확 위주 티어를 과대평가하지 않게 소폭 페널티
        arch_bonus -= 4.0

    tier = _rank_weight(rank_map, cid)

    score = 0.0
    score += tier * 110.0
    score += _rarity_weight(str(base.get("rarity") or "-")) * 18.0
    score += stats_score
    score += arch_bonus

    # 스킬 키워드 가점(대략)
    score += min(8.0, float(profile.get("dot_cnt") or 0) * 2.0)
    score += min(8.0, float(profile.get("extra_cnt") or 0) * 2.0)
    score += min(6.0, float(profile.get("ult_cnt") or 0) * 1.5)

    return {
        "id": cid,
        "base": base,
        "profile": profile,
        "no_crit": no_crit,
        "stats": {"hp": hp, "attack": atk, "defense": df, "critRate": cr},
        "tier": tier,
        "score": score,
    }


def _score_party(members: list[dict], required_classes: set[str], require_combo: bool) -> tuple[float, dict]:
    """파티 점수: 개별 점수 합 + 역할 밸런스 + 콤보(속성/특성)"""
    total = sum(float(m.get("score") or 0.0) for m in members)

    counts = {"tank": 0, "healer": 0, "debuffer": 0, "dps": 0}
    for m in members:
        arch = (m.get("profile") or {}).get("archetype") or "dps"
        if arch in counts:
            counts[arch] += 1
        else:
            counts["dps"] += 1

    # 밸런스 보정: 최소 1 딜러를 강제에 가깝게 유도
    balance = 0.0

    has_dps = counts["dps"] >= 1
    if has_dps:
        balance += 18.0
        if counts["dps"] >= 2:
            balance += 10.0
    else:
        # 딜러가 없으면 사실상 파티로서 성립이 어려우므로 큰 페널티(필터링에도 사용)
        balance -= 220.0

    if counts["tank"] == 1:
        balance += 8.0
    elif counts["tank"] > 1:
        balance -= 25.0 * (counts["tank"] - 1)

    if counts["healer"] == 1:
        balance += 10.0
    elif counts["healer"] > 1:
        balance -= 25.0 * (counts["healer"] - 1)
    else:
        # 힐러가 0이면 안정성 감소(완전 배제는 아님)
        balance -= 6.0

    if counts["debuffer"] >= 1:
        balance += 5.0
    if counts["debuffer"] > 1:
        balance -= 6.0 * (counts["debuffer"] - 1)

    total += balance

    combo_detail = {}
    combo_bonus = 0.0
    if True:
        combo_bonus, combo_detail = _party_combo_bonus([m["base"] for m in members])
        total += combo_bonus

    # 클래스 조건 충족 확인 (필수)
    classes_present = set(_norm_class_name(str(m["base"].get("class") or "-")) for m in members)
    missing_classes = sorted([c for c in required_classes if c and c not in classes_present])

    ok_classes = len(missing_classes) == 0

    # 콤보 요구(최소 1개라도 2+가 있어야 함)
    has_combo = True
    if require_combo:
        has_combo = bool(combo_detail.get("element_hits") or combo_detail.get("faction_hits"))

    meta = {
        "counts": counts,
        "has_dps": has_dps,
        "balance_bonus": balance,
        "combo_bonus": combo_bonus,
        "combo_detail": combo_detail,
        "classes_present": sorted(list(classes_present)),
        "missing_classes": missing_classes,
        "ok_classes": ok_classes,
        "has_combo": has_combo,
    }
    return total, meta


def recommend_best_party(
    owned_ids: list[str],
    required_ids: list[str],
    required_classes: list[str],
    rank_map: dict,
    party_size: int = 4,
    top_k: int = 1,
    require_combo: bool = True,
) -> dict:
    load_all()

    owned = [slug_id(x) for x in (owned_ids or []) if slug_id(x)]
    required = [slug_id(x) for x in (required_ids or []) if slug_id(x)]
    required = [x for x in required if x in owned]  # required는 owned 내부로 강제

    if party_size < 1:
        party_size = 4
    party_size = min(6, max(1, int(party_size)))

    if len(required) > party_size:
        return {"ok": False, "error": f"필수 포함 캐릭터가 {len(required)}명입니다. 파티 인원({party_size})을 초과합니다."}

    # required_classes 정규화
    req_cls = set()
    for c in (required_classes or []):
        cc = _norm_class_name(str(c))
        if cc and cc != "-":
            req_cls.add(cc)
    if len(req_cls) > party_size:
        return {"ok": False, "error": f"클래스 조건이 {len(req_cls)}개입니다. 파티 인원({party_size})을 초과합니다."}

    # base/detail 확보
    by_id = {c.get("id"): c for c in (CACHE.get("chars") or []) if isinstance(c, dict) and c.get("id")}
    details = CACHE.get("details") or {}

    # 후보 구성
    fixed_members = []
    for cid in required:
        b = by_id.get(cid)
        d = details.get(cid)
        if isinstance(b, dict) and isinstance(d, dict):
            fixed_members.append(_score_member(b, d, rank_map))

    fixed_ids = set(m["id"] for m in fixed_members)

    cand_members = []
    for cid in owned:
        if cid in fixed_ids:
            continue
        b = by_id.get(cid)
        d = details.get(cid)
        if isinstance(b, dict) and isinstance(d, dict):
            cand_members.append(_score_member(b, d, rank_map))

    # 후보 정렬 및 제한(조합 폭발 방지)
    cand_members.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    cand_members = cand_members[:28]  # 충분히 넓게, 그래도 안전

    need = party_size - len(fixed_members)
    if need <= 0:
        total, meta = _score_party(fixed_members, req_cls, require_combo)

        # ✅ 기본 최적 파티 룰 동일 적용 (필수만으로 완성된 경우도 예외 없음)
        cnt = meta.get("counts") or {}
        caps_ok = (cnt.get("dps") or 0) >= 1 and (cnt.get("healer") or 0) <= 1 and (cnt.get("tank") or 0) <= 1

        ok = bool(meta.get("ok_classes") and meta.get("has_combo") and caps_ok)

        note = "필수 포함만으로 파티가 완성되었습니다."
        if not caps_ok:
            note += " (역할 밸런스: 딜러 1+ / 힐러 1 / 탱커 1 규칙을 만족하지 못했습니다)"

        return {
            "ok": bool(ok),
            "party_size": party_size,
            "evaluated": 1,
            "party": fixed_members,
            "total_score": total,
            "meta": meta,
            "note": note,
        }

    if len(cand_members) < need:
        return {"ok": False, "error": f"후보가 부족합니다. (필수 제외 후 후보 {len(cand_members)}명, 필요 {need}명)"}

    import itertools

    best: list[tuple[float, list[dict], dict]] = []
    evaluated = 0

    for comb in itertools.combinations(cand_members, need):
        evaluated += 1
        members = fixed_members + list(comb)

        total, meta = _score_party(members, req_cls, require_combo)

        # ✅ 기본 최적 파티 룰(일반 PVE/PVP 공용): 딜러 최소 1, 탱커/힐러는 과투입 방지
        cnt = meta.get("counts") or {}
        if (cnt.get("dps") or 0) < 1:
            continue
        if (cnt.get("healer") or 0) > 1:
            continue
        if (cnt.get("tank") or 0) > 1:
            continue
        # ✅ 클래스 기반 딜러(Warrior/Rogue/Mage) 최소 1, Buffer 과투입(2+) 방지
        class_list = [str(((x.get("base") or {}).get("class")) or "").strip() for x in members]
        role_list = [str(((x.get("base") or {}).get("role")) or "").strip() for x in members]
        class_cnt = Counter([c.lower() for c in class_list if c])
        dps_class_cnt = sum(1 for c in class_list if str(c).strip().lower() in ("warrior","rogue","mage"))
        buffer_class_cnt = class_cnt.get("buffer", 0)

        # meta에 기록(프론트 표시/디버그용)
        meta["class_counts"] = dict(class_cnt)
        meta["dps_class_count"] = dps_class_cnt
        meta["buffer_class_count"] = buffer_class_cnt

        # 기본 룰: 버퍼 2명 이상은 제외, 딜러 클래스를 최소 1명 포함
        if buffer_class_cnt > 1:
            continue
        if dps_class_cnt < 1:
            continue

        # 필수 조건 체크
        if not meta.get("ok_classes"):
            continue
        if require_combo and not meta.get("has_combo"):
            continue

        best.append((total, members, meta))

    if not best:
        # 조건 때문에 공집합이면, 콤보 조건만 완화한 대체안 제공(클래스는 유지)
        for comb in itertools.combinations(cand_members, need):
            evaluated += 1
            members = fixed_members + list(comb)
            total, meta = _score_party(members, req_cls, require_combo=False)
            # ✅ 기본 최적 파티 룰 유지 (fallback에서도 과도한 힐/탱 방지)
            cnt = meta.get("counts") or {}
            if (cnt.get("dps") or 0) < 1:
                continue
            if (cnt.get("healer") or 0) > 1:
                continue
            if (cnt.get("tank") or 0) > 1:
                continue
            # ✅ 클래스 기반 딜러 최소 1 + Buffer 과투입 방지 (fallback에서도 유지)
            class_list = [str(((x.get("base") or {}).get("class")) or "").strip() for x in members]
            role_list = [str(((x.get("base") or {}).get("role")) or "").strip() for x in members]
            class_cnt = Counter([c.lower() for c in class_list if c])
            dps_class_cnt = sum(1 for c in class_list if str(c).strip().lower() in ("warrior","rogue","mage"))
            buffer_class_cnt = class_cnt.get("buffer", 0)

            meta["class_counts"] = dict(class_cnt)
            meta["dps_class_count"] = dps_class_cnt
            meta["buffer_class_count"] = buffer_class_cnt

            if buffer_class_cnt > 1:
                continue
            if dps_class_cnt < 1:
                continue
            if not meta.get("ok_classes"):
                continue
            best.append((total, members, meta))
        if not best:
            return {"ok": False, "error": "조건을 만족하는 파티를 찾지 못했습니다. (필수/클래스/후보를 확인해주세요)", "evaluated": evaluated}

    best.sort(key=lambda x: x[0], reverse=True)
    top = best[: max(1, int(top_k))]

    def pack(members: list[dict], total: float, meta: dict) -> dict:
        return {
            "total_score": total,
            "meta": meta,
            "members": [
                {
                    **(mm.get("base") or {}),
                    "score": float(mm.get("score") or 0.0),
                    "tier": float(mm.get("tier") or 0.0),
                    "archetype": (mm.get("profile") or {}).get("archetype"),
                    "scaling": (mm.get("profile") or {}).get("scaling"),
                    "no_crit": bool(mm.get("no_crit")),
                }
                for mm in sorted(members, key=lambda x: float(x.get("score") or 0.0), reverse=True)
            ],
        }

    out_parties = [pack(mems, total, meta) for (total, mems, meta) in top]

    return {
        "ok": True,
        "party_size": party_size,
        "evaluated": evaluated,
        "required": required,
        "required_classes": sorted(list(req_cls)),
        "require_combo": bool(require_combo),
        "parties": out_parties,
    }

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
