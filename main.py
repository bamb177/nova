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
# Rune recommendation logic (C) — exhaustive scoring across all rune sets
# -------------------------

# Keyword buckets (EN + KO) — use broad matching to survive translation variance
_KW_HEAL = ["heal", "healing", "restore", "recovery", "regen", "회복", "치유", "힐", "재생", "회복량"]
_KW_SHIELD = ["shield", "barrier", "guard", "protect", "보호막", "실드", "방벽"]
_KW_DOT = ["continuous damage", "damage over time", "dot", "burn", "bleed", "poison", "지속", "지속 피해", "도트", "화상", "중독", "출혈"]
_KW_EXTRA = ["extra attack", "follow-up", "follow up", "additional attack", "추가 공격", "추격", "연속 공격", "추가타"]
_KW_ULT = ["ultimate", "ult", "burst", "궁극기", "필살기"]
_KW_CRIT_OFF = ["cannot critically", "can't critically", "cannot crit", "can't crit", "does not crit", "no critical",
                "크리티컬 불가", "치명타 불가", "치명타가 발생하지", "크리티컬이 발생하지", "치명타가 발생하지 않"]


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
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _get_first(d: dict, keys: list[str]):
    for k in keys:
        if k in d:
            return d.get(k)
    return None


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if not m:
            return None
        try:
            return float(m.group(0))
        except Exception:
            return None
    return None


def _normalize_stats(detail: dict) -> dict[str, Optional[float]]:
    """
    JSON 스키마 편차 흡수:
    - stats / stat / attributes / attribute / baseStats / base_stats 등
    - hp / health / maxHp / maxHP / atk / attack / attackPower / def / defense / critRate 등
    """
    stats_obj = None
    for key in ["stats", "stat", "attributes", "attribute", "baseStats", "base_stats", "base", "status"]:
        v = detail.get(key)
        if isinstance(v, (dict, list)):
            stats_obj = v
            break

    out = {"hp": None, "attack": None, "defense": None, "critRate": None}

    # dict 형태
    if isinstance(stats_obj, dict):
        hp = _get_first(stats_obj, ["hp", "HP", "health", "Health", "maxHp", "maxHP", "MaxHP", "max_hp"])
        atk = _get_first(stats_obj, ["attack", "Attack", "atk", "ATK", "attackPower", "attack_power", "atkPower", "atk_power"])
        de = _get_first(stats_obj, ["defense", "Defense", "def", "DEF", "defence", "Defence"])
        cr = _get_first(stats_obj, ["critRate", "crit_rate", "crit", "criticalRate", "critical_rate", "치명타", "치확", "크리티컬"])
        out["hp"] = _as_float(hp)
        out["attack"] = _as_float(atk)
        out["defense"] = _as_float(de)
        out["critRate"] = _as_float(cr)

    # list 형태: [{"name":"HP","value":"7,711"}, ...] 같은 케이스
    if isinstance(stats_obj, list):
        for row in stats_obj:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("stat") or row.get("key") or "").strip().lower()
            val = row.get("value")
            if any(k in name for k in ["hp", "health", "max hp", "체력", "생명"]):
                out["hp"] = out["hp"] or _as_float(val)
            elif any(k in name for k in ["attack", "atk", "공격"]):
                out["attack"] = out["attack"] or _as_float(val)
            elif any(k in name for k in ["defense", "def", "방어"]):
                out["defense"] = out["defense"] or _as_float(val)
            elif any(k in name for k in ["crit", "critical", "치명", "크리"]):
                out["critRate"] = out["critRate"] or _as_float(val)

    return out


