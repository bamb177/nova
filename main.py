import os
import json
import re
from datetime import datetime, timezone
from flask import Flask, request, Response, redirect, jsonify, render_template

APP_TITLE = os.getenv("APP_TITLE", "Nova")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "public", "data", "zone-nova")
IMG_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters")

# 아이콘(네 리포지토리)
ELEM_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "element")
CLASS_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "classes")

# 캐릭터 데이터(2개 소스 병합: meta 우선 + characters로 보강)
CHAR_META_JSON = os.path.join(DATA_DIR, "characters_meta.json")
CHAR_JSON = os.path.join(DATA_DIR, "characters.json")

# 오버라이드(이전 수정 값 유지)
OVERRIDES_NAMES_JSON = os.path.join(DATA_DIR, "overrides_names.json")
OVERRIDES_FACTIONS_JSON = os.path.join(DATA_DIR, "overrides_factions.json")

ELEM_JSON = os.path.join(DATA_DIR, "element_chart.json")
BOSS_JSON = os.path.join(DATA_DIR, "bosses.json")

app = Flask(__name__, static_folder="public", static_url_path="")

RARITY_SCORE = {"SSR": 30, "SR": 18, "R": 10, "-": 0}
RARITY_ORDER = {"SSR": 3, "SR": 2, "R": 1, "-": 0}
VALID_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# ===== 클래스/역할 정의 =====
CLASS_SET = {"buffer", "debuffer", "guardian", "healer", "mage", "rogue", "warrior"}
ROLE_SET = {"buffer", "dps", "debuffer", "healer", "tank"}

# 클래스 -> 역할 매핑
CLASS_TO_ROLE = {
    "buffer": "buffer",
    "debuffer": "debuffer",
    "healer": "healer",
    "guardian": "tank",
    "mage": "dps",
    "rogue": "dps",
    "warrior": "dps",
}

# 파벌 표준화(요청 반영)
FACTION_NAME_MAP_FALLBACK = {
    "A.S.A": "Asa",
    "Bicta Tower": "Bikta",
    "Chemic": "Kemich",
    "Monochrome Nation": "Monochrome Realm",
    "Oduis": "Otis",
    "Pingjing City": "Heikyo Castle",
    "Sapphire": "Safir",
}

# 이름 표준화(요청 반영) - overrides_names.json이 있으면 그게 우선
NAME_OVERRIDE_FALLBACK = {
    "Greed Mammon": "Mammon",
    "Kela": "Clara",
    "Morgan": "Morgan Le Fay",
    "Leviathan": "Behemoth",
    "Snow Girl": "Yuki-onna",
    "Shanna": "Saya",
    "Naiya": "Naya",
    "Afrodite": "Aphrodite",
    "apep": "Apep",
    "Belphegar": "Belphegor",
    "Chiya": "Cynia",
    "Freye": "Frigga",
    "gaia": "Gaia",
    "Jeanne D Arc": "Joan of Arc",
    "Penny": "Pennie",
    "Yuis": "Zeus",
}

