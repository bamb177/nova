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

# ✅ 룬 데이터/수동 오버라이드
RUNES_JS = os.path.join(DATA_DIR, "runes.js")
RUNE_OVERRIDES = os.path.join(DATA_DIR, "rune_overrides.json")

# ✅ 이미지 경로(사용자 제공 경로/파일명)
CHAR_IMG_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters")
ELEM_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "element")
CLASS_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "classes")
RUNE_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "runes")

VALID_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}  # ✅ jpg만 고정하지 않기

ELEMENT_RENAME = {"Fire": "Blaze", "Wind": "Storm", "Ice": "Frost"}

app = Flask(__name__, static_folder="public", static_url_path="")

CACHE = {
    "chars": [],
    "details": {},
    "last_refresh": None,
    "error": None,
    "runes_db": None,
    "rune_overrides": None,
    "source": {
        "characters": "public/data/zone-nova/characters_ko/*.json",
        "overrides_names": "public/data/zone-nova/overrides_names.json",
        "overrides_factions": "public/data/zone-nova/overrides_factions.json",
        "runes_js": "public/data/zone-nova/runes.js",
        "rune_overrides": "public/data/zone-nova/rune_overrides.json",
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
    s2 = s[:1].upper() + s[1:].lower()  # Wind/Fire/Ice/Holy/Chaos
    return ELEMENT_RENAME.get(s2, s2)   # Blaze/Storm/Frost로 치환


def load_overrides() -> tuple[dict, dict]:
    names = safe_load_json(OVERRIDE_NAMES)
    factions = safe_load_json(OVERRIDE_FACTIONS)
    return (names if isinstance(names, dict) else {}), (factions if isinstance(factions, dict) else {})


def build_character_image_map(folder: str) -> dict:
    """
    characters 폴더의 모든 이미지 파일을 여러 키 형태 -> 파일명 매핑
    overrides_names로 표시명이 바뀌어도 raw_name / image 힌트 / id로 찾게 함.
    """
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

        # 공백/하이픈 제거 버전도
        compact = re.sub(r"[\s\-_]+", "", base_low)
        if compact:
            keys.add(compact)
            keys.add(slug_id(compact))

        # 숫자 prefix 제거 버전도
        stripped = re.sub(r"^[0-9]+[_\-\s]*", "", base_low).strip()
        if stripped:
            keys.add(stripped)
            keys.add(slug_id(stripped))

        for k in keys:
            if k and k not in m:
                m[k] = fn

    return m


def candidate_image_keys(cid: str, raw_name: str, display_name: str, image_hint: str | None) -> list[str]:
    """
    ✅ 우선순위:
    1) characters_ko의 image 힌트
    2) raw_name(오버라이드 적용 전 원본)
    3) cid(파일명이 id일 수도 있으니)
    4) display_name(오버라이드 적용 후)
    """
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
    """폴더 내에서 base name이 stem과 같은 파일을(대소문자 무시) 찾아 파일명 반환"""
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
    fn = find_file_by_stem(ELEM_ICON_DIR, element)  # Blaze/Storm/Frost/Chaos/Holy
    if fn:
        return f"/images/games/zone-nova/element/{fn}"
    return None


def class_icon_url(cls: str) -> str | None:
    if not cls or cls == "-":
        return None
    cls_clean = str(cls).strip()
    if not cls_clean:
        return None
    fn = find_file_by_stem(CLASS_ICON_DIR, cls_clean)  # Guardian/Healer/...
    if fn:
        return f"/images/games/zone-nova/classes/{fn}"
    return None


# =========================
# Rune DB loader (runes.js)
# =========================

FALLBACK_RUNES = [
    {
        "name": "Alpha",
        "twoPiece": "Attack Power +8%",
        "fourPiece": "Basic Attack Damage +30%",
        "icon": None,
    },
    {
        "name": "Shattered Foundation",
        "twoPiece": "Defense +12%",
        "fourPiece": "Shield Effectiveness +20%",
        "icon": None,
    },
    {
        "name": "Beth",
        "twoPiece": "Critical Hit Rate +6%",
        "fourPiece": "When HP >80%: Critical Hit Damage +24%",
        "icon": None,
    },
    {
        "name": "Zahn",
        "twoPiece": "HP +8%",
        "fourPiece": "After Ultimate: Take 5% less damage (10s)",
        "icon": None,
    },
    {
        "name": "Daleth",
        "twoPiece": "Healing Effectiveness +10%",
        "fourPiece": "Battle Start: Gain 1 Energy immediately",
        "icon": None,
    },
    {
        "name": "Epsilon",
        "twoPiece": "Attack Power +8%",
        "fourPiece": "After activating ultimate skill, team damage +10% (10s)",
        "icon": None,
        "note": "Same passive effect cannot stack",
    },
    {
        "name": "Hert",
        "twoPiece": "Extra Attack damage +20%",
        "fourPiece": "After dealing Extra Attack damage, Critical Hit Rate +15% (10s)",
        "icon": None,
        "note": "Guild raid only",
    },
    {
        "name": "Gimel",
        "twoPiece": "Continuous damage +20%",
        "fourPiece": "After dealing continuous damage, own attack power +2% (stacks up to 10, 5s)",
        "icon": None,
        "note": "Guild raid only",
    },
    {
        "name": "Giants",
        "twoPiece": "Attack power +8%",
        "fourPiece": "Debuffer only: after casting ultimate, targets take 10% increased damage (5s)",
        "icon": None,
        "classRestriction": ["Debuffer"],
        "note": "Guild raid only / Same passive effect cannot stack",
        "teamConflict": [],
    },
    {
        "name": "Tide",
        "twoPiece": "Defense +12%",
        "fourPiece": "Within 10s after combat starts, team's energy gain efficiency +30%",
        "icon": None,
        "note": "Guild raid only / Does not stack / Daleth 4-piece team effect disabled",
        "teamConflict": ["Daleth:4"],
    },
]


def _strip_js_comments(s: str) -> str:
    # remove // comments
    s = re.sub(r"//.*?$", "", s, flags=re.M)
    # remove /* */ comments
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return s


def _extract_js_literal(s: str) -> str | None:
    """
    runes.js 안에서 배열/오브젝트 리터럴 부분만 최대한 추출한다.
    - export default [...]
    - const X = [...]; export default X;
    - module.exports = [...]
    """
    if not s:
        return None
    s = _strip_js_comments(s).strip()

    # Prefer explicit export default literal
    m = re.search(r"export\s+default\s+(\[.*\]|\{.*\})\s*;?\s*$", s, flags=re.S)
    if m:
        return m.group(1).strip()

    # module.exports = literal
    m = re.search(r"module\.exports\s*=\s*(\[.*\]|\{.*\})\s*;?\s*$", s, flags=re.S)
    if m:
        return m.group(1).strip()

    # const X = literal;
    m = re.search(r"\bconst\s+\w+\s*=\s*(\[.*\]|\{.*\})\s*;?\s*$", s, flags=re.S)
    if m:
        return m.group(1).strip()

    # fallback: first top-level [ ... ] or { ... }
    i = s.find("[")
    j = s.rfind("]")
    if i != -1 and j != -1 and j > i:
        return s[i: j + 1].strip()

    i = s.find("{")
    j = s.rfind("}")
    if i != -1 and j != -1 and j > i:
        return s[i: j + 1].strip()

    return None


def _json_friendly(js: str) -> str:
    """
    매우 보수적으로 JS 리터럴을 JSON에 가깝게 변환.
    - trailing comma 제거
    - undefined -> null
    - unquoted keys를 "key": 로 변환
    - single quote -> double quote(단순 변환)
    """
    s = js.strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing comma
    s = re.sub(r"\bundefined\b", "null", s)
    # quote keys: { a: 1 } -> { "a": 1 }
    s = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1"\2"\3:', s)
    # single quote -> double quote (best-effort)
    s = re.sub(r"'", r'"', s)
    return s


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
            # 1) try pure json
            try:
                runes = json.loads(lit)
            except Exception:
                # 2) try json-friendly conversion
                try:
                    runes = json.loads(_json_friendly(lit))
                except Exception:
                    runes = None

    if not isinstance(runes, list):
        runes = FALLBACK_RUNES

    # normalize icon url if we have image field
    norm = []
    for r in runes:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or r.get("title") or "").strip()
        if not name:
            continue

        icon = r.get("icon")
        img = r.get("image") or r.get("img") or r.get("jpg") or r.get("file")
        if not icon and isinstance(img, str) and img:
            icon = f"/images/games/zone-nova/runes/{img.strip().lstrip('/')}"
        if isinstance(icon, str) and icon and not icon.startswith("/"):
            icon = f"/images/games/zone-nova/runes/{icon.strip().lstrip('/')}"

        norm.append({
            "name": name,
            "twoPiece": r.get("twoPiece") or r.get("two_piece") or r.get("2pc") or r.get("two") or "",
            "fourPiece": r.get("fourPiece") or r.get("four_piece") or r.get("4pc") or r.get("four") or "",
            "note": r.get("note") or "",
            "classRestriction": r.get("classRestriction") or r.get("class_restriction") or [],
            "teamConflict": r.get("teamConflict") or r.get("team_conflict") or [],
            "icon": icon if isinstance(icon, str) and icon else None,
        })

    CACHE["runes_db"] = norm
    return norm


def rune_icon_map() -> dict[str, str | None]:
    db = load_runes_db()
    m: dict[str, str | None] = {}
    for r in db:
        nm = str(r.get("name") or "").strip()
        if nm:
            m[nm] = r.get("icon")
    return m


# =========================
# Rune recommendation engine
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
            for k, vv in v.items():
                if k in ("description", "desc", "text", "effect", "effects", "alternativeConditions", "altConditions"):
                    walk(vv)
                else:
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


def _pct_hits(text: str, keys: list[str]) -> list[float]:
    """
    '120% attack power', '공격력의 120%' 등에서 120 수치 추출
    """
    hits: list[float] = []
    t = text.lower()

    # english: 120% attack power
    for k in keys:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*[^%\n]{0,20}\b" + re.escape(k) + r"\b", t):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    # korean: 공격력의 120%
    for k in keys:
        for m in re.finditer(re.escape(k) + r"\s*의\s*(\d+(?:\.\d+)?)\s*%", text):
            try:
                hits.append(float(m.group(1)))
            except Exception:
                pass

    return hits


def _detect_profile(detail: dict, base: dict) -> dict:
    """
    스킬 텍스트에서 ATK/HP/DEF 스케일링 및 키워드를 추정.
    """
    texts = []
    if isinstance(detail, dict):
        texts.extend(_collect_texts(detail.get("skills")))
        texts.extend(_collect_texts(detail.get("teamSkill") or detail.get("team_skill") or detail.get("team")))
    texts = [t for t in texts if isinstance(t, str)]

    atk_hits, hp_hits, def_hits = [], [], []
    heal_cnt = 0
    shield_cnt = 0
    dot_cnt = 0
    extra_cnt = 0
    ult_cnt = 0

    for t in texts:
        atk_hits += _pct_hits(t, ["attack power", "atk", "공격력"])
        hp_hits += _pct_hits(t, ["max hp", "hp", "체력", "생명"])
        def_hits += _pct_hits(t, ["defense", "def", "방어력"])

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

    if archetype == "tank" and shield_cnt <= 0 and heal_cnt > 0:
        archetype = "healer"

    profile = {
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
    }

    profile["healer_hybrid"] = bool(archetype == "healer" and atk_s >= 15.0 and (atk_s >= hp_s or atk_s >= def_s))
    return profile


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


def _substats_for(archetype: str, scaling: str) -> list[str]:
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


def _pick_sets(profile: dict, base: dict) -> tuple[list[dict], list[list[dict]]]:
    archetype = profile["archetype"]
    dot = profile["dot_cnt"] > 0
    extra = profile["extra_cnt"] > 0
    shield = profile["shield_cnt"] > 0
    ult = profile["ult_cnt"] > 0

    cls = str(base.get("class") or "")
    cls_l = cls.lower()

    primary: list[dict] = []
    alternates: list[list[dict]] = []

    if archetype == "tank":
        if shield:
            primary = [{"set": "Shattered Foundation", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
        else:
            primary = [{"set": "Zahn", "pieces": 4}, {"set": "Shattered Foundation", "pieces": 2}]
        return primary, alternates

    if archetype == "healer":
        primary = [{"set": "Daleth", "pieces": 4}, {"set": "Zahn", "pieces": 2}]
        if profile.get("healer_hybrid"):
            alternates.append([{"set": "Daleth", "pieces": 4}, {"set": "Beth", "pieces": 2}])
            alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Daleth", "pieces": 2}])
        return primary, alternates

    if archetype == "debuffer" and cls_l == "debuffer":
        primary = [{"set": "Giants", "pieces": 4}, {"set": "Beth", "pieces": 2}]
        alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        return primary, alternates

    if dot:
        primary = [{"set": "Gimel", "pieces": 4}, {"set": "Beth", "pieces": 2}]
        alternates.append([{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        return primary, alternates

    if extra:
        primary = [{"set": "Hert", "pieces": 4}, {"set": "Beth", "pieces": 2}]
        alternates.append([{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}])
        return primary, alternates

    primary = [{"set": "Alpha", "pieces": 4}, {"set": "Beth", "pieces": 2}]
    if ult:
        alternates.append([{"set": "Epsilon", "pieces": 4}, {"set": "Beth", "pieces": 2}])
    return primary, alternates


def recommend_runes(cid: str, base: dict, detail: dict) -> dict:
    overrides = load_rune_overrides()
    icon_map = rune_icon_map()

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
                sp.append({
                    "set": sname,
                    "pieces": int(it.get("pieces") or 0),
                    "icon": icon_map.get(sname),
                })
            builds.append({
                "title": str(b.get("title") or "Override"),
                "setPlan": sp,
                "slots": b.get("slots") or {},
                "substats": b.get("substats") or [],
                "notes": b.get("notes") or [],
            })
        return {
            "mode": "override",
            "profile": {"note": "rune_overrides.json 적용"},
            "builds": builds,
        }

    profile = _detect_profile(detail or {}, base or {})
    primary, alternates = _pick_sets(profile, base or {})

    def mk_build(title: str, setplan: list[dict]) -> dict:
        return {
            "title": title,
            "setPlan": [
                {"set": x["set"], "pieces": x["pieces"], "icon": icon_map.get(x["set"])}
                for x in setplan
            ],
            "slots": _slot_plan_for(profile["archetype"], profile["scaling"], base.get("element")),
            "substats": _substats_for(profile["archetype"], profile["scaling"]),
            "notes": [],
        }

    builds = [mk_build("추천(자동)", primary)]
    for idx, alt in enumerate(alternates[:3], start=1):
        builds.append(mk_build(f"대체안 {idx}", alt))

    notes = []
    db = load_runes_db()
    for b in builds:
        for s in b["setPlan"]:
            nm = s["set"]
            rdb = next((x for x in db if x.get("name") == nm), None)
            if not rdb:
                continue
            cr = rdb.get("classRestriction") or []
            if cr:
                notes.append(f"{nm} 4세트는 클래스 제한이 있습니다: {', '.join(map(str, cr))}")
            if rdb.get("note"):
                notes.append(f"{nm}: {rdb.get('note')}")
            if rdb.get("teamConflict"):
                notes.append(f"{nm}: 팀 세트 상충 주의 ({', '.join(map(str, rdb.get('teamConflict')))} )")

    seen = set()
    notes2 = []
    for n in notes:
        if n not in seen:
            seen.add(n)
            notes2.append(n)
    if notes2:
        for b in builds:
            b["notes"] = notes2

    return {"mode": "auto", "profile": profile, "builds": builds}


def rune_summary_for_list(cid: str, base: dict, detail: dict) -> dict | None:
    reco = recommend_runes(cid, base, detail)
    builds = reco.get("builds") or []
    if not builds:
        return None
    b0 = builds[0]
    return {
        "mode": reco.get("mode"),
        "sets": b0.get("setPlan") or [],
        "slots": b0.get("slots") or {},
    }


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
        "characters_ko_dir": CHAR_KO_DIR,
        "images": {
            "characters_dir": CHAR_IMG_DIR,
            "element_dir": ELEM_ICON_DIR,
            "classes_dir": CLASS_ICON_DIR,
            "runes_dir": RUNE_ICON_DIR,
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
        reco = recommend_runes(cid2, base, detail)
    except Exception as e:
        reco = {"mode": "error", "error": str(e), "builds": []}

    return jsonify({
        "ok": True,
        "id": cid2,
        "character": base,
        "detail": detail,
        "detail_source": f"public/data/zone-nova/characters_ko/{cid2}.json",
        "rune_reco": reco,
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
        last_refresh=CACHE["last_refresh"] or "",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    debug = os.getenv("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
