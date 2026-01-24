import os
import json
import re
import ast
from datetime import datetime, timezone
from typing import Optional, Any

from itertools import combinations
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
    # runes.js parsing diagnostics
    "runes_source": None,
    "runes_debug": None,
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

    if "after dealing continuous damage" in tl or "continuous damage" in tl or "dot" in tl or "지속 피해" in t or "지속피해" in t or "도트" in t or "중독" in t or "화상" in t or "출혈" in t or "감전" in t or "동상" in t or "부식" in t:
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
    if "continuous damage" in tl or "dot" in tl or "지속 피해" in t or "지속피해" in t or "도트" in t or "중독" in t or "화상" in t or "출혈" in t or "감전" in t or "동상" in t or "부식" in t:
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
    {"name": "Alpha", "twoPiece": "공격력 +8%", "fourPiece": "기본 공격 피해 +30%", "icon": None},
    {"name": "Poki", "twoPiece": "방어력 +12%", "fourPiece": "보호막 효과 +20%", "icon": None},
    {"name": "Beth", "twoPiece": "치명타 확률 +6%", "fourPiece": "HP가 80% 이상일 때 치명타 피해 +24%", "icon": None},
    {"name": "Zane", "twoPiece": "HP +8%", "fourPiece": "궁극기 사용 후 받는 피해 5% 감소 (10초)", "icon": None},
    {"name": "Daleth", "twoPiece": "회복 효과 +10%", "fourPiece": "전투 시작 시 즉시 에너지 1 획득", "icon": None},
    {"name": "Epsilon", "twoPiece": "추가 공격 피해 +20%", "fourPiece": "궁극기 사용 후 아군 전체의 피해가 10% 증가하며, 10초간 지속", "icon": None, "note": "동일한 패시브 효과는 중첩되지 않음"},
    {"name": "Het", "twoPiece": "추가 공격 피해 +20%", "fourPiece": "추가 공격 피해를 가한 후 치명타 확률 +15% (10초)", "icon": None, "note": "길드 레이드에서만 획득 가능"},
    {"name": "Gimel", "twoPiece": "지속 피해 +20%", "fourPiece": "지속 피해를 가한 후 자신의 공격력이 2% 증가하며 최대 10중첩, 5초간 지속", "icon": None, "note": "길드 레이드에서만 획득 가능"},
    {"name": "Iots", "twoPiece": "공격력 +8%", "fourPiece": "장착 캐릭터가 디버퍼 클래스일 경우, 궁극기 피해를 받은 대상이 5초간 받는 피해 10% 증가", "icon": None, "classRestriction": ["Debuffer"], "note": "동일 효과 중첩 불가. 길드 레이드에서만 획득가능"},
    {"name": "Kappa", "twoPiece": "방어력 +12%", "fourPiece": "전투 시작 후 10초 동안 아군 전체의 에너지 획득 효율 +30%", "icon": None, "note": "효과 중첩 불가. 파티 내 Daleth 4세트 효과는 비활성화됨. 길드 레이드 전용"},
]



def load_runes_db(force: bool = False) -> list[dict]:
    if CACHE["runes_db"] is not None and not force:
        return CACHE["runes_db"]

    # --- debug init (surfaced via /meta) ---
    CACHE["runes_source"] = None
    CACHE["runes_debug"] = {
        "runes_js_exists": os.path.isfile(RUNES_JS),
        "runes_js_size": os.path.getsize(RUNES_JS) if os.path.isfile(RUNES_JS) else 0,
        "extract_ok": False,
        "parse_ok": False,
        "unwrap_key": None,
        "fallback_reason": None,
    }

    raw = safe_read_text(RUNES_JS)
    runes_any: Any = None

    if raw:
        lit = _extract_js_literal(raw)
        if lit:
            CACHE["runes_debug"]["extract_ok"] = True
            # 1) pure json
            try:
                runes_any = json.loads(lit)
                CACHE["runes_debug"]["parse_ok"] = True
            except Exception:
                # 2) python literal eval (single quote / trailing comma robust)
                try:
                    runes_any = ast.literal_eval(_to_python_literal(lit))
                    CACHE["runes_debug"]["parse_ok"] = True
                except Exception:
                    # 3) json-friendly best-effort
                    try:
                        runes_any = json.loads(_json_friendly(lit))
                        CACHE["runes_debug"]["parse_ok"] = True
                    except Exception:
                        runes_any = None
                        CACHE["runes_debug"]["fallback_reason"] = "parse_failed"
        else:
            CACHE["runes_debug"]["fallback_reason"] = "extract_failed"
    else:
        CACHE["runes_debug"]["fallback_reason"] = "runes_js_missing_or_unreadable"

    # --- normalize to list[dict] ---
    runes_list: Optional[list] = None

    # A) direct list
    if isinstance(runes_any, list):
        runes_list = runes_any

    # B) wrapper object: {runes:[...]}, {data:[...]}, ...
    if runes_list is None and isinstance(runes_any, dict):
        for k in ("runes", "data", "items", "list", "sets"):
            v = runes_any.get(k)
            if isinstance(v, list):
                runes_list = v
                CACHE["runes_debug"]["unwrap_key"] = k
                break

    # C) Zone Nova runes.js pattern: export const RUNE_SETS = { Alpha:{...}, ... }
    #    -> dict mapping key -> rune dict
    if runes_list is None and isinstance(runes_any, dict):
        # Heuristic: many values look like rune entries
        vals = [v for v in runes_any.values() if isinstance(v, dict)]
        looks = 0
        for v in vals[:50]:
            if ("twoPiece" in v) or ("fourPiece" in v) or ("two_piece" in v) or ("four_piece" in v):
                looks += 1
        if vals and looks >= max(2, len(vals) // 4):
            runes_list = []
            for key, v in runes_any.items():
                if not isinstance(v, dict):
                    continue
                if not ("twoPiece" in v or "fourPiece" in v or "two_piece" in v or "four_piece" in v):
                    continue
                item = dict(v)
                # name이 없으면 key를 사용
                if not item.get("name"):
                    item["name"] = str(key)
                # 원본 키도 남겨두면 디버깅/매칭에 유용
                item.setdefault("key", str(key))
                runes_list.append(item)
            CACHE["runes_debug"]["unwrap_key"] = "map_values"

    if not isinstance(runes_list, list) or not runes_list:
        # parse는 성공했는데 리스트 형태로 정규화 실패
        if CACHE["runes_debug"]["fallback_reason"] is None:
            CACHE["runes_debug"]["fallback_reason"] = "not_a_list_after_parse"
        runes_list = FALLBACK_RUNES
        CACHE["runes_source"] = "fallback"
    else:
        CACHE["runes_source"] = "runes.js"

    rune_img_map = get_rune_img_map()

    def _as_list(x) -> list:
        if x is None:
            return []
        if isinstance(x, list):
            return x
        if isinstance(x, str) and x.strip():
            return [x.strip()]
        return []

    norm: list[dict] = []
    for r in runes_list:
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
            "classRestriction": _as_list(r.get("classRestriction") or r.get("class_restriction")),
            "teamConflict": _as_list(r.get("teamConflict") or r.get("team_conflict")),
            "icon": icon if isinstance(icon, str) and icon else None,
        })

    CACHE["runes_db"] = norm
    return norm


def rune_db_by_name() -> dict[str, dict]:
    db = load_runes_db()
    return {str(r.get("name")): r for r in db if isinstance(r, dict) and r.get("name")}



# -------------------------
# -------------------------
# Rune recommendation logic — role-based objective + exhaustive 4+2 search
# -------------------------

# Keyword dictionaries (EN + KO) used for both character profiling and rune-effect tagging.
_KW_HEAL = ["heal", "healing", "restore", "recovery", "회복", "치유", "힐"]
_KW_SHIELD = ["shield", "barrier", "보호막", "실드"]
_KW_DOT = ["continuous damage", "dot", "damage over time", "burn", "bleed", "poison", "지속 피해", "지속피해", "도트", "중독", "화상", "출혈", "감전", "동상", "부식"]
_KW_EXTRA = ["extra attack", "additional attack", "follow-up", "추가 공격", "추격", "추가타", "[extra attack]"]  # NOTE: '추가 피해'는 범용 추가데미지로 오탐이 많아 제외
_KW_TEAM = ["team", "all allies", "allied", "party", "아군", "팀", "전체", "전원"]
_KW_BUFF = ["increase", "increased", "buff", "up", "증가", "상승", "강화", "부여"]
_KW_DEBUFF = ["decrease", "reduced", "debuff", "down", "감소", "약화", "깎", "감쇠", "취약", "받는 피해"]
_KW_VULN = ["vulnerability", "take more damage", "damage taken", "받는 피해", "피해 증가", "취약"]
_KW_ENERGY = ["energy", "에너지", "gain", "regen", "회복", "획득", "충전"]
_KW_ULT = ["ultimate", "ult", "burst", "궁극기", "필살기", "궁"]
_KW_CRIT_DISABLE = ["cannot crit", "can't crit", "no crit", "crit disabled", "치명타 불가", "크리티컬 불가", "치명타가 발생하지"]