def _skill_nodes(detail: dict) -> list[dict]:
    """
    skills 구조 편차 흡수:
    - skills
    - skill / ability / abilities
    """
    for key in ["skills", "skill", "abilities", "ability"]:
        v = detail.get(key)
        if isinstance(v, dict):
            return [v]
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _extract_skill_texts(detail: dict) -> list[str]:
    """
    normal / auto / ultimate / passive 뿐 아니라 다양한 키를 넓게 수용.
    """
    texts: list[str] = []
    for node in _skill_nodes(detail):
        # 흔한 nested keys
        for k in [
            "normal", "basic", "basicAttack", "basic_attack", "normalAttack", "normal_attack",
            "auto", "autoAttack", "auto_attack",
            "skill1", "skill_1", "s1", "active1", "active_1",
            "skill2", "skill_2", "s2", "active2", "active_2",
            "skill3", "skill_3", "s3", "active3", "active_3",
            "ultimate", "ult", "burst", "ultimateSkill", "ultimate_skill",
            "passive", "passive1", "passive_1", "passive2", "passive_2",
        ]:
            v = node.get(k)
            if v is None:
                continue
            texts += _collect_texts(v)

        # skills가 list로 들어온 경우: name/desc
        if isinstance(node.get("list"), list):
            for it in node["list"]:
                texts += _collect_texts(it)

        # 어떤 파일은 skills = {"normal":{...},"auto":{...},...} 대신
        # {"1":{...},"2":{...}} 형태가 있음 → 전수 스캔
        for _, v in node.items():
            texts += _collect_texts(v)

    # 팀스킬/연계/기타
    for k in ["teamSkill", "team_skill", "team", "combo", "comboSkill", "combo_skill", "chain", "chainSkill"]:
        v = detail.get(k)
        if v is not None:
            texts += _collect_texts(v)

    # dedupe
    seen, uniq = set(), []
    for t in texts:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def detect_no_crit(detail: dict) -> bool:
    """
    '기본 치확=0'은 많은 게임에서 정상일 수 있으므로 no-crit로 보지 않는다.
    아래 케이스만 no-crit로 판정:
    - 명시적인 boolean/flag
    - 스킬 설명에 '치명타 불가/크리티컬 불가/cannot crit' 같은 문구가 존재
    """
    if not isinstance(detail, dict):
        return False

    for k in ["noCrit", "no_crit", "cannotCrit", "cannot_crit", "critDisabled", "crit_disabled"]:
        v = detail.get(k)
        if v is True:
            return True

    # deep scan for explicit flags
    def deep_flag(obj) -> bool:
        if isinstance(obj, dict):
            for kk, vv in obj.items():
                kkl = str(kk).lower()
                if any(x in kkl for x in ["nocrit", "cannotcrit", "critdisabled"]):
                    if vv is True:
                        return True
                if deep_flag(vv):
                    return True
        elif isinstance(obj, list):
            for it in obj:
                if deep_flag(it):
                    return True
        return False

    if deep_flag(detail):
        return True

    # phrase scan in skills
    for t in _extract_skill_texts(detail):
        tl = t.lower()
        if any(p in tl for p in _KW_CRIT_OFF):
            return True
    return False


def _pct_hits(text: str, keys: list[str]) -> list[float]:
    """
    스킬 문구에서 스케일링(%) 값을 뽑는다.
    - EN: "Deals 120% attack power"
    - KO: "공격력의 120%"
    - EN alt: "equal to 120% of DEF"
    """
    hits: list[float] = []
    t = text.lower()

    # pattern A: "<num>% ... <key>"
    for k in keys:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*[^%\n]{0,40}\b" + re.escape(k) + r"\b", t):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    # pattern B: "<key> ... <num>%"
    for k in keys:
        for m in re.finditer(r"\b" + re.escape(k) + r"\b[^%\n]{0,40}(\d+(?:\.\d+)?)\s*%", t):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    # pattern C: "공격력의 120%"
    for k in keys:
        for m in re.finditer(re.escape(k) + r"\s*의\s*(\d+(?:\.\d+)?)\s*%", text):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    # pattern D: "120% of ATK/DEF/HP"
    for k in keys:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*of\s*" + re.escape(k), t):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    return hits


