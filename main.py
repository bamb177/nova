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


def _extract_js_literal(s: str) -> Optional[str]:
    if not s:
        return None

    src = s

    def _extract_balanced_from(pos: int) -> Optional[str]:
        # find first { or [ after pos
        i = pos
        n = len(src)
        while i < n and src[i] not in "[{":
            i += 1
        if i >= n:
            return None

        open_ch = src[i]
        close_ch = "]" if open_ch == "[" else "}"
        depth = 0
        j = i

        in_str = None  # one of ', ", `
        esc = False

        while j < n:
            ch = src[j]

            # string state
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":  # escape
                    esc = True
                elif ch == in_str:
                    in_str = None
                j += 1
                continue

            # comment state (only when not in string)
            if ch == "/" and j + 1 < n:
                nxt = src[j + 1]
                if nxt == "/":
                    # line comment
                    j += 2
                    while j < n and src[j] not in "\r\n":
                        j += 1
                    continue
                if nxt == "*":
                    # block comment
                    j += 2
                    while j + 1 < n and not (src[j] == "*" and src[j + 1] == "/"):
                        j += 1
                    j += 2
                    continue

            if ch in ("'", '"', "`"):
                in_str = ch
                j += 1
                continue

            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return src[i:j + 1]

            j += 1

        return None

    # 1) export default [ ... ] or { ... }
    m = re.search(r"export\s+default\b", src)
    if m:
        lit = _extract_balanced_from(m.end())
        if lit:
            return lit.strip()

        # export default IDENT;
        m2 = re.search(r"export\s+default\s+([A-Za-z_][A-Za-z0-9_]*)\s*;?", src[m.end():])
        if m2:
            ident = m2.group(1)
            # find const/let/var IDENT = ...
            m3 = re.search(rf"\b(?:const|let|var)\s+{re.escape(ident)}\s*=\s*", src)
            if m3:
                lit2 = _extract_balanced_from(m3.end())
                if lit2:
                    return lit2.strip()

    # 2) module.exports = ...
    m = re.search(r"module\.exports\s*=\s*", src)
    if m:
        lit = _extract_balanced_from(m.end())
        if lit:
            return lit.strip()

    # 3) fallback: first top-level [ ... ] or { ... }
    lit = _extract_balanced_from(0)
    return lit.strip() if lit else None

def _json_friendly(js: str) -> str:
    # JSON 파서 친화적으로 보정(마지막 시도용)
    s = js.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing comma
    s = re.sub(r"\bundefined\b", "null", s)
    # unquoted keys -> quoted keys
    s = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1"\2"\3:', s)
    # single quote -> double quote (best-effort, may not cover all cases)
    s = re.sub(r"'", r'"', s)
    return s


def _to_python_literal(js: str) -> str:
    """
    json.loads 실패 시 마지막 fallback: ast.literal_eval을 위한 Python 리터럴 변환.
    backtick(`...`) 문자열도 최대한 일반 문자열로 변환한다.
    """
    s = js.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing comma
    s = re.sub(r"\bnull\b", "None", s)
    s = re.sub(r"\btrue\b", "True", s, flags=re.I)
    s = re.sub(r"\bfalse\b", "False", s, flags=re.I)
    s = re.sub(r'([{{\[,]]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1"\2"\3:', s)

    # backtick 템플릿 문자열 -> 큰따옴표 문자열 (단순 치환, escape 보정)
    def _bt(m):
        body = m.group(1)
        body = body.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{body}"'

    s = re.sub(r"`((?:\\.|[^`])*)`", _bt, s)
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
        f"{set_name} icon",
        f"icon {set_name}",
        _norm_key(f"{set_name} icon"),
        _norm_key(f"icon {set_name}"),
    ]

    for c in candidates:
        k1 = (c or "").strip().lower()
        if k1 in rune_map:
            return f"/images/games/zone-nova/runes/{rune_map[k1]}"
        k2 = _norm_key(c)
        if k2 in rune_map:
            return f"/images/games/zone-nova/runes/{rune_map[k2]}"

    # ✅ 마지막 안전장치: 포함(contains) 매칭
    needle = _norm_key(set_name)
    if needle:
        for k, rel in rune_map.items():
            if needle in k or k in needle:
                return f"/images/games/zone-nova/runes/{rel}"

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


def _pct_hits(text: str, keys: list[str]) -> list[float]:
    hits: list[float] = []
    t = text.lower()

    # "120% attack power"
    for k in keys:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*[^%\n]{0,24}\b" + re.escape(k) + r"\b", t):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    # "공격력의 120%"
    for k in keys:
        for m in re.finditer(re.escape(k) + r"\s*의\s*(\d+(?:\.\d+)?)\s*%", text):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    return hits