# Extra-attack strict matcher: 실제 '추가 공격(Extra attack)' 타입만 인정(범용 '추가 피해' 제외)
_RE_EXTRA_ATTACK_STRICT = re.compile(r'(\[\s*extra\s*attack\s*\]|extra\s*attack|additional\s*-?\s*attack|follow-?up|추가\s*공격|추격)', re.IGNORECASE)


# ---------- Character text extraction ----------

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
            for vv in v.values():
                walk(vv)
            return

    walk(x)
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _skill_texts(detail: dict) -> list[str]:
    if not isinstance(detail, dict):
        return []
    texts: list[str] = []

    for key in ["skills", "skill", "skillSet", "skill_set"]:
        if isinstance(detail.get(key), dict):
            texts += _collect_texts(detail.get(key))

    for key in ["normal", "basic", "basicAttack", "auto", "active", "ultimate", "burst", "passive", "passive1", "passive2", "skill1", "skill2", "skill3"]:
        if isinstance(detail.get(key), (dict, list, str)):
            texts += _collect_texts(detail.get(key))

    for key in ["teamSkill", "team_skill", "team", "synergy", "combo", "comboSkill"]:
        if isinstance(detail.get(key), (dict, list, str)):
            texts += _collect_texts(detail.get(key))

    if not texts:
        texts = _collect_texts(detail)

    return texts


# ---------- Scaling detection (ATK / HP / DEF) ----------

def _pct_hits(text: str, keys: list[str]) -> list[float]:
    # Extract percent scaling hits that indicate "X% of <stat>" in both EN and KO forms.
    hits: list[float] = []
    t = text.lower()

    # EN patterns
    for k in keys:
        k_low = k.lower()
        # "120% ... attack"
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*[^%\n]{0,28}\b" + re.escape(k_low) + r"\b", t):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass
        # "120% of attack" / "based on ATK"
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*(?:of|based\s+on|scales\s+with)\s*[^%\n]{0,16}\b" + re.escape(k_low) + r"\b", t):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    # KO patterns
    for k in keys:
        # "공격력의 120%"
        for m in re.finditer(re.escape(k) + r"\s*의\s*(\d+(?:\.\d+)?)\s*%", text):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass
        # loose fallback: "<stat> ... 120%"
        for m in re.finditer(re.escape(k) + r"[^\d%]{0,6}(\d+(?:\.\d+)?)\s*%", text):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    return hits


def _score_hits(hits: list[float]) -> float:
    if not hits:
        return 0.0
    return len(hits) * 10.0 + (sum(hits) / len(hits))


def detect_no_crit(detail: dict) -> bool:
    # Do NOT treat critRate==0 as 'cannot crit'. Only explicit flags/text.
    if not isinstance(detail, dict):
        return False

    for k in ["noCrit", "no_crit", "cannotCrit", "cannot_crit", "critDisabled", "crit_disabled"]:
        if detail.get(k) is True:
            return True

    for t in _skill_texts(detail):
        tl = t.lower()
        if any(k in tl for k in _KW_CRIT_DISABLE):
            return True

    return False




def detect_crit_rate_zero_or_missing(detail: dict, base: dict | None = None) -> bool:
    """Return True when character's crit rate is explicitly 0% or not present in stats.
    - This is different from `detect_no_crit()` which detects 'cannot crit' mechanics.
    - Used for hard bans such as 'Beth 추천 금지' when crit rate is 0 or missing.
    """
    def _to_float(x):
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            s = x.strip()
            if not s:
                return None
            # accept forms like "0", "0%", "0.0", "12.5%"
            m = re.search(r"-?\d+(?:\.\d+)?", s)
            if not m:
                return None
            try:
                return float(m.group(0))
            except Exception:
                return None
        return None

    stats = {}
    if isinstance(detail, dict) and isinstance(detail.get("stats"), dict):
        stats = detail.get("stats") or {}
    elif isinstance(base, dict) and isinstance(base.get("stats"), dict):
        stats = base.get("stats") or {}

    # 'critRate' (preferred), fallbacks
    cr = None
    for k in ("critRate", "crit_rate", "criticalRate", "critical_rate"):
        if k in stats:
            cr = stats.get(k)
            break

    val = _to_float(cr)
    if val is None:
        return True
    return abs(val) < 1e-9
def _role_from_base(base: dict) -> tuple[str, bool]:
    """Return (base_role, strict).
    - strict=True only when explicit 'role' field is present.
    - class is treated as a hint (strict=False), because some data uses 'class' for category/memory compatibility.
    """
    role_raw = str((base or {}).get("role") or "").strip().lower()
    cls_raw = str((base or {}).get("class") or "").strip().lower()

    if role_raw:
        if "tank" in role_raw:
            return "tank", True
        if "dps" in role_raw:
            return "dps", True
        if "healer" in role_raw:
            return "healer", True
        if "buffer" in role_raw:
            return "buffer", True
        if "debuffer" in role_raw:
            return "debuffer", True

    # class-only hint (not strict)
    if "guardian" in cls_raw:
        return "tank", False
    if "healer" in cls_raw:
        return "healer", False
    if "buffer" in cls_raw:
        return "buffer", False
    if "debuffer" in cls_raw:
        return "debuffer", False
    if cls_raw in ("warrior", "rogue", "mage"):
        return "dps", False

    return "dps", False


def _infer_role_from_texts(texts: list[str]) -> str:
    """Infer functional role from skill/memory texts.
    Notes:
      - Only classify as 'healer' when healing is clearly for allies/team, not just self-sustain.
      - 'buffer' requires team/allies + buff context.
      - 'debuffer' requires explicit debuff/vulnerability context.
    """
    team_buff = debuff = 0
    heal_team = heal_self = 0

    for t in texts:
        tl = (t or "").lower()

        has_team = any(k in tl for k in _KW_TEAM)
        has_heal = any(k in tl for k in _KW_HEAL)
        has_buff = any(k in tl for k in _KW_BUFF)
        has_debuff = any(k in tl for k in _KW_DEBUFF) or any(k in tl for k in _KW_VULN)

        # healing: distinguish ally/team healing vs self sustain
        if has_heal:
            if has_team or ("아군" in t) or ("팀" in t) or ("파티" in t) or ("전체" in t) or ("전원" in t):
                heal_team += 2
            else:
                heal_self += 1

        # team buff
        if has_team and has_buff:
            team_buff += 2

        # debuff
        if has_debuff:
            debuff += 1

    # prioritize true healers (ally/team healing)
    if heal_team >= max(team_buff, debuff) and heal_team >= 3:
        return "healer"
    if team_buff >= max(heal_team, debuff) and team_buff >= 3:
        return "buffer"
    if debuff >= max(heal_team, team_buff) and debuff >= 3:
        return "debuffer"
    return "dps"