def _detect_profile(detail: dict, base: dict) -> dict:
    texts = _extract_skill_texts(detail or {})

    atk_hits: list[float] = []
    hp_hits: list[float] = []
    def_hits: list[float] = []

    heal_cnt = shield_cnt = dot_cnt = extra_cnt = ult_cnt = 0
    extra_lines = dot_lines = 0

    sample = {"ATK": None, "HP": None, "DEF": None}

    for t in texts:
        a = _pct_hits(t, ["attack power", "atk", "attack", "공격력", "공격"])
        h = _pct_hits(t, ["max hp", "hp", "health", "체력", "생명"])
        d = _pct_hits(t, ["defense", "def", "방어력", "방어"])

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
            dot_lines += 1
        if any(k in tl for k in _KW_EXTRA):
            extra_cnt += 1
            extra_lines += 1
        if any(k in tl for k in _KW_ULT):
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

    if cls_l == "healer" or role_l == "healer":
        archetype = "healer"
    elif cls_l == "guardian" or role_l == "tank":
        archetype = "tank"
    elif cls_l == "debuffer" or role_l == "debuffer":
        archetype = "debuffer"
    else:
        archetype = "dps"

    # healer hybrid: healer지만 ATK 스케일이 강하게 잡히는 경우
    healer_hybrid = bool(archetype == "healer" and atk_s >= 15.0 and (atk_s >= hp_s or atk_s >= def_s))

    # "추가공격/지속피해가 얼마나 중심인가"를 0~1 범위로 근사
    extra_share = min(1.0, 0.15 * extra_lines + 0.05 * max(0, extra_cnt - extra_lines))
    dot_share = min(1.0, 0.15 * dot_lines + 0.05 * max(0, dot_cnt - dot_lines))

    stats = _normalize_stats(detail or {})
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
        "stats": stats,
        "extra_share": extra_share,
        "dot_share": dot_share,
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
        plan["4"] = ["Critical Rate (%)", "Attack Penetration (%)", "Critical Damage (%)", "Attack (%)"]
        plan["5"] = [_element_damage_label(element), "Attack (%)", "HP (%)", "Defense (%)"]
        plan["6"] = ["Attack (%)", "HP (%)", "Defense (%)"]

    # 스케일링이 HP/DEF 기반이면 부옵/메인스탯 후보에 더 자주 반영
    if scaling in ("HP", "DEF") and archetype in ("dps", "debuffer"):
        plan["6"] = [f"{scaling} (%)", "Attack (%)", "HP (%)", "Defense (%)"]

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


# -------------------------
# Rune effect parsing & scoring
# -------------------------

def _parse_effect_text(s: str) -> dict[str, float]:
    """
    rune 2pc/4pc 문자열에서 효과를 정규화한다.
    반환 값은 '가중치가 가능한' 단순 스탯 벡터.
    """
    if not isinstance(s, str) or not s.strip():
        return {}
    t = s.strip()
    tl = t.lower()

    eff: dict[str, float] = {}

    def add(k: str, v: float):
        if v == 0:
            return
        eff[k] = eff.get(k, 0.0) + float(v)

    # % 숫자 추출 헬퍼
    def pct_near(keys: list[str]) -> Optional[float]:
        for key in keys:
            # "X +8%" / "X 8%" / "X: ... +8%"
            m = re.search(re.escape(key) + r"[^%\n]{0,24}([+\-]?\d+(?:\.\d+)?)\s*%", tl)
            if m:
                try:
                    return float(m.group(1))
                except Exception:
                    pass
            # "+8% X" 형태
            m = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%\s*[^%\n]{0,24}" + re.escape(key), tl)
            if m:
                try:
                    return float(m.group(1))
                except Exception:
                    pass
        return None

    # Core stats
    v = pct_near(["attack power", "attack", "atk", "공격력"])
    if v is not None:
        add("atk_pct", v)
    v = pct_near(["hp", "health", "max hp", "체력", "생명"])
    if v is not None:
        add("hp_pct", v)
    v = pct_near(["defense", "def", "방어력", "방어"])
    if v is not None:
        add("def_pct", v)

    # Crit
    v = pct_near(["critical hit rate", "crit rate", "critical rate", "치명타 확률", "치명확률", "치확", "크리티컬 확률"])
    if v is not None:
        add("crit_rate", v)
    v = pct_near(["critical hit damage", "crit damage", "critical damage", "치명타 피해", "치피", "크리티컬 피해"])
    if v is not None:
        add("crit_dmg", v)

    # Healing / Shield
    v = pct_near(["healing effectiveness", "healing", "치유", "회복", "회복량", "치유량"])
    if v is not None:
        add("heal_eff", v)
    v = pct_near(["shield effectiveness", "shield", "보호막", "실드", "방벽"])
    if v is not None:
        add("shield_eff", v)

    # Extra / DOT
    v = pct_near(["extra attack", "follow-up", "additional attack", "추가 공격", "추가공격", "추가타"])
    if v is not None:
        add("extra_dmg", v)
    v = pct_near(["continuous damage", "dot", "지속 피해", "지속피해", "도트"])
    if v is not None:
        add("dot_dmg", v)

    # Basic attack damage
    v = pct_near(["basic attack damage", "basic damage", "기본 공격 피해", "평타 피해"])
    if v is not None:
        add("basic_dmg", v)

    # Team damage
    v = pct_near(["team damage", "party damage", "team dmg", "파티 피해", "팀 피해", "아군 피해"])
    if v is not None:
        add("team_dmg", v)

    # Attack penetration
    v = pct_near(["attack penetration", "penetration", "관통"])
    if v is not None:
        add("pen", v)

    # Energy start (flat bonus → score as fixed utility)
    if "gain 1 energy" in tl or "에너지" in t and ("즉시" in t or "전투 시작" in t):
        add("energy", 1.0)

    # Conditional like "When HP >80%: Crit dmg +24%" already covered by crit_dmg; apply mild condition penalty later
    if "when hp" in tl or "hp >" in tl or "체력" in t and ("이상" in t or "초과" in t):
        add("conditional", 1.0)

    return eff