def detect_no_crit(detail: dict) -> bool:
    """
    '캐릭터가 크리티컬이 없다' 케이스 탐지:
    - stats/attributes 등에서 crit/critical 관련 키가 존재하고 값이 0 또는 false 인 경우를 우선 신뢰
    """
    if not isinstance(detail, dict):
        return False

    # 1) direct flags
    for k in ["noCrit", "no_crit", "cannotCrit", "cannot_crit", "critDisabled", "crit_disabled"]:
        v = detail.get(k)
        if v is True:
            return True

    # 2) stats dict
    stats = detail.get("stats") or detail.get("stat") or detail.get("attributes") or detail.get("attribute")
    if isinstance(stats, dict):
        for k, v in stats.items():
            kk = str(k).lower()
            if "crit" in kk or "critical" in kk:
                if isinstance(v, (int, float)) and float(v) <= 0:
                    return True
                if isinstance(v, str) and v.strip() in ("0", "0.0"):
                    return True
                if v is False:
                    return True

    # 3) stats list rows
    if isinstance(stats, list):
        for row in stats:
            if isinstance(row, dict):
                name = str(row.get("name") or row.get("stat") or "").lower()
                if "crit" in name or "critical" in name:
                    v = row.get("value")
                    if isinstance(v, (int, float)) and float(v) <= 0:
                        return True
                    if isinstance(v, str) and v.strip() in ("0", "0.0"):
                        return True
                    if v is False:
                        return True

    # 4) generic deep scan: if any key with crit exists and explicitly false/0
    def deep_scan(obj) -> Optional[bool]:
        if isinstance(obj, dict):
            for k, v in obj.items():
                kk = str(k).lower()
                if "crit" in kk or "critical" in kk:
                    if v is False:
                        return True
                    if isinstance(v, (int, float)) and float(v) <= 0:
                        return True
                    if isinstance(v, str) and v.strip() in ("0", "0.0"):
                        return True
                r = deep_scan(v)
                if r:
                    return True
        elif isinstance(obj, list):
            for it in obj:
                r = deep_scan(it)
                if r:
                    return True
        return None

    return bool(deep_scan(detail))


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



def _is_no_crit(detail: dict) -> bool:
    """캐릭터가 크리티컬 자체를 사용하지 못하거나(또는 크리 스탯이 0으로 고정) 추정되는 경우 True."""
    if not isinstance(detail, dict):
        return False

    # 명시 플래그 우선
    flag_keys_true = ["noCrit", "no_crit", "cannotCrit", "cannot_crit", "critDisabled", "criticalDisabled"]
    for k in flag_keys_true:
        v = detail.get(k)
        if isinstance(v, bool) and v is True:
            return True

    flag_keys_false = ["canCrit", "can_crit", "critEnabled", "criticalEnabled"]
    for k in flag_keys_false:
        v = detail.get(k)
        if isinstance(v, bool) and v is False:
            return True

    max_crit = 0.0
    seen_any = False

    def walk(obj):
        nonlocal max_crit, seen_any
        if obj is None:
            return
        if isinstance(obj, dict):
            for kk, vv in obj.items():
                if isinstance(kk, str) and "crit" in kk.lower():
                    if isinstance(vv, (int, float)):
                        seen_any = True
                        max_crit = max(max_crit, float(vv))
                    elif isinstance(vv, str):
                        mm = re.search(r"[-+]?\d+(?:\.\d+)?", vv)
                        if mm:
                            seen_any = True
                            max_crit = max(max_crit, float(mm.group(0)))
                walk(vv)
        elif isinstance(obj, list):
            for it in obj:
                walk(it)

    # stats/baseStats 우선, 없으면 전체 스캔
    walk(detail.get("stats") or detail.get("baseStats") or detail.get("base_stats") or detail)

    # 크리 관련 키가 존재하고 값이 0 이하로만 나오면 "크리 없음"으로 본다
    if seen_any and max_crit <= 0:
        return True
    return False

def _element_damage_label(element: str) -> str:
    e = normalize_element(element or "-")
    if e in ("Storm", "Blaze", "Frost", "Holy", "Chaos"):
        return f"{e} Attribute Damage (%)"
    return "Element Attribute Damage (%)"