def _detect_profile(detail: dict, base: dict) -> dict:
    texts = _skill_texts(detail or {})

    atk_hits, hp_hits, def_hits = [], [], []
    for t in texts:
        atk_hits += _pct_hits(t, ["attack power", "atk", "attack", "공격력"])
        hp_hits += _pct_hits(t, ["max hp", "hp", "health", "체력", "생명"])
        def_hits += _pct_hits(t, ["defense", "def", "방어력"])

    atk_s = _score_hits(atk_hits)
    hp_s = _score_hits(hp_hits)
    def_s = _score_hits(def_hits)

    best = max(atk_s, hp_s, def_s)
    scaling = "MIX"
    if best > 0:
        scaling = "ATK" if best == atk_s else ("HP" if best == hp_s else "DEF")

    dot_cnt = extra_cnt = ult_cnt = 0
    normal_cnt = 0
    team_buff_cnt = debuff_cnt = heal_cnt = shield_cnt = 0

    for t in texts:
        tl = t.lower()
        if any(k in tl for k in _KW_DOT):
            dot_cnt += 1
        # strict extra attack detection (avoid false positives like "추가 피해")
        if _RE_EXTRA_ATTACK_STRICT.search(t):
            extra_cnt += 1
        if any(k in tl for k in _KW_ULT):
            ult_cnt += 1
        if ("basic attack" in tl) or ("normal attack" in tl) or ("기본 공격" in t) or ("기본공격" in t) or ("평타" in t):
            normal_cnt += 1
        if any(k in tl for k in _KW_HEAL):
            heal_cnt += 1
        if any(k in tl for k in _KW_SHIELD):
            shield_cnt += 1
        if any(k in tl for k in _KW_TEAM) and any(k in tl for k in _KW_BUFF):
            team_buff_cnt += 1
        if any(k in tl for k in _KW_DEBUFF) or any(k in tl for k in _KW_VULN):
            debuff_cnt += 1

    total = max(1, len(texts))
    dot_share = dot_cnt / total
    normal_share = normal_cnt / total
    extra_share = extra_cnt / total
    ult_importance = min(1.0, ult_cnt / total * 2.0)
    team_buff_strength = min(1.0, team_buff_cnt / total * 2.0)
    debuff_strength = min(1.0, debuff_cnt / total * 2.0)
    heal_strength = min(1.0, heal_cnt / total * 2.0)
    shield_strength = min(1.0, shield_cnt / total * 2.0)

    # Het 4세트 허용 조건: 명시 키워드(추가공격/extra attack/follow-up/additional attack) 존재 여부
    has_explicit_extra_attack = any(_RE_EXTRA_ATTACK_STRICT.search(t or "") for t in texts)


    base_role, base_strict = _role_from_base(base or {})
    text_role = _infer_role_from_texts(texts)

    # tank heuristic: strong HP/DEF scaling indications without clear support role
    tankish = (hp_s + def_s) >= (atk_s * 1.2) and (hp_s >= 12.0 or def_s >= 12.0)

    # Role resolution policy:
    #  - If explicit role is present (strict), trust it.
    #  - Otherwise, start from class-hint role, and only promote DPS -> support when evidence is strong.
    role = base_role if base_strict else base_role

    if not base_strict:
        # If class hinted 'buffer/debuffer/healer' but evidence is weak, demote to DPS.
        if base_role in ("buffer", "debuffer") and (team_buff_strength < 0.35 and debuff_strength < 0.35):
            role = "dps"
        if base_role == "healer" and heal_strength < 0.45:
            role = "dps"

        # If class hinted DPS (Warrior/Rogue/Mage) but evidence strongly supports support role, promote.
        # This prevents misclassifying DPS as support due to a single "buff" keyword.
        if base_role == "dps":
            if text_role == "healer" and heal_strength >= 0.55:
                role = "healer"
            elif text_role == "buffer" and team_buff_strength >= 0.60:
                role = "buffer"
            elif text_role == "debuffer" and debuff_strength >= 0.60:
                role = "debuffer"
            else:
                role = "dps"

        # Always keep Guardian-class tanks as tank via base_role mapping; apply tank heuristic as backup.
        if tankish and role == "dps" and (team_buff_strength + debuff_strength + heal_strength) < 0.35:
            role = "tank"

    no_crit = detect_no_crit(detail or {})
    healer_hybrid = bool(role == "healer" and atk_s >= 15.0 and heal_strength < 0.45)

    crit_rate_zero_or_missing = detect_crit_rate_zero_or_missing(detail or {}, base or {})
    basic_attack_based = bool(role == "dps" and scaling == "ATK" and (normal_share >= 0.20 or normal_cnt >= 2))

    sample_text = None
    if scaling == "ATK":
        sample_text = next((t for t in texts if _pct_hits(t, ["attack power", "atk", "attack", "공격력"])), None)
    elif scaling == "HP":
        sample_text = next((t for t in texts if _pct_hits(t, ["max hp", "hp", "health", "체력", "생명"])), None)
    elif scaling == "DEF":
        sample_text = next((t for t in texts if _pct_hits(t, ["defense", "def", "방어력"])), None)

    return {
        "role": role,
        "scaling": scaling,
        "atk_score": atk_s,
        "hp_score": hp_s,
        "def_score": def_s,
        "dot_share": dot_share,
        "normal_share": normal_share,
        "extra_share": extra_share,
        "has_explicit_extra_attack": has_explicit_extra_attack,
        "ult_importance": ult_importance,
        "team_buff_strength": team_buff_strength,
        "debuff_strength": debuff_strength,
        "heal_strength": heal_strength,
        "shield_strength": shield_strength,
        "healer_hybrid": healer_hybrid,
        "no_crit": no_crit,
        "crit_rate_zero_or_missing": crit_rate_zero_or_missing,
        "basic_attack_based": basic_attack_based,
        "sample_text": sample_text,
    }


# ---------- Rune tagging ----------

def _has_any(text: str, keys: list[str]) -> bool:
    tl = (text or "").lower()
    return any(k in tl for k in keys)


def _rune_tags_from_effect(effect_text: str) -> set[str]:
    t = (effect_text or "").strip()
    tl = t.lower()
    tags: set[str] = set()

    # base stats
    if "attack power" in tl or "atk" in tl or "공격력" in t:
        tags.add("ATK")
    if "defense" in tl or "def" in tl or "방어력" in t:
        tags.add("DEF")
    if "hp" in tl or "health" in tl or "체력" in t or "생명" in t:
        tags.add("HP")

    # crit
    if "critical hit rate" in tl or "crit rate" in tl or "치명타 확률" in t or "치확" in t:
        tags.add("CRIT_RATE")
    if "critical hit damage" in tl or "crit damage" in tl or "치명타 피해" in t or "치피" in t:
        tags.add("CRIT_DMG")

    # damage type synergies
    if "basic attack damage" in tl or "기본 공격 피해" in t:
        tags.add("BASIC_DMG")
    if "extra attack" in tl or "추가 공격" in t:
        tags.add("EXTRA_DMG")
    if ("continuous damage" in tl or "damage over time" in tl or "dot" in tl
            or "지속 피해" in t or "지속피해" in t or "도트" in t
            or "중독" in t or "화상" in t or "출혈" in t or "감전" in t
            or "동상" in t or "부식" in t):
        tags.add("DOT_DMG")

    # heal/shield
    if "healing effectiveness" in tl or "치유" in t or "회복" in t:
        tags.add("HEAL")
    if "shield effectiveness" in tl or "보호막" in t or "실드" in t:
        tags.add("SHIELD")

    # team damage / vulnerability
    if ("team" in tl or "all allies" in tl or "아군" in t or "팀" in t) and ("damage" in tl or "피해" in t):
        tags.add("TEAM_DMG")
    if _has_any(t, _KW_VULN) or ("받는 피해" in t):
        tags.add("VULN")

    # energy economy
    if ("gain 1 energy" in tl) or ("gain 1 energy immediately" in tl) or ("전투 시작" in t and "에너지" in t):
        tags.add("START_ENERGY")
    if ("energy gain efficiency" in tl) or ("에너지 획득 효율" in t) or ("에너지 획득효율" in t):
        tags.add("ENERGY_EFF")

    # ultimate trigger
    if ("after ultimate" in tl) or ("after activating ultimate" in tl) or ("궁극기" in t and ("후" in t or "사용" in t or "발동" in t)):
        tags.add("ULT_TRIGGER")

    return tags



# ---------- Exact-match guards (regex) ----------

# Opener-limited energy efficiency 4-piece (Kappa; previously Tide):
# - "전투 시작 후 10초 ... 에너지 획득 효율 +30%" (or EN equivalents)
# This is *explicitly excluded in PVE* per project requirements.
_RE_OPENER_10S = re.compile(
    r"(?:\bfirst\s*10\s*s\b|\binitial\s*10\s*s\b|\bwithin\s*the\s*first\s*10\s*s\b|첫\s*10\s*초|처음\s*10\s*초|전투\s*시작\s*(?:후)?\s*10\s*초)",
    re.IGNORECASE,
)
_RE_BATTLE_START = re.compile(
    r"(?:\bat\s*the\s*start\s*(?:of\s*(?:battle|combat))\b|\bwhen\s*(?:battle|combat)\s*starts\b|전투\s*시작(?:\s*시)?|전투\s*개시)",
    re.IGNORECASE,
)
_RE_ENERGY_EFF = re.compile(
    r"(?:energy\s*(?:gain|recovery)\s*eff(?:iciency)?|energy\s*eff(?:iciency)?|에너지\s*획득\s*효율|에너지\s*회복\s*효율)",
    re.IGNORECASE,
)

def _is_opener_energy_4p(set_name: str, rune_db: dict[str, dict]) -> bool:
    """Identify opener-limited energy efficiency 4p (Kappa/Tide-type) by name OR by 4p effect text."""
    if not set_name:
        return False
    nm = str(set_name).strip().lower()
    r = rune_db.get(set_name) or {}
    four = str(r.get("fourPiece") or "")
    # Name-based fast path
    if nm in ("kappa", "tide"):
        # If 4p text exists, verify it matches opener-energy pattern to avoid mis-tagging.
        if four:
            return bool(_RE_BATTLE_START.search(four) and _RE_OPENER_10S.search(four) and _RE_ENERGY_EFF.search(four))
        return True
    # Text-based path (future-proof against renames)
    if not four:
        return False
    return bool(_RE_BATTLE_START.search(four) and _RE_OPENER_10S.search(four) and _RE_ENERGY_EFF.search(four))


# Backward-compat: keep Tide-only helper (used by older log strings), now delegating.
def _is_tide_4p_exact(set_name: str, rune_db: dict[str, dict]) -> bool:
    return _is_opener_energy_4p(set_name, rune_db)

