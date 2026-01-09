import os
import json
import re
import ast
from datetime import datetime, timezone
from typing import Optional, Any

from flask import Flask, jsonify, redirect, render_template

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
    '크리티컬 비활성/불가' 탐지.

    원칙:
    - 명시적 플래그/키(예: cannotCrit, canCrit=false)를 최우선 신뢰
    - critRate & critDmg가 "둘 다 0"으로 명시된 경우에만 no-crit으로 처리(과탐 방지)
    - 스킬 텍스트에 cannot crit/치명타 불가 등 문구가 있으면 no-crit으로 처리(누락 보완)
    """
    if not isinstance(detail, dict):
        return False

    # 1) explicit flags at top-level
    for k in ["noCrit", "no_crit", "cannotCrit", "cannot_crit", "critDisabled", "crit_disabled"]:
        v = detail.get(k)
        if v is True:
            return True

    for k in ["canCrit", "can_crit", "criticalEnabled", "critical_enabled"]:
        v = detail.get(k)
        if isinstance(v, bool) and v is False:
            return True

    # 2) stats-like container
    stats = detail.get("stats") or detail.get("stat") or detail.get("attributes") or detail.get("attribute")
    crit_rate = None
    crit_dmg = None
    can_crit = None

    def read_stat_dict(d: dict):
        nonlocal crit_rate, crit_dmg, can_crit
        for k, v in d.items():
            kk = str(k).lower()
            if kk in ("cancrit", "can_crit", "criticalenabled", "critical_enabled"):
                if isinstance(v, bool):
                    can_crit = v
            if kk in ("critrate", "crit_rate", "criticalrate", "critical_rate"):
                crit_rate = v
            if kk in ("critdmg", "crit_dmg", "criticaldmg", "critical_damage", "criticaldamage"):
                crit_dmg = v

    if isinstance(stats, dict):
        read_stat_dict(stats)
    elif isinstance(stats, list):
        for row in stats:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("stat") or "").lower()
            v = row.get("value")
            if name in ("critrate", "crit_rate", "criticalrate", "critical_rate"):
                crit_rate = v
            if name in ("critdmg", "crit_dmg", "criticaldmg", "critical_damage", "criticaldamage"):
                crit_dmg = v
            if name in ("cancrit", "can_crit", "criticalenabled", "critical_enabled"):
                if isinstance(v, bool):
                    can_crit = v

    if can_crit is False:
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

    # 3) only treat as no-crit when BOTH are explicitly 0 (and keys exist)
    cr = to_num(crit_rate)
    cd = to_num(crit_dmg)
    if (crit_rate is not None or crit_dmg is not None) and (cr is not None and cd is not None):
        if cr <= 0 and cd <= 0:
            return True

    # 4) text hint (skills/teamSkill)
    texts = []
    texts.extend(_collect_texts(detail.get("skills")))
    texts.extend(_collect_texts(detail.get("teamSkill") or detail.get("team_skill") or detail.get("team")))
    blob = "\n".join([t.lower() for t in texts if isinstance(t, str)])

    if any(k in blob for k in _KW_NO_CRIT):
        return True

    return False


def _detect_profile(detail: dict, base: dict) -> dict:
    texts = []
    if isinstance(detail, dict):
        texts.extend(_collect_texts(detail.get("skills")))
        texts.extend(_collect_texts(detail.get("teamSkill") or detail.get("team_skill") or detail.get("team")))

    atk_hits, hp_hits, def_hits = [], [], []
    heal_cnt = shield_cnt = dot_cnt = extra_cnt = ult_cnt = 0

    sample = {"ATK": None, "HP": None, "DEF": None}

    for t in texts:
        a = _pct_hits(t, ["attack power", "atk", "공격력"])
        h = _pct_hits(t, ["max hp", "hp", "체력", "생명"])
        d = _pct_hits(t, ["defense", "def", "방어력"])
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

    # ✅ 핵심 보정: DEF 스케일링이 우세하면 탱커 성향으로 승격
    # - healer/debuffer는 원 역할을 유지
    # - dps로 떨어진 케이스(Apep 등)를 방지
    if archetype == "dps":
        if scaling == "DEF" and def_s > 0 and def_s >= max(atk_s, hp_s) + 5.0:
            archetype = "tank"
        # HP 스케일 + 보호막/생존 키워드가 강하면 탱커로 승격(필요 시)
        elif scaling == "HP" and hp_s > 0 and hp_s >= atk_s + 5.0 and (shield_cnt > 0):
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


def _pick_sets(profile: dict, base: dict, no_crit: bool) -> tuple[list[dict], list[list[dict]], list[str]]:
    archetype = profile["archetype"]
    dot = profile["dot_cnt"] > 0
    extra = profile["extra_cnt"] > 0
    shield = profile["shield_cnt"] > 0
    ult = profile["ult_cnt"] > 0

    primary: list[dict] = []
    alternates: list[list[dict]] = []
    rationale: list[str] = []

    # off-piece 2set 선택: critless면 Beth 제외
    off2 = {"set": ("Epsilon" if no_crit else "Beth"), "pieces": 2}

    if archetype == "tank":
        if shield:
            primary = [{"set": "Shattered Foundation", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
            rationale.append("보호막/생존 키워드 감지 → 방어/보호막 세트 우선.")
        else:
            primary = [{"set": "Zahn", "pieces": 4}, {"set": "Shattered Foundation", "pieces": 2}]
            rationale.append("탱커 분류 → HP/피해감소 중심 세트 우선.")
        return primary, alternates, rationale

    if archetype == "healer":
        primary = [{"set": "Daleth", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
        rationale.append("힐러 분류 → 치유 효율/초반 에너지/생존 세트 우선.")
        if profile.get("healer_hybrid"):
            alternates.append([{"set": "Daleth", "pieces": 4}, {"set": ("Epsilon" if no_crit else "Beth"), "pieces": 2}])
            alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Daleth", "pieces": 2}])
            rationale.append("힐러지만 공격 스케일링이 강함(하이브리드) → 딜 보조 대체안 제공.")
        return primary, alternates, rationale

    if archetype == "debuffer":
        primary = [{"set": "Epsilon", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
        rationale.append("디버퍼 분류 → 궁극기/파티 기여 중심(Epsilon) 세트 우선.")
        if not no_crit:
            alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        return primary, alternates, rationale

    # dps
    if dot:
        primary = [{"set": "Gimel", "pieces": 4}, off2]
        alternates.append([{"set": "Alpha", "pieces": 4}, off2])
        rationale.append("지속피해(DOT) 키워드 감지 → DOT 강화 세트 우선.")
        return primary, alternates, rationale

    if extra:
        primary = [{"set": "Hert", "pieces": 4}, off2]
        alternates.append([{"set": "Alpha", "pieces": 4}, off2])
        rationale.append("추가공격 키워드 감지 → 추가공격 강화 세트 우선.")
        return primary, alternates, rationale

    primary = [{"set": "Alpha", "pieces": 4}, off2]
    rationale.append("기본 딜러 분류 → 범용 딜 세트(Alpha) 우선.")
    if ult:
        alternates.append([{"set": "Epsilon", "pieces": 4}, off2])
        rationale.append("궁극기/파티 기여 키워드 감지 → 팀 딜 보조(Epsilon) 대체안 제공.")
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