def _set_effects(rune: dict, pieces: int) -> dict[str, float]:
    if not isinstance(rune, dict):
        return {}
    if pieces == 2:
        return _parse_effect_text(str(rune.get("twoPiece") or ""))
    return _parse_effect_text(str(rune.get("fourPiece") or ""))


def _score_effect_vec(profile: dict, eff: dict[str, float], pieces: int, no_crit: bool) -> float:
    """
    프로필 기반 유틸리티 점수(상대 비교용).
    - 절대치가 아니라, 세트 간 상대 순위가 안정적으로 나오도록 설계.
    """
    if not eff:
        return 0.0

    archetype = profile.get("archetype") or "dps"
    scaling = profile.get("scaling") or "MIX"
    extra_share = float(profile.get("extra_share") or 0.0)
    dot_share = float(profile.get("dot_share") or 0.0)

    # weight base
    w_atk = 1.0
    w_hp = 0.6
    w_def = 0.6
    w_crit_r = 1.0
    w_crit_d = 0.9
    w_pen = 0.8
    w_elem = 0.7
    w_team = 0.6
    w_basic = 0.5
    w_extra = 1.0
    w_dot = 1.0
    w_heal = 1.0
    w_shield = 0.9
    w_energy = 0.7

    if archetype == "healer":
        w_heal = 1.4
        w_hp = 1.0
        w_def = 0.8
        w_atk = 0.35  # 기본은 낮게, 하이브리드는 아래에서 보정
        w_crit_r = 0.2
        w_crit_d = 0.15
        w_team = 0.6
        w_energy = 0.9

    if archetype == "tank":
        w_hp = 1.2
        w_def = 1.2
        w_shield = 1.2
        w_atk = 0.2
        w_crit_r = 0.1
        w_crit_d = 0.1
        w_pen = 0.1
        w_team = 0.2

    if archetype == "debuffer":
        w_team = 1.0
        w_energy = 0.9
        w_atk = 0.6
        w_pen = 0.5

    # scaling emphasis
    if scaling == "HP":
        w_hp *= 1.5
        w_atk *= 0.6
    elif scaling == "DEF":
        w_def *= 1.5
        w_atk *= 0.6

    # hybrid healer boost
    if archetype == "healer" and profile.get("healer_hybrid"):
        w_atk *= 1.25
        w_crit_r *= 0.8
        w_crit_d *= 0.8

    if no_crit:
        w_crit_r = 0.0
        w_crit_d = 0.0

    # conditional penalty (HP>80 등) — 시간제한 딜에서는 유지가 어려울 수 있어 감점
    cond = 0.0
    if eff.get("conditional"):
        cond = 0.92  # 약 8% 패널티
    else:
        cond = 1.0

    # score
    score = 0.0
    score += w_atk * eff.get("atk_pct", 0.0)
    score += w_hp * eff.get("hp_pct", 0.0)
    score += w_def * eff.get("def_pct", 0.0)
    score += w_crit_r * eff.get("crit_rate", 0.0)
    score += w_crit_d * eff.get("crit_dmg", 0.0)
    score += w_pen * eff.get("pen", 0.0)
    score += w_basic * eff.get("basic_dmg", 0.0)
    score += w_team * eff.get("team_dmg", 0.0)
    score += w_heal * eff.get("heal_eff", 0.0)
    score += w_shield * eff.get("shield_eff", 0.0)

    # Extra/DOT are only valuable if the kit actually uses them
    score += w_extra * extra_share * eff.get("extra_dmg", 0.0)
    score += w_dot * dot_share * eff.get("dot_dmg", 0.0)

    # energy: treat as fixed utility, scaled by pieces (4pc generally has stronger impact)
    score += w_energy * eff.get("energy", 0.0) * (1.4 if pieces == 4 else 1.0)

    return score * cond