CACHE = {
    "chars": [],
    "bosses": [],
    "element_adv": {"Fire": "Wind", "Wind": "Ice", "Ice": "Holy", "Holy": "Chaos", "Chaos": "Fire"},
    "last_refresh": None,
    "source": {
        "characters": None,
        "element_chart": "public/data/zone-nova/element_chart.json",
        "bosses": "public/data/zone-nova/bosses.json"
    },
    "error": None,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def read_json_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_read_json(path: str, default):
    try:
        if os.path.isfile(path):
            return read_json_file(path)
    except Exception:
        pass
    return default


def build_file_map(folder: str) -> dict:
    """
    폴더 스캔 → 다양한 키로 파일 매칭 가능하도록 맵 생성
    key: 정규화된 베이스명(lower)
    value: 실제 파일명
    """
    m = {}
    if not os.path.isdir(folder):
        return m

    pri = {".jpg": 4, ".jpeg": 4, ".png": 3, ".webp": 2}

    for fn in os.listdir(folder):
        ext = os.path.splitext(fn)[1].lower()
        if ext not in VALID_IMG_EXT:
            continue

        base = os.path.splitext(fn)[0]
        base_low = base.lower()

        keys = {base_low}
        keys.add(slug_id(base))

        # classes 폴더: 1*.jpg 같은 케이스 대응(선행 숫자+구분자 제거)
        stripped = re.sub(r"^[0-9]+[_\-\s]*", "", base_low).strip()
        if stripped:
            keys.add(stripped)
            keys.add(slug_id(stripped))

        # 공백/하이픈/언더스코어 제거 버전도 추가
        compact = re.sub(r"[\s\-_]+", "", base_low)
        if compact:
            keys.add(compact)
            keys.add(slug_id(compact))

        for k in keys:
            if k not in m:
                m[k] = fn
            else:
                cur_ext = os.path.splitext(m[k])[1].lower()
                if pri.get(ext, 0) > pri.get(cur_ext, 0):
                    m[k] = fn

    return m


def normalize_char_name(name: str) -> str:
    name = (name or "").replace("’", "'").strip()
    name = " ".join(name.split())
    return name


def _norm_key(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", (s or "").strip().lower())


def load_overrides() -> tuple[dict, dict]:
    """
    (names_override, factions_override)
    - 파일이 있으면 파일 우선
    - 없으면 fallback 사용
    """
    names = dict(NAME_OVERRIDE_FALLBACK)
    factions = dict(FACTION_NAME_MAP_FALLBACK)

    f_names = safe_read_json(OVERRIDES_NAMES_JSON, {})
    if isinstance(f_names, dict) and f_names:
        # 파일이 있으면 fallback 위에 덮어씀(파일 우선)
        names.update({str(k): str(v) for k, v in f_names.items()})

    f_factions = safe_read_json(OVERRIDES_FACTIONS_JSON, {})
    if isinstance(f_factions, dict) and f_factions:
        factions.update({str(k): str(v) for k, v in f_factions.items()})

    return names, factions


def apply_name_override(cid: str, name: str, names_map: dict) -> str:
    """
    id 기반 / name 기반 모두 대응
    """
    cid = (cid or "").strip()
    name = (name or "").strip()

    # 1) id 완전일치(대소문자/공백 제거 포함)
    if cid:
        for k in (cid, cid.lower(), _norm_key(cid)):
            if k in names_map:
                return str(names_map[k]).strip()

    # 2) name 일치(원본/소문자/컴팩트/slug)
    candidates = [
        name,
        name.lower(),
        _norm_key(name),
        slug_id(name),
    ]
    for k in candidates:
        if k in names_map:
            return str(names_map[k]).strip()

    # 3) keys도 정규화 비교(파일이 "Greed Mammon" 형태로 들어간 케이스)
    nk = _norm_key(name)
    for kk, vv in names_map.items():
        if _norm_key(kk) == nk:
            return str(vv).strip()

    return name


def apply_faction_override(faction: str, factions_map: dict) -> str:
    f = (faction or "").strip()
    if not f:
        return ""
    if f in factions_map:
        return str(factions_map[f]).strip()

    # 정규화 비교
    fk = _norm_key(f)
    for kk, vv in factions_map.items():
        if _norm_key(kk) == fk:
            return str(vv).strip()

    return f


def normalize_class(v: str) -> str:
    """
    입력이 class일 수도 있고 role일 수도 있음.
    class(7)로 정규화. 못 맞추면 '-'.
    """
    s = (v or "").strip()
    if not s:
        return "-"

    low = s.lower()

    # 흔한 변형 보정
    alias = {
        "guard": "guardian",
        "guardian": "guardian",
        "tank": "guardian",       # role이 들어온 경우
        "dps": "warrior",         # role이 들어온 경우
        "mage": "mage",
        "rogue": "rogue",
        "warrior": "warrior",
        "healer": "healer",
        "buffer": "buffer",
        "debuffer": "debuffer",
        "support": "buffer",
        "attacker": "warrior",

        # UI 표기 변경(Disruptor) 들어오는 경우도 debuffer로 수렴
        "disruptor": "debuffer",
    }

    if low in CLASS_SET:
        return low

    if low in alias:
        return alias[low]

    return "-"  # 알 수 없는 값


def role_from_class(cls: str, cid: str) -> str:
    """
    class -> role(5) 변환. Apep 예외 반영.
    """
    if not cls or cls == "-":
        return "-"

    # Apep: Warrior라도 Tank 가능 -> role을 Tank로 처리
    if cid == "apep" and cls == "warrior":
        return "tank"

    return CLASS_TO_ROLE.get(cls, "-")


def candidate_image_keys(cid: str, name: str) -> list[str]:
    """
    Jeanne D Arc 같은 케이스까지 최대한 매칭 키를 많이 생성
    """
    out = []
    cid = (cid or "").strip()
    nm = (name or "").strip()

    def add(x: str):
        x = (x or "").strip()
        if not x:
            return
        out.append(x.lower())
        out.append(slug_id(x))
        out.append(re.sub(r"[\s\-_]+", "", x.lower()))
        out.append(slug_id(re.sub(r"[\s\-_]+", "", x)))

    add(nm)
    add(nm.replace("'", ""))
    add(nm.replace("’", ""))
    add(nm.replace(" ", ""))
    add(cid)

    # Jeanne D Arc 특별히 더 보강
    if cid == "jeannedarc" or "jeanne" in cid:
        add("Jeanne D Arc")
        add("JeanneDArc")
        add("Joan of Arc")
        add("JoanofArc")

    # 중복 제거
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def extract_char_list(raw) -> list[dict]:
    """
    지원:
    1) [ {...}, {...} ]
    2) { "characters": [ ... ] }
    3) { "characters": { "id": {...} } }  (맵)
    4) { "id": {...}, "id2": {...} }      (맵)
    """
    if isinstance(raw, list):
        return raw

    if not isinstance(raw, dict):
        raise RuntimeError("캐릭터 JSON 포맷 오류: 최상위가 list/dict가 아닙니다.")

    v = raw.get("characters")
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        out = []
        for k, val in v.items():
            if isinstance(val, dict):
                item = dict(val)
                item["_id"] = k
                out.append(item)
        return out

    dict_values = list(raw.values())
    dict_like_cnt = sum(1 for x in dict_values if isinstance(x, dict))
    if dict_like_cnt >= 3:
        out = []
        for k, val in raw.items():
            if isinstance(val, dict):
                item = dict(val)
                item["_id"] = k
                out.append(item)
        return out

    raise RuntimeError("캐릭터 JSON 포맷 오류: list 또는 {characters:[...]} 또는 맵 형태여야 합니다.")


def load_and_merge_character_sources() -> tuple[list[dict], str]:
    """
    characters_meta.json(우선) + characters.json(보강) 병합
    - Apep/Gaia 같은 누락을 characters.json에서 채움
    """
    sources = []
    src_names = []

    if os.path.isfile(CHAR_META_JSON):
        sources.append(safe_read_json(CHAR_META_JSON, {}))
        src_names.append("characters_meta.json")

    if os.path.isfile(CHAR_JSON):
        sources.append(safe_read_json(CHAR_JSON, {}))
        src_names.append("characters.json")

    if not sources:
        raise RuntimeError("캐릭터 JSON을 찾지 못했습니다: characters_meta.json / characters.json")

    # 우선순위: meta -> characters
    merged: dict[str, dict] = {}

    for raw in sources:
        lst = extract_char_list(raw)
        for c in lst:
            if not isinstance(c, dict):
                continue
            cid = (c.get("id") or c.get("_id") or "").strip()
            name = (c.get("name") or "").strip()
            if not cid:
                cid = slug_id(name)
            cid = slug_id(cid)
            if not cid:
                continue

            if cid not in merged:
                merged[cid] = dict(c)
            else:
                # 기존(meta)에 없던 필드를 characters.json에서 보강
                for k, v in c.items():
                    if k not in merged[cid] or merged[cid].get(k) in (None, "", "-", []):
                        merged[cid][k] = v

    return list(merged.values()), " + ".join(src_names)


def normalize_chars(raw) -> list[dict]:
    chars = extract_char_list(raw) if not isinstance(raw, list) else raw

    names_override, factions_override = load_overrides()

    char_img_map = build_file_map(IMG_DIR)
    elem_icon_map = build_file_map(ELEM_ICON_DIR)
    class_icon_map = build_file_map(CLASS_ICON_DIR)

    out = []
    seen = set()

    for c in chars:
        if not isinstance(c, dict):
            continue

        name = normalize_char_name(c.get("name") or "")
        cid = (c.get("id") or c.get("_id") or "").strip()
        if not cid:
            cid = slug_id(name)
        cid = slug_id(cid)
        if not cid or cid in seen:
            continue
        seen.add(cid)

        # ===== 이름 오버라이드 =====
        name = apply_name_override(cid, name, names_override)

        rarity = (c.get("rarity") or "-").strip().upper()
        element = (c.get("element") or "-").strip()

        # 파벌
        faction_raw = c.get("faction") or c.get("Faction") or c.get("group") or c.get("Group") or ""
        faction = apply_faction_override(str(faction_raw), factions_override)

        # ===== 클래스/역할 분리 =====
        cls_raw = c.get("class") or c.get("Class") or c.get("job") or c.get("Job") or c.get("type") or c.get("Type") or c.get("role") or c.get("Role")
        cls = normalize_class(str(cls_raw) if cls_raw is not None else "")

        # Jeanne D Arc 정규화(이전 오류 방지)
        if slug_id(name) in {"jeannedarc", "joanofarc"} or cid in {"jeannedarc", "joanofarc"} or "jeanne" in cid:
            cid = "jeannedarc"
            # 표기 자체는 overrides_names.json에 따라갈 수 있으니 name은 유지하되,
            # 이미지 매칭을 위해 Jeannedarc alias는 아래 candidate_image_keys에서 보강
            if not name:
                name = "Jeanne D Arc"

        role = role_from_class(cls, cid)

        # ===== 캐릭터 이미지 매칭 =====
        image_url = None

        # 1) 매핑 테이블(일부 케이스)
        special = {
            "snowgirl": "Snow",
            "morganlefay": "Morgan",
            "morganle_fay": "Morgan",
            "jeannedarc": "Jeanne D Arc",
        }
        forced_base = special.get(cid)
        if forced_base:
            for k in candidate_image_keys(cid, forced_base):
                real = char_img_map.get(k)
                if real:
                    image_url = f"/images/games/zone-nova/characters/{real}"
                    break

        # 2) 일반 키 매칭
        if not image_url:
            for k in candidate_image_keys(cid, name):
                real = char_img_map.get(k)
                if real:
                    image_url = f"/images/games/zone-nova/characters/{real}"
                    break

        # 3) Jeanne D Arc 최종 보강
        if not image_url and cid == "jeannedarc" and os.path.isdir(IMG_DIR):
            picked = None
            for fn in os.listdir(IMG_DIR):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in VALID_IMG_EXT:
                    continue
                base = os.path.splitext(fn)[0]
                sb = slug_id(base)
                if sb == "jeannedarc" or "jeanne" in sb or "joanofarc" in sb:
                    picked = fn
                    break
            if picked:
                image_url = f"/images/games/zone-nova/characters/{picked}"

        # ===== 아이콘(네 리포지토리) =====
        elem_icon = None
        if element and element != "-":
            ek = element.lower()
            real = elem_icon_map.get(ek) or elem_icon_map.get(slug_id(ek))
            if real:
                elem_icon = f"/images/games/zone-nova/element/{real}"

        class_icon = None
        if cls and cls != "-":
            ck = cls.lower()
            real = class_icon_map.get(ck) or class_icon_map.get(slug_id(ck))
            if real:
                class_icon = f"/images/games/zone-nova/classes/{real}"

        out.append({
            "id": cid,
            "name": name or cid,
            "rarity": rarity,
            "element": element,

            "faction": faction,      # ✅ 추가(요청 2)

            "class": cls,            # 7개
            "role": role,            # 5개

            "image": image_url,
            "element_icon": elem_icon,
            "class_icon": class_icon,
        })

    # ✅ 정렬: 등급(SSR>SR>R) -> 이름순 (요청 6)
    def _sort_key(x: dict):
        r = (x.get("rarity") or "-").upper()
        return (-RARITY_ORDER.get(r, 0), (x.get("name") or "").lower())

    out.sort(key=_sort_key)
    return out


def normalize_bosses(raw) -> list[dict]:
    if isinstance(raw, dict) and isinstance(raw.get("bosses"), list):
        bosses = raw["bosses"]
    else:
        raise RuntimeError("bosses.json 포맷 오류: { bosses:[...] } 형태가 필요합니다.")

    out, seen = [], set()
    for b in bosses:
        if not isinstance(b, dict):
            continue
        bid = slug_id(b.get("id") or b.get("name") or "")
        if not bid or bid in seen:
            continue
        seen.add(bid)
        out.append({
            "id": bid,
            "name": (b.get("name") or bid).strip(),
            "weakness": b.get("weakness") or None,
            "enemy_element": b.get("enemy_element") or None,
        })
    return out


def load_all(force: bool = False) -> None:
    if CACHE["chars"] and CACHE["bosses"] and not force:
        return

    CACHE["error"] = None
    try:
        merged_list, src_label = load_and_merge_character_sources()
        CACHE["source"]["characters"] = f"public/data/zone-nova/{src_label}"
        CACHE["chars"] = normalize_chars(merged_list)

        edata = read_json_file(ELEM_JSON)
        adv = edata.get("adv") if isinstance(edata, dict) else None
        if not (isinstance(adv, dict) and adv):
            raise RuntimeError("element_chart.json 포맷 오류: { adv:{...} } 형태가 필요합니다.")
        CACHE["element_adv"] = {str(k): str(v) for k, v in adv.items()}

        bdata = read_json_file(BOSS_JSON)
        CACHE["bosses"] = normalize_bosses(bdata)

        CACHE["last_refresh"] = now_iso()

    except Exception as e:
        CACHE["chars"] = []
        CACHE["bosses"] = []
        CACHE["last_refresh"] = now_iso()
        CACHE["error"] = str(e)


def resolve_ids(input_list: list[str], chars: list[dict]) -> list[str]:
    if not input_list:
        return []
    by_id = {c["id"].lower(): c["id"] for c in chars if c.get("id")}
    by_name = {(c.get("name") or "").lower(): c["id"] for c in chars if c.get("id")}

    out = []
    for x in input_list:
        k = (x or "").strip().lower()
        if not k:
            continue
        out.append(by_id.get(k) or by_name.get(k) or slug_id(x))

    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def breakdown(c: dict, mode: str, enemy_element: str | None, boss_weakness: str | None, adv_map: dict) -> dict:
    rarity = c.get("rarity") or "-"
    element = c.get("element") or "-"
    role = (c.get("role") or "-").lower()

    rarity_pts = RARITY_SCORE.get(rarity, 0)
    boss_bonus = 25 if (boss_weakness and element == boss_weakness) else 0

    adv_bonus = 0
    dis_penalty = 0
    if enemy_element:
        advantagers = [k for k, v in adv_map.items() if v == enemy_element]
        if element in advantagers:
            adv_bonus = 20
        if adv_map.get(enemy_element) == element:
            dis_penalty = -10

    role_bonus = 0
    if mode == "pvp" and role in ("tank", "healer"):
        role_bonus = 6
    if mode == "boss" and role in ("debuffer", "buffer"):
        role_bonus = 6

    total = rarity_pts + boss_bonus + adv_bonus + dis_penalty + role_bonus
    return {
        "rarity_pts": rarity_pts,
        "boss_bonus": boss_bonus,
        "adv_bonus": adv_bonus,
        "dis_penalty": dis_penalty,
        "role_bonus": role_bonus,
        "total": total,
    }


def recommend_party(payload: dict, chars: list[dict], adv_map: dict) -> dict:
    mode = payload.get("mode") or "pve"
    owned = resolve_ids(payload.get("owned") or [], chars)
    required = resolve_ids(payload.get("required") or [], chars)
    banned = set(resolve_ids(payload.get("banned") or [], chars))
    enemy_element = payload.get("enemy_element") or None
    boss_weakness = payload.get("boss_weakness") or None

    by_id = {c["id"]: c for c in chars}
    pool = [by_id[i] for i in owned if i in by_id and i not in banned]

    if len(pool) < 4:
        return {"ok": False, "issues": ["보유(Owned) 선택 인원이 4명 미만입니다."], "best_party": None}

    pool_ids = {c["id"] for c in pool}
    issues = []
    for r in required:
        if r not in pool_ids:
            issues.append(f"필수 포함 캐릭터({r})가 보유 목록에 없습니다.")

    def score(c: dict) -> int:
        return breakdown(c, mode, enemy_element, boss_weakness, adv_map)["total"]

    party = []
    for rid in required:
        if rid in pool_ids and rid not in party:
            party.append(rid)

    remain = [c for c in pool if c["id"] not in party]
    remain.sort(key=lambda c: score(c), reverse=True)
    while len(party) < 4 and remain:
        party.append(remain.pop(0)["id"])

    members = []
    for pid in party[:4]:
        c = by_id.get(pid)
        if not c:
            continue
        bd = breakdown(c, mode, enemy_element, boss_weakness, adv_map)
        members.append({
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "rarity": c.get("rarity") or "-",
            "element": c.get("element") or "-",
            "faction": c.get("faction") or "",
            "class": c.get("class") or "-",
            "role": c.get("role") or "-",
            "image": c.get("image"),
            "score": bd["total"],
            "breakdown": bd,
        })

    team_total = sum(m["score"] for m in members)

    return {
        "ok": True,
        "mode": mode,
        "input": {
            "owned": owned,
            "required": required,
            "banned": sorted(list(banned)),
            "enemy_element": enemy_element,
            "boss_weakness": boss_weakness,
        },
        "best_party": {
            "party_size": 4,
            "team_total": team_total,
            "members": members,
            "analysis": issues if issues else ["조건 충족(4인 구성)"],
        }
    }


@app.get("/")
def home():
    return redirect("/ui/select")


@app.get("/refresh")
def refresh():
    load_all(force=True)
    return redirect("/ui/select")


@app.get("/meta")
def meta():
    load_all()
    return jsonify({
        "title": APP_TITLE,
        "characters_cached": len(CACHE["chars"]),
        "bosses_cached": len(CACHE["bosses"]),
        "last_refresh": CACHE["last_refresh"],
        "error": CACHE["error"],
        "source": CACHE["source"],
        "image_dir": IMG_DIR,
        "elem_icon_dir": ELEM_ICON_DIR,
        "class_icon_dir": CLASS_ICON_DIR,
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


@app.get("/zones/zone-nova/bosses")
def api_bosses():
    load_all()
    return jsonify({
        "count": len(CACHE["bosses"]),
        "last_refresh": CACHE["last_refresh"],
        "source": CACHE["source"]["bosses"],
        "error": CACHE["error"],
        "bosses": CACHE["bosses"],
    })


@app.post("/recommend/v3")
def api_recommend():
    load_all()
    payload = request.get_json(force=True) or {}
    res = recommend_party(payload, CACHE["chars"], CACHE["element_adv"])
    return Response(json.dumps(res, ensure_ascii=False, indent=2),
                    mimetype="application/json; charset=utf-8")


@app.get("/ui/select")
def ui_select():
    load_all()
    return render_template(
        "select.html",
        title=APP_TITLE,
        cache_count=len(CACHE["chars"]),
        boss_count=len(CACHE["bosses"]),
        last_refresh=CACHE["last_refresh"] or "N/A",
        error=CACHE["error"],
        chars_json=json.dumps(CACHE["chars"], ensure_ascii=False),
        bosses_json=json.dumps(CACHE["bosses"], ensure_ascii=False),
        adv_json=json.dumps(CACHE["element_adv"], ensure_ascii=False),
    )


def _count_ge2(values):
    from collections import Counter
    c = Counter([v for v in values if v])
    return any(n >= 2 for n in c.values())


def compute_combo_bonus(party_chars, w_elem=20, w_faction=20):
    """
    party_chars: 캐릭 dict 리스트 (element, faction 포함)
    - 같은 속성 2명 이상 -> w_elem
    - 같은 파벌 2명 이상 -> w_faction
    - 둘 다 만족하면 과대가중 방지를 위해 max 적용(원하면 합산으로 변경 가능)
    """
    elems = [c.get("element") for c in party_chars]
    facts = [c.get("faction") for c in party_chars]

    elem_ok = _count_ge2(elems)
    fac_ok = _count_ge2(facts)

    elem_bonus = w_elem if elem_ok else 0
    fac_bonus = w_faction if fac_ok else 0
    bonus = max(elem_bonus, fac_bonus)

    reasons = []
    if elem_ok:
        reasons.append("콤보: 같은 속성 2인 이상")
    if fac_ok:
        reasons.append("콤보: 같은 파벌 2인 이상")
    return bonus, reasons


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
