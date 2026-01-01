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

# 오버라이드
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

# 클래스 -> 역할 매핑(내부 로직용: 기존 유지)
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

# ✅ 속성명 변경(요청 2)
ELEMENT_RENAME = {
    "ice": "Frost",
    "wind": "Storm",
    "fire": "Blaze",
    "holy": "Holy",
    "chaos": "Chaos",
}

CACHE = {
    "chars": [],
    "bosses": [],
    "element_adv": {"Blaze": "Storm", "Storm": "Frost", "Frost": "Holy", "Holy": "Chaos", "Chaos": "Blaze"},
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
    """
    ✅ 중복 이슈 방지:
    - 다양한 dash(– — − -)를 '-'로 정규화
    """
    s = (s or "").strip().lower().replace("’", "'")
    # 다양한 dash 정규화
    s = s.replace("–", "-").replace("—", "-").replace("−", "-").replace("-", "-")
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

        stripped = re.sub(r"^[0-9]+[_\-\s]*", "", base_low).strip()
        if stripped:
            keys.add(stripped)
            keys.add(slug_id(stripped))

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
    name = name.replace("–", "-").replace("—", "-").replace("−", "-").replace("-", "-")
    name = " ".join(name.split())
    return name


def _norm_key(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("–", "-").replace("—", "-").replace("−", "-").replace("-", "-")
    return re.sub(r"[\s\-_]+", "", s)


def normalize_element_name(v: str) -> str:
    """
    Ice->Frost, Wind->Storm, Fire->Blaze
    """
    s = (v or "").strip()
    if not s or s == "-":
        return "-"
    low = s.lower()
    return ELEMENT_RENAME.get(low, s)


def element_icon_candidates(element: str) -> list[str]:
    """
    표기는 새 속성, 아이콘 파일명은 기존일 가능성까지 고려한 후보키
    """
    e = (element or "").strip()
    if not e or e == "-":
        return []
    low = e.lower()
    if low == "frost":
        return ["frost", "ice"]
    if low == "storm":
        return ["storm", "wind"]
    if low == "blaze":
        return ["blaze", "fire"]
    return [low]


def load_overrides() -> tuple[dict, dict]:
    names = dict(NAME_OVERRIDE_FALLBACK)
    factions = dict(FACTION_NAME_MAP_FALLBACK)

    f_names = safe_read_json(OVERRIDES_NAMES_JSON, {})
    if isinstance(f_names, dict) and f_names:
        names.update({str(k): str(v) for k, v in f_names.items()})

    f_factions = safe_read_json(OVERRIDES_FACTIONS_JSON, {})
    if isinstance(f_factions, dict) and f_factions:
        factions.update({str(k): str(v) for k, v in f_factions.items()})

    return names, factions


def apply_name_override(cid: str, name: str, names_map: dict) -> str:
    cid = (cid or "").strip()
    name = (name or "").strip()

    if cid:
        for k in (cid, cid.lower(), _norm_key(cid), slug_id(cid)):
            if k in names_map:
                return str(names_map[k]).strip()

    candidates = [name, name.lower(), _norm_key(name), slug_id(name)]
    for k in candidates:
        if k in names_map:
            return str(names_map[k]).strip()

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

    fk = _norm_key(f)
    for kk, vv in factions_map.items():
        if _norm_key(kk) == fk:
            return str(vv).strip()

    return f


def normalize_class(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return "-"

    low = s.lower()

    alias = {
        "guard": "guardian",
        "guardian": "guardian",
        "tank": "guardian",
        "dps": "warrior",
        "mage": "mage",
        "rogue": "rogue",
        "warrior": "warrior",
        "healer": "healer",
        "buffer": "buffer",
        "debuffer": "debuffer",
        "support": "buffer",
        "attacker": "warrior",
        "disruptor": "debuffer",
    }

    if low in CLASS_SET:
        return low
    if low in alias:
        return alias[low]
    return "-"


def role_from_class(cls: str, cid: str) -> str:
    if not cls or cls == "-":
        return "-"

    if cid == "apep" and cls == "warrior":
        return "tank"

    return CLASS_TO_ROLE.get(cls, "-")


def candidate_image_keys(cid: str, name: str) -> list[str]:
    out = []
    cid = (cid or "").strip()
    nm = (name or "").strip()

    def add(x: str):
        x = (x or "").strip()
        if not x:
            return
        # dash 정규화
        x = x.replace("–", "-").replace("—", "-").replace("−", "-").replace("-", "-")
        out.append(x.lower())
        out.append(slug_id(x))
        out.append(re.sub(r"[\s\-_]+", "", x.lower()))
        out.append(slug_id(re.sub(r"[\s\-_]+", "", x)))

    add(nm)
    add(nm.replace("'", ""))
    add(nm.replace("’", ""))
    add(nm.replace(" ", ""))
    add(cid)

    if cid == "jeannedarc" or "jeanne" in cid:
        add("Jeanne D Arc")
        add("JeanneDArc")
        add("Joan of Arc")
        add("JoanofArc")

    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def extract_char_list(raw) -> list[dict]:
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
                for k, v in c.items():
                    if k not in merged[cid] or merged[cid].get(k) in (None, "", "-", []):
                        merged[cid][k] = v

    return list(merged.values()), " + ".join(src_names)


def _quality_score(x: dict) -> int:
    """
    ✅ 동일 이름 중복 병합시, 정보가 더 완전한 레코드를 우선
    """
    s = 0
    if (x.get("image") or ""):
        s += 4
    if (x.get("faction") or "") and x.get("faction") != "-":
        s += 3
    if (x.get("element") or "") and x.get("element") != "-":
        s += 2
    if (x.get("class") or "") and x.get("class") != "-":
        s += 2
    if (x.get("rarity") or "") and x.get("rarity") != "-":
        s += 1
    if (x.get("element_icon") or ""):
        s += 1
    if (x.get("class_icon") or ""):
        s += 1
    return s


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
        if not cid:
            continue

        # ✅ 1차 중복 제거: id 기준
        if cid in seen:
            continue
        seen.add(cid)

        name = apply_name_override(cid, name, names_override)

        rarity = (c.get("rarity") or "-").strip().upper()

        # ✅ 속성명 변경(요청 2)
        element_raw = (c.get("element") or "-").strip()
        element = normalize_element_name(element_raw)

        faction_raw = c.get("faction") or c.get("Faction") or c.get("group") or c.get("Group") or ""
        faction = apply_faction_override(str(faction_raw), factions_override)

        cls_raw = c.get("class") or c.get("Class") or c.get("job") or c.get("Job") or c.get("type") or c.get("Type") or c.get("role") or c.get("Role")
        cls = normalize_class(str(cls_raw) if cls_raw is not None else "")

        if slug_id(name) in {"jeannedarc", "joanofarc"} or cid in {"jeannedarc", "joanofarc"} or "jeanne" in cid:
            cid = "jeannedarc"
            if not name:
                name = "Jeanne D Arc"

        role = role_from_class(cls, cid)

        image_url = None

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

        if not image_url:
            for k in candidate_image_keys(cid, name):
                real = char_img_map.get(k)
                if real:
                    image_url = f"/images/games/zone-nova/characters/{real}"
                    break

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

        elem_icon = None
        if element and element != "-":
            # ✅ 새 속성명/구 속성명 모두 시도
            picked = None
            for ek in element_icon_candidates(element):
                real = elem_icon_map.get(ek) or elem_icon_map.get(slug_id(ek))
                if real:
                    picked = real
                    break
            if picked:
                elem_icon = f"/images/games/zone-nova/element/{picked}"

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
            "faction": faction,
            "class": cls,
            "role": role,
            "image": image_url,
            "element_icon": elem_icon,
            "class_icon": class_icon,
        })

    # ✅ 2차 중복 제거: "같은 이름"이 여러 번 나오는 케이스 병합
    by_name = {}
    for x in out:
        key = _norm_key(x.get("name") or "") or x.get("id")
        if key not in by_name:
            by_name[key] = x
            continue

        cur = by_name[key]
        # 더 "완전한" 레코드를 채택하되, 서로의 빈 값은 병합
        if _quality_score(x) > _quality_score(cur):
            keep, other = x, cur
        else:
            keep, other = cur, x

        for k in ("rarity", "element", "faction", "class", "role", "image", "element_icon", "class_icon"):
            if (keep.get(k) in (None, "", "-", [])) and (other.get(k) not in (None, "", "-", [])):
                keep[k] = other.get(k)

        # id는 keep 기준 유지
        by_name[key] = keep

    out = list(by_name.values())

    # ✅ 정렬: 등급(SSR>SR>R) -> 이름순
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

        weakness = normalize_element_name(b.get("weakness") or None) if b.get("weakness") else None
        enemy_element = normalize_element_name(b.get("enemy_element") or None) if b.get("enemy_element") else None

        out.append({
            "id": bid,
            "name": (b.get("name") or bid).strip(),
            "weakness": weakness,
            "enemy_element": enemy_element,
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

        # ✅ adv 맵도 새 속성명으로 변환
        new_adv = {}
        for k, v in adv.items():
            kk = normalize_element_name(str(k))
            vv = normalize_element_name(str(v))
            new_adv[str(kk)] = str(vv)
        CACHE["element_adv"] = new_adv

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

    # ✅ 입력 요소도 새 속성명으로 정규화
    enemy_element = normalize_element_name(enemy_element) if enemy_element else None
    boss_weakness = normalize_element_name(boss_weakness) if boss_weakness else None

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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