def _rune_tag_index(rune_db: dict[str, dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for name, r in rune_db.items():
        two = str(r.get("twoPiece") or "")
        four = str(r.get("fourPiece") or "")
        idx[name] = {"tags2": _rune_tags_from_effect(two), "tags4": _rune_tags_from_effect(four)}
    return idx


# ---------- Scoring: objective by role ----------

def _score_set(profile: dict, set_name: str, pieces: int, rune_db: dict[str, dict], tag_idx: dict[str, dict], mode: str = "pve") -> float:
    tags = (tag_idx.get(set_name) or {}).get("tags4" if pieces == 4 else "tags2", set())

    role = profile["role"]
    scaling = profile["scaling"]
    no_crit = profile["no_crit"]

    dot = profile["dot_share"]
    extra = profile["extra_share"]
    ult = profile["ult_importance"]
    debuff = profile["debuff_strength"]
    heal = profile["heal_strength"]
    shield = profile["shield_strength"]

    score = 0.0

    # PVE hard rule: opener-limited energy efficiency 4p (Kappa/Tide-type) is excluded.
    md = (str(mode or "pve").strip().lower() or "pve")
    if md == "pve" and pieces == 4 and _is_opener_energy_4p(set_name, rune_db):
        # Returning a very low score keeps compatibility with downstream sorting, while effectively excluding.
        return -1e9
    
    # Hard gate: if crit rate is 0% or missing, Beth is forbidden (2p/4p).
    if (set_name or "").strip().lower() == "beth" and profile.get("crit_rate_zero_or_missing"):
        return -1e9

    # Hard gate: Beth 4p (HP>=80% crit dmg) is a DPS-only set. Tanks/supports should not be recommended.
    if pieces == 4 and (set_name or "").strip().lower() == "beth":
        if role in ("tank", "guardian", "healer", "buffer", "debuffer"):
            return -1e9
        if no_crit:
            return -1e9
# scaling match (mostly for 2pc)
    if pieces == 2:
        if "ATK" in tags and scaling == "ATK":
            score += 6.0
        elif "ATK" in tags:
            score += 2.0

        if "DEF" in tags and scaling == "DEF":
            score += 6.0
        elif "DEF" in tags:
            score += 2.0

        if "HP" in tags and scaling == "HP":
            score += 6.0
        elif "HP" in tags:
            score += 2.0

        if "CRIT_RATE" in tags and not no_crit:
            if role == "dps":
                score += 6.0
            elif role == "debuffer":
                score += 2.0
            elif profile.get("healer_hybrid"):
                score += 2.0
            else:
                # 힐러/탱커/버퍼는 기본적으로 치확 2세트 효율이 낮음(하이브리드 예외)
                score += 0.0

    # role-specific (4pc dominates)
    if role == "buffer":
        if "TEAM_DMG" in tags:
            score += 18.0 * (0.6 + 0.4 * ult)
        if "ENERGY_EFF" in tags:
            score += 20.0 * (0.6 + 0.4 * ult)
        if "START_ENERGY" in tags:
            score += 16.0 * (0.6 + 0.4 * ult)
        if "ULT_TRIGGER" in tags:
            score += 6.0 * ult
        if "HP" in tags or "DEF" in tags:
            score += 2.0
        if "CRIT_DMG" in tags or "CRIT_RATE" in tags or "BASIC_DMG" in tags:
            score += 0.5

    elif role == "debuffer":
        if "VULN" in tags:
            score += 22.0 * (0.6 + 0.4 * ult) * (0.6 + 0.4 * max(debuff, 0.2))
        if "TEAM_DMG" in tags:
            score += 8.0 * (0.6 + 0.4 * ult)
        if "ENERGY_EFF" in tags:
            score += 10.0 * (0.6 + 0.4 * ult)
        if "START_ENERGY" in tags:
            score += 8.0 * (0.6 + 0.4 * ult)
        if "ULT_TRIGGER" in tags:
            score += 4.0 * ult
        if "HP" in tags or "DEF" in tags:
            score += 2.5

    elif role == "healer":
        if "HEAL" in tags:
            score += 22.0 * (0.7 + 0.3 * max(heal, 0.2))
        if "ENERGY_EFF" in tags:
            score += 10.0 * (0.6 + 0.4 * ult)
        if "START_ENERGY" in tags:
            score += 10.0 * (0.6 + 0.4 * ult)
        if "HP" in tags or "DEF" in tags:
            score += 6.0
        if "SHIELD" in tags:
            # 보호막 세트는 "보호막/실드" 기믹이 실제로 존재할 때만 유효
            if shield <= 0.05:
                # 보호막 스킬이 사실상 없으면 4세트 채용을 억제
                if pieces == 4:
                    score -= 6.0
            else:
                score += 10.0 * min(1.0, shield)
        if profile.get("healer_hybrid") and not no_crit:
            if "CRIT_RATE" in tags or "CRIT_DMG" in tags:
                score += 3.0
            if "ATK" in tags and scaling == "ATK":
                score += 4.0

    elif role == "tank":
        if "HP" in tags:
            score += 16.0
        if "DEF" in tags:
            score += 16.0
        if "SHIELD" in tags:
            # 보호막 세트는 보호막 기믹이 있을 때만 가치가 큼
            if shield <= 0.05:
                if pieces == 4:
                    score -= 6.0
            else:
                score += 14.0 * min(1.0, shield)
        if "START_ENERGY" in tags:
            score += 3.0
        if "ENERGY_EFF" in tags:
            score += 3.0

    else:  # DPS
        # NOTE: DPS는 "역할"보다 "스케일(ATK/DEF/HP)"을 우선 반영해야 함.
        # 예: DEF 스케일 DPS(Apep 등)에게 ATK/기본공격 피해 중심 세트가 상위로 뜨는 회귀를 방지.

        # 치명 세트: ATK 스케일 DPS는 높은 가중치, DEF/HP 스케일 DPS는 보조(가중치 하향)
        if ("CRIT_RATE" in tags or "CRIT_DMG" in tags) and not no_crit:
            if scaling == "ATK":
                score += 16.0
            else:
                score += 8.0

        # 기본공격 피해: 대부분 ATK 기반(평타 비중)에서만 의미가 큼.
        # DEF/HP 스케일 DPS에는 오추천을 유발하므로 거의 가치를 주지 않는다.
        if "BASIC_DMG" in tags:
            ns = float(profile.get("normal_share") or 0.0)
            if scaling != "ATK":
                score += 0.0
            else:
                if ns < 0.10:
                    score -= 6.0  # avoid Alpha 4p when basic-attack share is low
                else:
                    score += 10.0 * min(1.0, ns * 3.0)
        if "EXTRA_DMG" in tags:
            # Extra-attack 세트는 실제 'Extra attack/추가 공격' 기믹이 있을 때만 고가치
            if extra < 0.12:
                score -= 8.0  # 오탐 억제
            else:
                score += 18.0 * (0.3 + 0.7 * min(1.0, extra * 3.0))
        if "DOT_DMG" in tags:
            # DoT 세트는 실제 DoT 기믹이 있을 때만 고가치 ('지속' 오탐 방지)
            if dot < 0.12:
                score -= 8.0
            else:
                score += 18.0 * (0.3 + 0.7 * min(1.0, dot * 3.0))

        # 스케일 매칭 보상: DEF/HP 스케일은 스탯 자체 기여도가 크므로 보상을 조금 더 줌
        if "ATK" in tags:
            if scaling == "ATK":
                score += 8.0
            else:
                # DEF/HP 스케일 DPS에게 ATK 세트가 끼어드는 것을 강하게 억제
                # (특히 4세트 ATK/평타 세트가 1순위로 뜨는 회귀 방지)
                if role == "dps":
                    score -= 12.0 if pieces == 4 else 8.0
        if "DEF" in tags and scaling == "DEF":
            score += 14.0
        if "HP" in tags and scaling == "HP":
            score += 14.0

        if "ENERGY_EFF" in tags:
            score += 4.0 * ult
        if "START_ENERGY" in tags:
            score += 3.0 * ult

        if "TEAM_DMG" in tags:
            score += 2.0

        if "HP" in tags or "DEF" in tags:
            score += 1.0

    if no_crit and ("CRIT_RATE" in tags or "CRIT_DMG" in tags):
        score -= 8.0

    return score


def _best_rune_builds(profile: dict, rune_db: dict[str, dict], mode: str = "pve") -> tuple[list[dict], list[str]]:
    tag_idx = _rune_tag_index(rune_db)
    sets = list(rune_db.keys())
    md = (str(mode or "pve").strip().lower() or "pve")

    role = profile.get("role") or "dps"
    extra_attack_4_allowed = bool(profile.get("has_explicit_extra_attack"))

    sets_all = list(sets)
    allowed4 = set(sets_all)
    allowed2 = set(sets_all)
    forced4: str | None = None
    forced2: str | None = None

    # ---- Role-based rune pools (hard constraints) ----
    if role == "tank":
        allowed4 = {"Poki", "Zane"}
        allowed2 = {"Poki", "Zane", "Kappa"}  # Kappa는 2세트만
    elif role == "dps":
        allowed4 = {"Alpha", "Beth", "Epsilon", "Het", "Gimel", "Iots"}
        allowed2 = set(allowed4)
    elif role == "buffer":
        allowed4 = set(sets_all) - {"Daleth"}
        allowed2 = set(sets_all) - {"Daleth"}
    elif role == "debuffer":
        # (요청사항) Debuffer는 Iots 4세트 고정 + 2세트는 추천으로 구성
        forced4 = "Iots"
        allowed4 = {"Iots"}
        allowed2 = set(sets_all) - {"Poki", "Zane", "Daleth", "Kappa", "Iots"}
    elif role == "healer":
        # (요청사항) Healer는 Daleth 2세트 필수
        forced2 = "Daleth"
        allowed4 = set(sets_all)
        allowed2 = set(sets_all)

    # ---- Basic attack 기반 DPS(예: Freya): Alpha 4세트 고정 ----
    if role != "debuffer" and profile.get("basic_attack_based"):
        forced4 = "Alpha"
        allowed4.add("Alpha")

    # ---- Apep 예외: DEF 스케일링(요청사항) → Poki 필수 (2세트 우선) ----
    name_l = (str(profile.get("_name") or "") + " " + str(profile.get("_cid") or "")).lower()
    if "apep" in name_l:
        forced2 = "Poki"
        allowed2.add("Poki")

    # ---- Beth 추천 금지: 치확이 0%이거나 미존재 ----
    if profile.get("crit_rate_zero_or_missing"):
        allowed4.discard("Beth")
        allowed2.discard("Beth")
        if forced4 == "Beth":
            forced4 = None
        if forced2 == "Beth":
            forced2 = None


    # ---- Het/Epsilon 4세트 허용 조건: 명시 키워드(추가공격/extra attack/follow-up/additional attack) 있을 때만 ----
    if not extra_attack_4_allowed:
        # 4세트만 금지 (2세트는 허용)
        allowed4.discard("Het")
        allowed4.discard("Epsilon")
        if forced4 in {"Het", "Epsilon"}:
            forced4 = None
    # Final iteration sets
    sets4 = [forced4] if forced4 else sorted(list(allowed4))
    sets2 = [forced2] if forced2 else sorted(list(allowed2))

    # Prevent 4p/2p duplication when a forced set exists
    if forced2:
        sets4 = [s for s in sets4 if s != forced2]
    if forced4:
        sets2 = [s for s in sets2 if s != forced4]

    # Safety: if constraints made candidates empty, fall back to full set list
    if not sets4:
        sets4 = list(sets_all)
    if not sets2:
        sets2 = list(sets_all)


    # Re-apply Het(4p) restriction even after safety fallbacks
    if not extra_attack_4_allowed:
        sets4 = [s for s in sets4 if s not in {"Het", "Epsilon"}]

    best: list[tuple[float, str, str]] = []
    for s4 in sets4:
        sc4 = _score_set(profile, s4, 4, rune_db, tag_idx, mode=mode)
        if sc4 < -5:
            continue
        for s2 in sets2:
            # 룬 세트는 중복 장착 불가: 4세트와 2세트가 같은 세트면 제외
            if s2 == s4:
                continue
            sc2 = _score_set(profile, s2, 2, rune_db, tag_idx, mode=mode)
            total = sc4 + sc2
            best.append((total, s4, s2))

    best.sort(key=lambda x: x[0], reverse=True)

    # ✅ Safety: if everything was filtered out, relax the per-set threshold and still pick the best pair.
    # (We keep the PVE hard exclusion for opener-energy 4p via _score_set().)
    if not best and len(sets) >= 2:
        for s4 in sets4:
            sc4 = _score_set(profile, s4, 4, rune_db, tag_idx, mode=mode)
            for s2 in sets2:
                if s2 == s4:
                    continue
                sc2 = _score_set(profile, s2, 2, rune_db, tag_idx, mode=mode)
                best.append((sc4 + sc2, s4, s2))
        best.sort(key=lambda x: x[0], reverse=True)

    # UI/요청사항: "대체안" 노출은 혼선을 유발하므로 1개만 반환
    top = best[:1]

    rationale: list[str] = []
    rationale.append(f"역할 판정: {profile['role']} / 스케일링 판정: {profile['scaling']}")
    if profile.get("sample_text"):
        rationale.append(f"스케일링 근거 예시: '{str(profile['sample_text'])[:140]}'")
    if profile.get("no_crit"):
        rationale.append("치명타 불가/비활성 문구 감지 → 치명타(치확/치피) 중심 세트는 감점 처리.")
    if profile["role"] in ("buffer", "debuffer"):
        rationale.append("서포트 역할은 팀 기여/궁극기 가동률(에너지) 비중을 높게 두고 최적화합니다.")
    elif profile["role"] == "dps":
        if profile.get("scaling") in ("DEF", "HP"):
            rationale.append("DEF/HP 스케일 DPS는 공격력/기본공격 피해 중심 세트 효율이 낮아 감점 처리하고, 스케일 스탯(DEF/HP) 중심으로 최적화합니다.")
        else:
            rationale.append("딜러 역할은 본인 기대 피해(치명/특수 피해 타입) 비중을 높게 두고 최적화합니다.")

    builds: list[dict] = []
    for (score, s4, s2) in top:
        builds.append({
            "title": "추천(자동)",
            "_score": round(score, 2),
            "setPlan": [{"set": s4, "pieces": 4}, {"set": s2, "pieces": 2}],
        })
    if md == "pve":
        rationale.append("PVE: Kappa(오프너 10초 에너지) 4세트(전투 시작/첫 10초 오프너 한정)는 장기전 평균 효율이 낮아 강한 패널티를 적용했습니다.")
    return builds, rationale


# ---------- Slot plan (main stats) ----------

def _element_damage_label(element: str) -> str:
    e = normalize_element(element or "-")
    if e in ("Storm", "Blaze", "Frost", "Holy", "Chaos"):
        return f"{e} Attribute Damage (%)"
    return "Element Attribute Damage (%)"


def _slot_plan_for(profile: dict, element: str) -> dict:
    role = profile["role"]
    scaling = profile["scaling"]
    no_crit = profile["no_crit"]

    plan = {
        "1": ["HP (Flat Value)"],
        "2": ["Attack (Flat Value)"],
        "3": ["Defense (Flat Value)"],
        "4": [],
        "5": [],
        "6": [],
    }

    scaling_pct = "Attack (%)" if scaling == "ATK" else ("HP (%)" if scaling == "HP" else ("Defense (%)" if scaling == "DEF" else "Attack (%)"))

    if role == "healer":
        plan["4"] = ["Healing Effectiveness (%)", "HP (%)", "Defense (%)"]
        plan["5"] = ["HP (%)", "Defense (%)"]
        plan["6"] = ["HP (%)", "Defense (%)"]
        return plan

    if role == "tank":
        plan["4"] = ["Defense (%)", "HP (%)"]
        plan["5"] = ["Defense (%)", "HP (%)"]
        plan["6"] = ["Defense (%)", "HP (%)"]
        return plan

    if role in ("buffer", "debuffer"):
        plan["4"] = ["Energy-related (if exists)", "HP (%)", "Defense (%)", scaling_pct]
        plan["5"] = ["HP (%)", "Defense (%)", _element_damage_label(element)]
        plan["6"] = ["HP (%)", "Defense (%)", scaling_pct]
        if not no_crit:
            plan["4"].append("Critical Rate (%) (부옵/대체)")
        return plan

    # DPS
    # DEF/HP 스케일 DPS는 공격력/치명 템플릿을 그대로 쓰면 오추천이 발생하므로 우선순위를 재정렬한다.
    if no_crit:
        plan["4"] = ["Attack Penetration (%)", scaling_pct, "Attack (%)", "HP (%) (생존)"]
        plan["5"] = [_element_damage_label(element), scaling_pct, "Attack (%)", "HP (%) (생존)"]
        plan["6"] = [scaling_pct, "Attack (%)", "HP (%) (생존)", "Defense (%) (생존)"]
    else:
        if scaling == "DEF":
            plan["4"] = ["Defense (%)", "Critical Rate (%)", "Critical Damage (%)", "Attack Penetration (%)"]
            plan["5"] = ["Defense (%)", _element_damage_label(element), "Attack Penetration (%)", "HP (%) (생존)"]
            plan["6"] = ["Defense (%)", "Critical Rate (%) (대체)", "HP (%) (생존)"]
        elif scaling == "HP":
            plan["4"] = ["HP (%)", "Critical Rate (%)", "Critical Damage (%)", "Attack Penetration (%)"]
            plan["5"] = ["HP (%)", _element_damage_label(element), "Attack Penetration (%)", "Defense (%) (생존)"]
            plan["6"] = ["HP (%)", "Critical Rate (%) (대체)", "Defense (%) (생존)"]
        else:
            plan["4"] = ["Critical Rate (%)", "Attack Penetration (%)", "Critical Damage (%)", scaling_pct]
            plan["5"] = [_element_damage_label(element), scaling_pct, "Attack (%)", "HP (%)", "Defense (%)"]
            plan["6"] = [scaling_pct, "Attack (%)", "HP (%)", "Defense (%)"]

    return plan


def _substats_for(profile: dict) -> list[str]:
    role = profile["role"]
    scaling = profile["scaling"]
    no_crit = profile["no_crit"]

    scaling_pct = "Attack (%)" if scaling == "ATK" else ("HP (%)" if scaling == "HP" else ("Defense (%)" if scaling == "DEF" else "Attack (%)"))

    if role == "healer":
        out = ["Healing Effectiveness (%)", "HP (%)", "Defense (%)", "Flat HP / Flat DEF"]
        if profile.get("healer_hybrid") and not no_crit:
            out += ["Critical Rate (%)", "Critical Damage (%)", "Attack (%)"]
        return out

    if role == "tank":
        return ["HP (%)", "Defense (%)", "Flat HP / Flat DEF", "Damage Reduction / RES (존재 시)"]

    if role in ("buffer", "debuffer"):
        out = ["Energy Recovery / Energy Gain (존재 시)", "HP (%)", "Defense (%)", scaling_pct]
        if not no_crit:
            out += ["Critical Rate (%) (부옵/대체)"]
        return out

    if no_crit:
        # no-crit DPS도 DEF/HP 스케일이면 스케일 스탯을 최우선으로
        return [scaling_pct, "Attack Penetration (%)", "Element Attribute Damage (%)", "HP (%) / Defense (%) (생존)", "Attack (%) (대체)", "Flat Attack (대체)"]

    # DPS substat priority
    if scaling == "DEF":
        return ["Defense (%)", "Critical Rate (%)", "Critical Damage (%)", "Attack Penetration (%)", "HP (%) (생존)", "Flat DEF (대체)"]
    if scaling == "HP":
        return ["HP (%)", "Critical Rate (%)", "Critical Damage (%)", "Attack Penetration (%)", "Defense (%) (생존)", "Flat HP (대체)"]

    return ["Critical Rate (%)", "Critical Damage (%)", scaling_pct, "Attack Penetration (%)", "Flat Attack", "HP (%) / Defense (%) (생존)"]


def recommend_runes(cid: str, base: dict, detail: dict, mode: str = "pve") -> dict:
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
    # For rule-based exceptions (e.g., Apep/Poki), keep identifiers in profile.
    profile["_cid"] = str(cid or "")
    profile["_name"] = str((base or {}).get("name") or (detail or {}).get("name") or "")
    core_builds, rationale = _best_rune_builds(profile, rune_db, mode=mode)

    # ✅ Hard guarantee: never return an empty builds list when rune_db is available.
    if (not core_builds) and len(rune_db.keys()) >= 2:
        tag_idx = _rune_tag_index(rune_db)
        sets = list(rune_db.keys())
        best = None
        for s4 in sets:
            sc4 = _score_set(profile, s4, 4, rune_db, tag_idx, mode=mode)
            for s2 in sets:
                if s2 == s4:
                    continue
                sc2 = _score_set(profile, s2, 2, rune_db, tag_idx, mode=mode)
                tot = sc4 + sc2
                if best is None or tot > best[0]:
                    best = (tot, s4, s2)
        if best:
            core_builds = [{
                "title": "추천(자동)",
                "_score": round(float(best[0]), 2),
                "setPlan": [{"set": best[1], "pieces": 4}, {"set": best[2], "pieces": 2}],
            }]
            rationale = (rationale or []) + ["(안전장치) 후보 필터링으로 결과가 비었으나, 최상위 조합을 강제로 1개 산출했습니다."]

    builds = []
    for b in core_builds:
        setplan = []
        for s in b.get("setPlan") or []:
            nm = s.get("set")
            r = rune_db.get(nm) or {}
            setplan.append({
                "set": nm,
                "pieces": s.get("pieces"),
                "icon": r.get("icon"),
                "twoPiece": r.get("twoPiece", ""),
                "fourPiece": r.get("fourPiece", ""),
                "note": r.get("note", ""),
            })

        builds.append({
            "title": b.get("title") or "추천(자동)",
            "setPlan": setplan,
            "slots": _slot_plan_for(profile, base.get("element")),
            "substats": _substats_for(profile),
            "notes": [],
            "rationale": rationale + [f"조합 점수(상대 비교용): {b.get('_score')}"],
        })

    # constraint notes
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

    uniq_notes = []
    seen = set()
    for n in notes:
        if n not in seen:
            seen.add(n)
            uniq_notes.append(n)

    for b in builds:
        b["notes"] = uniq_notes

    return {"mode": "auto", "profile": profile, "builds": builds}



def recommend_runes_both(cid: str, base: dict, detail: dict) -> dict:
    """Return both PVE and PVP rune recommendations.

    Backward-compatible: also includes `builds` as PVE builds.
    """
    pve = recommend_runes(cid, base, detail, mode="pve")
    pvp = recommend_runes(cid, base, detail, mode="pvp")
    return {
        "mode": "both",
        "pve": pve,
        "pvp": pvp,
        "profile": (pve.get("profile") or pvp.get("profile") or {}),
        "builds": pve.get("builds") or [],
    }


def rune_summary_for_list(cid: str, base: dict, detail: dict) -> Optional[dict]:
    reco = recommend_runes(cid, base, detail)
    builds = reco.get("builds") or []
    if not builds:
        return None
    b0 = builds[0]
    return {"mode": reco.get("mode"), "sets": [{"set": s.get("set"), "pieces": s.get("pieces"), "icon": s.get("icon")} for s in (b0.get("setPlan") or [])]}
# -------------------------
# Party recommendation (AI 추천 파티)
# -------------------------

_TIER_ALPHA = {"SS": 4.5, "S+": 4.2, "S": 4.0, "A+": 3.2, "A": 3.0, "B+": 2.2, "B": 2.0, "C": 1.0, "D": 0.0}

def _tier_value(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().upper()
    if not s:
        return 0.0
    # numeric-like
    try:
        return float(s)
    except Exception:
        pass
    # letter tiers
    if s in _TIER_ALPHA:
        return _TIER_ALPHA[s]
    # normalize variants like "S++"
    s2 = re.sub(r"[^A-Z\+]", "", s)
    if s2 in _TIER_ALPHA:
        return _TIER_ALPHA[s2]
    if s2.startswith("S"):
        return 4.0
    if s2.startswith("A"):
        return 3.0
    if s2.startswith("B"):
        return 2.0
    if s2.startswith("C"):
        return 1.0
    return 0.0


def _is_dps_class(cls: str) -> bool:
    c = (cls or "").strip().lower()
    return c in ("warrior", "rogue", "mage")


def _party_counts(members: list[dict]) -> dict:
    # archetype 기준 카운트 (buffer는 별도로 집계)
    cnt = {"tank": 0, "healer": 0, "buffer": 0, "debuffer": 0, "dps": 0}
    for m in members:
        a = str(m.get("archetype") or "").lower()
        if a in cnt:
            cnt[a] += 1
        else:
            cnt["dps"] += 1
    return cnt


def _combo_detail(members: list[dict]) -> dict:
    elem = {}
    fac = {}
    for m in members:
        e = str(m.get("element") or "").strip()
        f = str(m.get("faction") or "").strip()
        if e:
            elem[e] = elem.get(e, 0) + 1
        if f:
            fac[f] = fac.get(f, 0) + 1
    elem_hits = [k for k, v in elem.items() if v >= 2]
    fac_hits = [k for k, v in fac.items() if v >= 2]
    return {"element_hits": elem_hits, "faction_hits": fac_hits, "element_counts": elem, "faction_counts": fac}


def _member_payload(cid: str, tier: float, base: dict, detail: dict, role_override: Optional[str] = None) -> dict:
    prof = _detect_profile(detail or {}, base or {})

    # effective archetype/role for party composition
    role = (role_override or prof.get("role") or _role_from_base(base or {}) or "dps").strip().lower()
    if role not in ("tank", "dps", "healer", "buffer", "debuffer"):
        role = (prof.get("role") or "dps").strip().lower()
        if role not in ("tank", "dps", "healer", "buffer", "debuffer"):
            role = "dps"

    no_crit = detect_no_crit(detail or {})
    return {
        "id": cid,
        "name": base.get("name") or cid,
        "rarity": base.get("rarity"),
        "element": base.get("element"),
        "faction": base.get("faction"),
        "class": base.get("class"),
        "role": base.get("role"),
        "image": base.get("image"),
        "element_icon": base.get("element_icon"),
        "class_icon": base.get("class_icon"),
        # ✅ party role used by the optimizer (override-aware)
        "archetype": role,
        "scaling": prof.get("scaling") or "MIX",
        "no_crit": bool(no_crit),
        "tier": tier,
        "score": tier,  # UI에서 member.score로 표기
    }

def _score_party(
    members: list[dict],
    require_combo: bool,
    required_classes: list[str],
    combo_mode: str = "either",
    content_mode: str = "generic",
) -> tuple[float, dict]:
    """Return (score, meta).

    combo_mode:
      - "either": (same element 2+) OR (same faction 2+)
      - "both"  : (same element 2+) AND (same faction 2+)

    content_mode:
      - "pve" | "pvp" | "guild" | "rift" | "generic"

    Note:
      - 콤보는 기본적으로 "가산점"으로 반영한다.
      - require_combo=True인 경우에만 하드 제약으로 강제한다.
      - 최소 1 DPS는 하드 제약으로 강제한다. (buffer는 DPS로 간주하지 않음)
    """
    # base score: sum of tier
    total = sum(float(m.get("tier") or 0.0) for m in members)

    counts = _party_counts(members)

    mode = (content_mode or "generic").strip().lower()

    # ---- hard composition constraints ----
    if counts.get("dps", 0) < 1:
        total -= 9999.0

    if mode == "guild" and counts.get("dps", 0) < 2:
        total -= 9999.0

    # ---- soft composition scoring (content-aware) ----
    # 기본: 딜 1명은 이미 하드 제약, 이후는 가중치
    if mode == "pve":
        if counts["dps"] >= 2:
            total += 0.9
        if counts["tank"] >= 1:
            total += 0.5
        if counts["healer"] >= 1:
            total += 0.6
        if counts["buffer"] >= 1:
            total += 0.35
        if counts["debuffer"] >= 1:
            total += 0.35
        if counts["healer"] >= 2:
            total -= 0.6
        if counts["tank"] >= 2:
            total -= 0.4

    elif mode == "pvp":
        if counts["dps"] >= 2:
            total += 0.6
        if counts["tank"] >= 1:
            total += 0.6
        if counts["healer"] >= 1:
            total += 0.6
        if counts["debuffer"] >= 1:
            total += 0.35
        if counts["buffer"] >= 1:
            total += 0.25
        if counts["healer"] >= 2:
            total -= 0.5

    elif mode == "guild":
        # 레이드: 딜 2명 이상 선호
        if counts["dps"] >= 3:
            total += 0.7
        if counts["debuffer"] >= 1:
            total += 0.55
        if counts["buffer"] >= 1:
            total += 0.45
        if counts["tank"] >= 1:
            total += 0.25
        # 힐은 있으면 좋지만 과도하면 감점
        if counts["healer"] >= 1:
            total += 0.25
        if counts["healer"] >= 2:
            total -= 0.6

    elif mode == "rift":
        # Rift: 생존+디버프 밸런스
        if counts["dps"] >= 2:
            total += 0.7
        if counts["healer"] >= 1:
            total += 0.6
        if counts["tank"] >= 1:
            total += 0.4
        if counts["debuffer"] >= 1:
            total += 0.45
        if counts["buffer"] >= 1:
            total += 0.25
        if counts["healer"] >= 2:
            total -= 0.5

    else:
        # generic
        if counts["dps"] >= 2:
            total += 0.5
        if counts["healer"] >= 1 or counts["tank"] >= 1:
            total += 0.5
        if counts["debuffer"] >= 1:
            total += 0.3
        if counts["buffer"] >= 1:
            total += 0.2

    # ---- required class satisfaction (hard) ----
    req = [str(x).strip() for x in (required_classes or []) if str(x).strip()]
    if req:
        present = {str(m.get("class") or "").strip() for m in members}
        miss = [c for c in req if c not in present]
        if miss:
            total -= 9999.0  # invalid

    # ---- combo scoring ----
    combo = _combo_detail(members)
    cm = (str(combo_mode or "either").strip().lower() or "either")
    if cm not in ("either", "both"):
        cm = "either"

    # 가산점: 속성/특성 2+를 각각 반영
    # (서로 다른 히트가 여러 개면 조금 더 가산)
    elem_hits = combo.get("element_hits") or []
    fac_hits = combo.get("faction_hits") or []
    if elem_hits:
        total += 0.35 + 0.10 * max(0, len(elem_hits) - 1)
    if fac_hits:
        total += 0.35 + 0.10 * max(0, len(fac_hits) - 1)

    if require_combo:
        ok = False
        if cm == "either":
            ok = bool(elem_hits or fac_hits)
        else:
            ok = bool(elem_hits and fac_hits)
        if not ok:
            total -= 9999.0

    meta = {"counts": counts, "combo_detail": combo, "combo_mode": cm}
    return total, meta


def recommend_best_party(
    owned_ids: list[str],
    required_ids: list[str],
    required_classes: list[str],
    rank_map: dict,
    party_size: int = 4,
    top_k: int = 1,
    require_combo: bool = False,
    combo_mode: str = "either",
    required_overrides: Optional[dict] = None,
    content_mode: str = "generic",
    time_limit_ms: int = 4500,
    preloaded: Optional[dict] = None,
) -> dict:
    """Recommend best party.

    Performance:
      - candidate payload을 조합 평가 전에 캐시하여, 스킬/메모리카드 텍스트 분석 비용을 1회로 제한
      - 후보 풀은 tier 기반으로 상한을 두되, 역할 다양성(특히 DPS)을 보장
      - time_limit_ms 내에서만 탐색
    """
    load_all()

    required_overrides = required_overrides or {}

    # 캐시/인덱스
    if preloaded and isinstance(preloaded, dict) and preloaded.get("by_id") and preloaded.get("details"):
        by_id = preloaded["by_id"]
        details = preloaded["details"]
        payload_cache = preloaded.setdefault("payload_cache", {})
    else:
        by_id = {c.get("id"): c for c in (CACHE.get("chars") or []) if c.get("id")}
        details = (CACHE.get("details") or {})
        payload_cache = {}

    owned = [cid for cid in owned_ids if cid in by_id]
    if len(owned) < party_size:
        return {"ok": False, "error": f"owned must be >= {party_size}"}

    req = [cid for cid in (required_ids or []) if cid in by_id]
    if len(req) > party_size:
        return {"ok": False, "error": "required too many"}

    # role override map (party optimizer archetype override)
    ov_map = {}
    for cid, st in (required_overrides or {}).items():
        if not isinstance(st, dict):
            continue
        role = (st.get("role") or "").strip()
        if role:
            ov_map[str(cid)] = role

    # tier score per candidate (rank_map preferred)
    def tier_of(cid: str) -> float:
        v = None
        if isinstance(rank_map, dict):
            v = rank_map.get(cid)
        if v is None:
            v = by_id.get(cid, {}).get("tier")
        return _tier_value(v)

    # payload cache (expensive profile detection)
    def payload(cid: str) -> dict:
        if cid in payload_cache:
            return payload_cache[cid]
        base = by_id.get(cid) or {}
        det = details.get(cid) or {}
        p = _member_payload(cid, tier_of(cid), base, det, role_override=ov_map.get(cid))
        payload_cache[cid] = p
        return p

    # ---- build candidate pool ----
    # 기본: tier 상위, 역할 다양성(특히 DPS)
    required_set = set(req)

    # approximate archetype from base (cheap) for prefilter
    def approx_arch(cid: str) -> str:
        if cid in ov_map:
            return ov_map[cid].strip().lower()
        base = by_id.get(cid) or {}
        r = (_role_from_base(base) or "dps").strip().lower()
        if r not in ("tank", "dps", "healer", "buffer", "debuffer"):
            r = "dps"
        return r

    # sort owned by tier desc
    sorted_owned = sorted(owned, key=lambda c: tier_of(c), reverse=True)

    # role buckets
    buckets = {"dps": [], "tank": [], "healer": [], "buffer": [], "debuffer": []}
    for cid in sorted_owned:
        buckets.setdefault(approx_arch(cid), []).append(cid)

    # keep top overall + per-role top N
    pool_set = set(req)

    # overall cap depends on time + combination size
    # 4 choose 16 = 1820, 4 choose 18 = 3060 (ok) but repeated across parties -> cap lower
    overall_cap = 18
    if party_size == 4:
        overall_cap = 16

    for cid in sorted_owned:
        if len(pool_set) >= overall_cap:
            break
        pool_set.add(cid)

    role_targets = {
        "dps": 8,
        "tank": 3,
        "healer": 3,
        "buffer": 3,
        "debuffer": 3,
    }
    for role, n in role_targets.items():
        for cid in buckets.get(role, [])[:n]:
            if len(pool_set) >= overall_cap:
                break
            pool_set.add(cid)

    pool = [cid for cid in sorted_owned if cid in pool_set]

    # ensure required are present
    for cid in req:
        if cid not in pool:
            pool.append(cid)

    # remove required from selection pool for remaining picks
    selectable = [cid for cid in pool if cid not in required_set]

    # ---- combination search ----
    import time
    deadline = time.monotonic() + max(0.2, float(time_limit_ms or 4500)) / 1000.0

    need = party_size - len(req)
    if need == 0:
        members = [payload(cid) for cid in req]
        score, meta = _score_party(members, require_combo, required_classes, combo_mode=combo_mode, content_mode=content_mode)
        if score < -1000:
            return {"ok": False, "error": "no_valid_party"}
        return {
            "ok": True,
            "parties": [{"members": members, "total_score": score, "meta": meta}],
        }

    # pruning: if selectable too small
    if len(selectable) < need:
        return {"ok": False, "error": "not_enough_candidates"}

    best = []  # list of (score, party_members, meta)

    from itertools import combinations

    # Early checks: if required already violates hard constraints, skip
    req_payloads = [payload(cid) for cid in req]

    # iterate combinations
    for comb in combinations(selectable, need):
        if time.monotonic() > deadline:
            break
        mems = req_payloads + [payload(cid) for cid in comb]
        score, meta = _score_party(mems, require_combo, required_classes, combo_mode=combo_mode, content_mode=content_mode)
        if score < -1000:
            continue
        best.append((score, mems, meta))

    if not best:
        return {"ok": False, "error": "no_valid_party"}

    best.sort(key=lambda x: x[0], reverse=True)
    best = best[: max(1, int(top_k or 1))]

    parties = []
    for score, mems, meta in best:
        parties.append({"members": mems, "total_score": score, "meta": meta})

    return {"ok": True, "parties": parties}


def recommend_multi_parties(
    owned_ids: list[str],
    must_assignments: Optional[dict],
    required_overrides: dict,
    required_classes: list[str],
    rank_map: dict,
    party_size: int = 4,
    require_combo: bool = False,
    combo_mode: str = "either",
    target_category: Optional[str] = None,
    time_limit_ms: int = 4500,
) -> dict:
    """Recommend multi parties.

    - 같은 카테고리 내에서만 중복 불가 (파티 간 중복 제거)
    - 카테고리 간 중복은 허용 (콘텐츠가 별개)
    - target_category가 지정되면 해당 카테고리만 계산

    must_assignments format (front-end):
      {
        auto: [cid...],
        byCategory: {
          PVE:  [ [cid...] ],
          PVP:  [ [cid...], [cid...] ],
          Guild:[ [cid...],[cid...],[cid...] ],
          Rift: [ [cid...],[cid...] ]
        }
      }

    legacy:
      - Left 키는 Rift로 취급
    """
    load_all()

    by_id = {c.get("id"): c for c in (CACHE.get("chars") or []) if c.get("id")}
    details = (CACHE.get("details") or {})

    owned = [cid for cid in (owned_ids or []) if cid in by_id]
    if len(owned) < party_size:
        return {"ok": False, "error": f"owned must be >= {party_size}"}

    # canonical categories and party counts
    party_counts = {"PVE": 1, "PVP": 2, "Guild": 3, "Rift": 2}

    def canon_cat(s: str) -> str:
        k = (s or "").strip()
        if not k:
            return ""
        u = k.upper()
        if u in ("LEFT", "RIFT"):
            return "Rift"
        if u in ("GUILD", "GUILDRAID", "GUILD_RAID"):
            return "Guild"
        if u == "PVE":
            return "PVE"
        if u == "PVP":
            return "PVP"
        # preserve original casing if matches
        if k in party_counts:
            return k
        return k.title()

    # parse assignments
    auto_ids = []
    by_cat = {k: [[] for _ in range(n)] for k, n in party_counts.items()}

    if isinstance(must_assignments, dict):
        auto_ids = must_assignments.get("auto") or []
        if not isinstance(auto_ids, list):
            auto_ids = []
        bc = must_assignments.get("byCategory") or must_assignments.get("by_category") or {}
        if isinstance(bc, dict):
            for cat, arr in bc.items():
                c = canon_cat(cat)
                if c == "Rift":
                    c = "Rift"
                if c not in by_cat:
                    continue
                if not isinstance(arr, list):
                    continue
                # normalize length
                for i in range(min(len(arr), len(by_cat[c]))):
                    if isinstance(arr[i], list):
                        by_cat[c][i] = [x for x in arr[i] if x in by_id]

    auto_ids = [cid for cid in auto_ids if cid in by_id]

    # preloaded cache shared across parties for speed
    preloaded = {"by_id": by_id, "details": details, "payload_cache": {}}

    import time
    deadline = time.monotonic() + max(0.2, float(time_limit_ms or 4500)) / 1000.0

    # Determine which categories to compute
    cats = ["PVE", "PVP", "Guild", "Rift"]
    if target_category:
        tc = canon_cat(target_category)
        if tc == "Rift":
            tc = "Rift"
        # tc should match keys
        if tc and tc in party_counts:
            cats = [tc]

    warnings = []
    groups = {}

    # helper: distribute auto ids into per-party required list within a category
    def distribute_auto(cat_key: str, per_party: list[list[str]]):
        # already assigned in explicit lists
        assigned = set(x for slot in per_party for x in slot)
        remaining = [cid for cid in auto_ids if cid not in assigned]
        if not remaining:
            return
        pcount = len(per_party)
        pi = 0
        for cid in remaining:
            # find next party with room
            tries = 0
            placed = False
            while tries < pcount:
                idx = (pi + tries) % pcount
                if len(per_party[idx]) < party_size:
                    per_party[idx].append(cid)
                    placed = True
                    pi = idx + 1
                    break
                tries += 1
            if not placed:
                warnings.append(f"{cat_key}: 필수(자동) 캐릭터를 모두 배치하지 못했습니다. (파티 크기 초과)")
                break

    for cat in cats:
        # per-category available pool (카테고리 간 중복 허용)
        available = owned[:]

        # clone explicit required per party
        per_party_req = [list(x) for x in by_cat.get(cat) or [[] for _ in range(party_counts[cat])]]

        # apply auto must: target_category가 있으면 해당 카테고리에만, 없으면 모든 카테고리에 적용
        distribute_auto(cat, per_party_req)

        parties = []

        # content_mode mapping for scoring
        mode_map = {"PVE": "pve", "PVP": "pvp", "Guild": "guild", "Rift": "rift"}
        cmode = mode_map.get(cat, "generic")

        for idx in range(len(per_party_req)):
            if time.monotonic() > deadline:
                warnings.append("시간 제한으로 일부 파티만 계산했습니다.")
                break

            req_ids = [cid for cid in per_party_req[idx] if cid in available]
            # 너무 많으면 잘라내고 경고
            if len(req_ids) > party_size:
                warnings.append(f"{cat} {idx+1}파티: 필수 포함이 {party_size}명을 초과하여 일부를 제외했습니다.")
                req_ids = req_ids[:party_size]

            # call single-party recommender
            remaining_ms = int(max(120, (deadline - time.monotonic()) * 1000))
            res = recommend_best_party(
                owned_ids=available,
                required_ids=req_ids,
                required_classes=required_classes,
                rank_map=rank_map,
                party_size=party_size,
                top_k=1,
                require_combo=require_combo,
                combo_mode=combo_mode,
                required_overrides=required_overrides,
                content_mode=cmode,
                time_limit_ms=remaining_ms,
                preloaded=preloaded,
            )

            if not res.get("ok"):
                parties.append({"members": [], "total_score": 0, "meta": {"error": res.get("error")}})
                warnings.append(f"{cat} {idx+1}파티: 추천 실패({res.get('error')})")
                continue

            party = (res.get("parties") or [{}])[0]
            parties.append(party)

            # remove selected from available (same category 내 중복 방지)
            sel_ids = [m.get("id") for m in (party.get("members") or []) if m.get("id")]
            sel_set = set(sel_ids)
            available = [cid for cid in available if cid not in sel_set]

        if parties:
            groups[cat] = parties

    return {"ok": True, "groups": groups, "warnings": warnings, "combo_mode": combo_mode}


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
    CACHE["runes_source"] = None
    CACHE["runes_debug"] = None
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
        ,
        "runes": {
            "count": len(CACHE["runes_db"]) if isinstance(CACHE.get("runes_db"), list) else None,
            "source": CACHE.get("runes_source"),
            "debug": CACHE.get("runes_debug"),
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
        # rune recommendation supports ?rune_mode=pve|pvp|both
        rm = (request.args.get("rune_mode") or "both").strip().lower()
        if rm in ("pve", "pvp"):
            rune_reco = recommend_runes(cid2, base, detail, mode=rm)
        else:
            rune_reco = recommend_runes_both(cid2, base, detail)
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
        required_overrides = payload.get("required_overrides") or {}
        required_classes = payload.get("required_classes") or []
        rank_map = payload.get("rank_map") or {}
        party_size = payload.get("party_size") or 4
        top_k = payload.get("top_k") or 1
        require_combo = payload.get("require_combo")
        if not isinstance(require_combo, bool):
            require_combo = False

        if not isinstance(rank_map, dict):
            rank_map = {}

        combo_mode = payload.get("combo_mode") or "either"
        time_limit_ms = payload.get("time_limit_ms") or payload.get("timeLimitMs") or 4500
        try:
            time_limit_ms = int(time_limit_ms)
        except Exception:
            time_limit_ms = 4500

        # category/preset: front는 등급표 preset 기준으로 1개 카테고리만 추천 요청
        preset_key = (payload.get("preset_key") or payload.get("presetKey") or "")
        category = (payload.get("category") or payload.get("target_category") or payload.get("targetCategory") or "")
        target_category = category
        if not target_category and preset_key:
            pk = str(preset_key).strip().lower()
            if pk in ("pve",):
                target_category = "PVE"
            elif pk in ("pvp",):
                target_category = "PVP"
            elif pk in ("guild", "guild-raid", "guildraid"):
                target_category = "Guild"
            elif pk in ("rift", "left"):
                target_category = "Rift"
        must_assignments = payload.get("must_assignments") or payload.get("mustAssignments")
        multi = payload.get("multi")
        if not isinstance(multi, bool):
            # must_assignments가 있으면 multi로 판단
            multi = isinstance(must_assignments, dict)

        if multi:
            res = recommend_multi_parties(
                owned_ids=owned if isinstance(owned, list) else [],
                must_assignments=must_assignments if isinstance(must_assignments, dict) else None,
                required_overrides=required_overrides if isinstance(required_overrides, dict) else {},
                required_classes=required_classes if isinstance(required_classes, list) else [],
                rank_map=rank_map,
                party_size=int(party_size) if str(party_size).isdigit() else 4,
                require_combo=bool(require_combo),
                combo_mode=str(combo_mode),
                target_category=target_category or None,
                time_limit_ms=time_limit_ms,
            )
        else:
            res = recommend_best_party(
                owned_ids=owned if isinstance(owned, list) else [],
                required_ids=required if isinstance(required, list) else [],
                required_classes=required_classes if isinstance(required_classes, list) else [],
                rank_map=rank_map,
                party_size=int(party_size) if str(party_size).isdigit() else 4,
                top_k=int(top_k) if str(top_k).isdigit() else 1,
                require_combo=bool(require_combo),
                combo_mode=str(combo_mode),
                required_overrides=required_overrides,
                content_mode=(str(target_category).lower() if target_category else "generic"),
                time_limit_ms=time_limit_ms,
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