def _slot_plan_for(archetype: str, scaling: str, element: str, no_crit: bool = False) -> dict:
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

    # DPS / Debuffer (damage)
    if no_crit:
        # 크리 미지원 캐릭터: 치확/치피 제외
        plan["4"] = ["Attack Penetration (%)", "Attack (%)", "Energy Regen (%) (필요 시)", "HP (%) (생존)"]
        plan["5"] = [_element_damage_label(element), "Attack (%)", "Attack Penetration (%)", "HP (%) (생존)"]
        plan["6"] = ["Attack (%)", "Attack Penetration (%)", "HP (%) (생존)", "Defense (%) (생존)"]
        return plan

    plan["4"] = ["Critical Rate (%)", "Attack Penetration (%)", "Critical Damage (%)", "Attack (%)"]
    plan["5"] = [_element_damage_label(element), "Attack (%)", "HP (%)", "Defense (%)"]
    plan["6"] = ["Attack (%)", "HP (%)", "Defense (%)"]
    return plan

def _substats_for(archetype: str, scaling: str, no_crit: bool = False) -> list[str]:
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
        return [
            "Attack (%)",
            "Attack Penetration (%)",
            "Element Attribute Damage (%)",
            "Flat Attack",
            "HP (%) / Defense (%) (생존)",
        ]

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