def _best_set_combo(profile: dict, rune_db: list[dict], no_crit: bool) -> tuple[list[dict], list[list[dict]], list[str]]:
    """
    모든 룬에 대해 4pc, 2pc 후보를 스코어링하고,
    가장 높은 4+2 조합을 산출한다. (Guild raid only 패널티 없음)
    """
    if not rune_db:
        return (
            [{"set": "Alpha", "pieces": 4}, {"set": ("Epsilon" if no_crit else "Beth"), "pieces": 2}],
            [],
            ["runes.js 로딩 실패 → 기본 세트(Alpha + Beth/Epsilon) fallback 적용"],
        )

    # precompute
    scored4 = []
    scored2 = []

    for r in rune_db:
        nm = str(r.get("name") or "").strip()
        if not nm:
            continue

        eff2 = _set_effects(r, 2)
        eff4 = _set_effects(r, 4)

        s2 = _score_effect_vec(profile, eff2, 2, no_crit)
        s4 = _score_effect_vec(profile, eff4, 4, no_crit)

        scored2.append((s2, nm, eff2))
        scored4.append((s4, nm, eff4))

    scored2.sort(key=lambda x: x[0], reverse=True)
    scored4.sort(key=lambda x: x[0], reverse=True)

    # best 4+2 with different set names
    best = None
    for s4, n4, e4 in scored4[:30]:
        if s4 <= 0:
            continue
        for s2, n2, e2 in scored2[:30]:
            if n2 == n4:
                continue
            total = s4 + s2
            if best is None or total > best[0]:
                best = (total, n4, n2, s4, s2, e4, e2)

    if best is None:
        # all scores are 0 → revert to old heuristic
        return (
            [{"set": "Alpha", "pieces": 4}, {"set": ("Epsilon" if no_crit else "Beth"), "pieces": 2}],
            [],
            ["세트 효과 점수화가 불가(효과 파싱 0) → 기본 세트 fallback 적용"],
        )

    _, n4, n2, s4, s2, e4, e2 = best

    rationale = [
        f"세트 최적화(C): 4세트 '{n4}'(점수 {s4:.1f}) + 2세트 '{n2}'(점수 {s2:.1f}) 조합이 가장 높음.",
    ]

    # explain drivers (top 3 effect keys per set)
    def top_keys(eff: dict[str, float], limit=3):
        items = [(k, v) for k, v in eff.items() if v]
        items.sort(key=lambda x: abs(x[1]), reverse=True)
        return items[:limit]

    t4 = ", ".join([f"{k}:{v:g}" for k, v in top_keys(e4)])
    t2 = ", ".join([f"{k}:{v:g}" for k, v in top_keys(e2)])
    if t4:
        rationale.append(f"4세트 핵심효과 벡터: {t4}")
    if t2:
        rationale.append(f"2세트 핵심효과 벡터: {t2}")

    # alternates: top 2 alternative 4pc with same best 2pc
    alternates: list[list[dict]] = []
    for s4x, n4x, _ in scored4[1:8]:
        if n4x == n4:
            continue
        alternates.append([{"set": n4x, "pieces": 4}, {"set": n2, "pieces": 2}])
        if len(alternates) >= 3:
            break

    return (
        [{"set": n4, "pieces": 4}, {"set": n2, "pieces": 2}],
        alternates,
        rationale,
    )