def _pick_sets(profile: dict, base: dict, no_crit: bool = False) -> tuple[list[dict], list[str]]:
    """
    추천 메인 1개만 반환 (대체안 제거).
    no_crit=True이면 치확/치피 기반 세트(Beth 등) 비선호.
    """
    archetype = profile["archetype"]
    dot = profile["dot_cnt"] > 0
    extra = profile["extra_cnt"] > 0
    shield = profile["shield_cnt"] > 0

    cls_l = str(base.get("class") or "").lower()

    primary: list[dict] = []
    rationale: list[str] = []

    if archetype == "tank":
        if shield:
            primary = [{"set": "Shattered Foundation", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
            rationale.append("보호막/생존 키워드 감지 → 방어/보호막 세트 우선.")
        else:
            primary = [{"set": "Zahn", "pieces": 4}, {"set": "Shattered Foundation", "pieces": 2}]
            rationale.append("탱커 분류 → HP/피해감소 중심 세트 우선.")
        return primary, rationale

    if archetype == "healer":
        primary = [{"set": "Daleth", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
        rationale.append("힐러 분류 → 치유 효율/초반 에너지/생존 세트 우선.")
        if profile.get("healer_hybrid") and not no_crit:
            # 하이브리드(공격도 의미있고 크리 사용 가능할 때)인 경우만 치명 2세트 고려
            primary = [{"set": "Daleth", "pieces": 4}, {"set": "Beth", "pieces": 2}]
            rationale.append("힐러지만 ATK 스케일링이 강함(하이브리드) → 치명 2세트 병행.")
        return primary, rationale

    if archetype == "debuffer" and cls_l == "debuffer":
        primary = [{"set": "Giants", "pieces": 4}, {"set": ("Epsilon" if no_crit else "Beth"), "pieces": 2}]
        rationale.append("디버퍼 분류 → 파티 증뎀(디버퍼 전용) 세트 우선.")
        if no_crit:
            rationale.append("크리 미지원 → 치명 2세트 대신 공격 2세트(Epsilon) 적용.")
        return primary, rationale

    if dot:
        primary = [{"set": "Gimel", "pieces": 4}, {"set": ("Epsilon" if no_crit else "Beth"), "pieces": 2}]
        rationale.append("지속피해(DOT) 키워드 감지 → DOT 강화 세트 우선.")
        if no_crit:
            rationale.append("크리 미지원 → 치명 2세트 대신 공격 2세트(Epsilon) 적용.")
        return primary, rationale

    if extra:
        primary = [{"set": "Hert", "pieces": 4}, {"set": ("Epsilon" if no_crit else "Beth"), "pieces": 2}]
        rationale.append("추가공격 키워드 감지 → 추가공격 강화 세트 우선.")
        if no_crit:
            rationale.append("크리 미지원 → 치명 2세트 대신 공격 2세트(Epsilon) 적용.")
        return primary, rationale

    # 기본 딜러
    if no_crit:
        primary = [{"set": "Alpha", "pieces": 4}, {"set": "Epsilon", "pieces": 2}]
        rationale.append("기본 딜러 분류 + 크리 미지원 → 공격/팀딜 중심 세트 우선.")
        return primary, rationale

    primary = [{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}]
    rationale.append("기본 딜러 분류 → 기본 공격/치명 세트 우선.")
    return primary, rationale

def recommend_runes(cid: str, base: dict, detail: dict) -> dict:
    try:
        overrides = load_rune_overrides()
        icon_map = rune_icon_map()
        db = load_runes_db()
        db_by_name = {str(x.get("name") or "").strip(): x for x in db if isinstance(x, dict)}
    
        # 1) manual override
        ov = overrides.get(cid)
        if isinstance(ov, dict) and ov.get("builds"):
            # ✅ 운영 편의: override도 1개만 사용 (첫번째만)
            b = (ov.get("builds") or [None])[0]
            if not isinstance(b, dict):
                return {"mode": "override", "profile": {"note": "rune_overrides.json 적용"}, "builds": []}
    
            sp = []
            for it in b.get("setPlan") or []:
                if not isinstance(it, dict):
                    continue
                sname = str(it.get("set") or "").strip()
                rdb = db_by_name.get(sname) or {}
                sp.append({
                    "set": sname,
                    "pieces": int(it.get("pieces") or 0),
                    "icon": icon_map.get(sname),
                    "twoPiece": rdb.get("twoPiece") or "",
                    "fourPiece": rdb.get("fourPiece") or "",
                    "note": rdb.get("note") or "",
                })
    
            build = {
                "title": str(b.get("title") or "추천(수동)"),
                "setPlan": sp,
                "slots": b.get("slots") or {},
                "substats": b.get("substats") or [],
                "notes": b.get("notes") or [],
                "rationale": b.get("rationale") or ["rune_overrides.json 수동 오버라이드 적용"],
            }
            return {"mode": "override", "profile": {"note": "rune_overrides.json 적용"}, "builds": [build]}
    
        # 2) auto
        profile = _detect_profile(detail or {}, base or {})
        no_crit = _is_no_crit(detail or {})
    
        primary, rationale = _pick_sets(profile, base or {}, no_crit=no_crit)
    
        sample_text = profile.get("sample_text")
        if sample_text:
            rationale = rationale + [f"스케일링 판정({profile.get('scaling')}): '{sample_text[:120]}'"]
        if no_crit:
            rationale = rationale + ["크리티컬 미지원(또는 크리 스탯 0) 감지 → 치확/치피 기반 추천 제외."]
    
        def mk_set_item(x: dict) -> dict:
            sname = x.get("set")
            rdb = db_by_name.get(sname) or {}
            return {
                "set": sname,
                "pieces": x.get("pieces"),
                "icon": icon_map.get(sname),
                "twoPiece": rdb.get("twoPiece") or "",
                "fourPiece": rdb.get("fourPiece") or "",
                "note": rdb.get("note") or "",
                "classRestriction": rdb.get("classRestriction") or [],
                "teamConflict": rdb.get("teamConflict") or [],
            }
    
        build = {
            "title": "추천(자동)",
            "setPlan": [mk_set_item(x) for x in primary],
            "slots": _slot_plan_for(profile["archetype"], profile["scaling"], base.get("element"), no_crit=no_crit),
            "substats": _substats_for(profile["archetype"], profile["scaling"], no_crit=no_crit),
            "notes": [],
            "rationale": rationale,
        }
    
        # rune db notes (restrictions/conflicts)
        notes = []
        for s in build["setPlan"]:
            nm = s.get("set")
            cr = s.get("classRestriction") or []
            if cr:
                notes.append(f"{nm} 4세트는 클래스 제한이 있습니다: {', '.join(map(str, cr))}")
            if s.get("note"):
                notes.append(f"{nm}: {s.get('note')}")
            tc = s.get("teamConflict") or []
            if tc:
                notes.append(f"{nm}: 팀 세트 상충 주의 ({', '.join(map(str, tc))})")
    
        seen, uniq_notes = set(), []
        for n in notes:
            if n not in seen:
                seen.add(n)
                uniq_notes.append(n)
        build["notes"] = uniq_notes
    
        return {"mode": "auto", "profile": {**profile, "no_crit": no_crit}, "builds": [build]}
    except Exception as e:
        msg = str(e)
        return {
            "mode": "error",
            "error": msg,
            "builds": [{
                "title": "추천 실패",
                "setPlan": [],
                "slots": {},
                "substats": [],
                "notes": [],
                "rationale": [f"추천 엔진 오류: {msg}"],
            }],
        }

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
        msg = str(e)
        rune_reco = {
            "mode": "error",
            "error": msg,
            "builds": [{
                "title": "추천 실패",
                "setPlan": [],
                "slots": {},
                "substats": [],
                "notes": [],
                "rationale": [f"추천 엔진 오류: {msg}"],
            }],
        }

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