def recommend_runes(cid: str, base: dict, detail: dict) -> dict:
    """
    반환 스키마(프론트 호환):
    {
      mode: 'override'|'auto'|'error',
      profile: {...},
      builds: [{
        title, setPlan:[{set,pieces,icon,twoPiece,fourPiece,note}], slots, substats, notes, rationale
      }]
    }
    """
    try:
        overrides = load_rune_overrides()
    except Exception:
        overrides = {}

    try:
        rune_db_list = load_runes_db()
    except Exception:
        rune_db_list = []

    rune_db_map = {str(r.get("name")): r for r in rune_db_list if isinstance(r, dict) and r.get("name")}

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
                r = rune_db_map.get(sname) or {}
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

    # auto (C)
    profile = _detect_profile(detail or {}, base or {})
    no_crit = detect_no_crit(detail or {})

    primary, alternates, rationale = _best_set_combo(profile, rune_db_list, no_crit)

    # add scaling evidence
    sample_text = profile.get("sample_text")
    if sample_text:
        rationale.append(f"스케일링 판정({profile.get('scaling')}): '{sample_text[:120]}'")
    if no_crit:
        rationale.append("치명타 불가/크리티컬 비활성 문구 감지 → 크리 관련 추천(치확/치피)을 제외.")

    def mk_build(title: str, setplan: list[dict]) -> dict:
        sp = []
        for x in setplan:
            sname = x["set"]
            r = rune_db_map.get(sname) or {}
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

    builds = [mk_build("추천(자동/C)", primary)]
    for idx, alt in enumerate(alternates[:3], start=1):
        builds.append(mk_build(f"대체안 {idx}", alt))

    # rune DB 기반 제약/노트 표기 (표시만, 페널티는 없음)
    notes = []
    for b in builds:
        for s in b["setPlan"]:
            nm = s["set"]
            r = rune_db_map.get(nm) or {}
            cr = r.get("classRestriction") or []
            if cr:
                notes.append(f"{nm} 4세트는 클래스 제한이 있습니다: {', '.join(map(str, cr))}")
            if r.get("note"):
                notes.append(f"{nm}: {r.get('note')}")
            if r.get("teamConflict"):
                notes.append(f"{nm}: 팀 세트 상충 주의 ({', '.join(map(str, r.get('teamConflict')))} )")

    uniq_notes = []
    seen = set()
    for n in notes:
        if n not in seen:
            seen.add(n)
            uniq_notes.append(n)
    if uniq_notes:
        for b in builds:
            b["notes"] = uniq_notes

    return {"mode": "auto", "profile": {**profile, "no_crit": no_crit}, "builds": builds}


def rune_summary_for_list(cid: str, base: dict, detail: dict) -> Optional[dict]:
    """
    리스트 카드용: 세트만 가볍게 노출
    - 에러가 나더라도 None 반환으로 UI 전체를 깨지 않게 처리
    """
    try:
        reco = recommend_runes(cid, base, detail)
        builds = reco.get("builds") or []
        if not builds:
            return None
        b0 = builds[0]
        return {
            "mode": reco.get("mode"),
            "sets": [{"set": s.get("set"), "pieces": s.get("pieces"), "icon": s.get("icon")} for s in (b0.get("setPlan") or [])],
        }
    except Exception:
        return None
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
