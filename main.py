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

# 우선순위: meta -> json
CHAR_META_PATH = os.path.join(DATA_DIR, "characters_meta.json")
CHAR_JSON_PATH = os.path.join(DATA_DIR, "characters.json")

ELEM_JSON = os.path.join(DATA_DIR, "element_chart.json")
BOSS_JSON = os.path.join(DATA_DIR, "bosses.json")

app = Flask(__name__, static_folder="public", static_url_path="")

RARITY_SCORE = {"SSR": 30, "SR": 18, "R": 10, "-": 0}
RARITY_ORDER = {"SSR": 0, "SR": 1, "R": 2, "-": 9}
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

CACHE = {
    "chars": [],
    "bosses": [],
    "element_adv": {"Fire": "Wind", "Wind": "Ice", "Ice": "Holy", "Holy": "Chaos", "Chaos": "Fire"},
    "last_refresh": None,
    "source": {
        "characters_primary": None,
        "characters_fallback": None,
        "element_chart": "public/data/zone-nova/element_chart.json",
        "bosses": "public/data/zone-nova/bosses.json",
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
    name = " ".join(name.split())
    return name


def normalize_faction(v: str) -> str:
    v = (v or "").strip()
    v = " ".join(v.split())
    return v


def normalize_class(v: str) -> str:
    """
    입력이 class일 수도 있고 role일 수도 있음.
    class(7)로 정규화. 못 맞추면 '-'.
    """
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
        "debeffer": "debuffer",   # 오탈자 보정
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
    """
    class -> role(5) 변환. Apep 예외 반영.
    """
    if not cls or cls == "-":
        return "-"

    if cid == "apep" and cls == "warrior":
        return "tank"
    return CLASS_TO_ROLE.get(cls, "-")


def role_display(role_low: str) -> str:
    r = (role_low or "-").strip().lower()
    if r == "dps":
        return "DPS"
    if r in {"tank", "healer", "buffer", "debuffer"}:
        return r[:1].upper() + r[1:]
    return "-"


def class_display(cls_low: str) -> str:
    c = (cls_low or "-").strip().lower()
    if c in {"debuffer", "debeffer"}:
        return "Disruptor"
    if c in {"warrior", "mage", "rogue", "guardian", "healer", "buffer"}:
        return c[:1].upper() + c[1:]
    return "-"


def candidate_image_keys(cid: str, name: str) -> list[str]:
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


def _uniq_keep_order(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        x = (x or "").strip()
        if not x:
            continue
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def merge_raw_chars(primary_raw, fallback_raw) -> list[dict]:
    """
    primary(=meta)와 fallback(=characters.json)을 합쳐서
    - primary에 없는 캐릭터는 추가
    - primary에 있는 캐릭터라도 값이 '-' / '' / None이면 fallback으로 보정
    """
    p_list = extract_char_list(primary_raw) if primary_raw is not None else []
    f_list = extract_char_list(fallback_raw) if fallback_raw is not None else []

    def key_of(c: dict) -> str:
        cid = (c.get("id") or c.get("_id") or "").strip()
        nm = normalize_char_name(c.get("display_name") or c.get("name") or "")
        return slug_id(cid) or slug_id(nm)

    def is_empty(v) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return v.strip() == "" or v.strip() == "-"
        return False

    merged = {}
    # 먼저 primary
    for c in p_list:
        if not isinstance(c, dict):
            continue
        k = key_of(c)
        if not k:
            continue
        merged[k] = dict(c)

    # fallback 반영
    for c in f_list:
        if not isinstance(c, dict):
            continue
        k = key_of(c)
        if not k:
            continue

        if k not in merged:
            merged[k] = dict(c)
            continue

        # 보정: primary 값이 비어있으면 fallback으로 채움
        tgt = merged[k]
        for field in [
            "name", "display_name", "name_raw", "aliases",
            "rarity", "element", "class", "role",
            "faction", "faction_display", "faction_raw"
        ]:
            if field in c and (field not in tgt or is_empty(tgt.get(field))):
                tgt[field] = c.get(field)

    return list(merged.values())


def normalize_chars(raw_list) -> list[dict]:
    """
    최종 UI/추천에 쓰일 캐릭터 리스트로 정규화
    """
    chars = extract_char_list(raw_list)

    char_img_map = build_file_map(IMG_DIR)
    elem_icon_map = build_file_map(ELEM_ICON_DIR)
    class_icon_map = build_file_map(CLASS_ICON_DIR)

    out = []
    seen = set()

    for c in chars:
        if not isinstance(c, dict):
            continue

        # ID
        cid = (c.get("id") or c.get("_id") or "").strip()
        if not cid:
            fallback_name = normalize_char_name(c.get("display_name") or c.get("name") or "")
            cid = slug_id(fallback_name)
        cid = slug_id(cid)
        if not cid or cid in seen:
            continue

        # 이름(표시명 우선)
        name = normalize_char_name(c.get("display_name") or c.get("name") or "")
        name_raw = normalize_char_name(c.get("name_raw") or c.get("name") or "")

        aliases = []
        if isinstance(c.get("aliases"), list):
            aliases.extend([normalize_char_name(str(x)) for x in c.get("aliases") if x is not None])
        aliases.extend([name, name_raw, cid])
        aliases = _uniq_keep_order(aliases)

        # Jeanne D Arc 보강
        if slug_id(name) in {"jeannedarc", "joanofarc"} or cid in {"jeannedarc", "joanofarc"} or "jeanne" in cid:
            cid = "jeannedarc"
            name = "Jeanne D Arc"
            name_raw = name_raw or "Jeanne D Arc"
            aliases = _uniq_keep_order(aliases + ["Jeanne D Arc", "JeanneDArc", "Joan of Arc", "JoanofArc"])

        # id가 바뀌었을 수 있어 재검사
        if cid in seen:
            continue
        seen.add(cid)

        rarity = (c.get("rarity") or "-").strip().upper()
        if rarity not in RARITY_ORDER:
            rarity = "-"

        element = (c.get("element") or "-").strip()

        # 파벌(표시용 우선)
        faction = normalize_faction(
            c.get("faction_display")
            or c.get("faction")
            or c.get("Faction")
            or ""
        ) or None

        # 클래스/역할
        cls_raw = (
            c.get("class") or c.get("Class")
            or c.get("job") or c.get("Job")
            or c.get("type") or c.get("Type")
            or c.get("role") or c.get("Role")
        )
        cls = normalize_class(str(cls_raw) if cls_raw is not None else "")
        role = role_from_class(cls, cid)

        # ===== 이미지 매칭 =====
        image_url = None

        # 기존 special (호환)
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
            alias_candidates = _uniq_keep_order([name, name_raw] + aliases)
            for a in alias_candidates:
                for k in candidate_image_keys(cid, a):
                    real = char_img_map.get(k)
                    if real:
                        image_url = f"/images/games/zone-nova/characters/{real}"
                        break
                if image_url:
                    break

        # Jeanne 최종 보강
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

        # 아이콘
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
            "name_raw": name_raw or None,
            "aliases": aliases,

            "rarity": rarity,
            "element": element,
            "faction": faction,

            "class": cls,                     # canonical (추천/로직용)
            "role": role,                     # canonical (추천/로직용)
            "class_display": class_display(cls),
            "role_display": role_display(role),

            "image": image_url,
            "element_icon": elem_icon,
            "class_icon": class_icon,
        })

    # (6) 정렬: 등급 -> 이름
    out.sort(key=lambda x: (RARITY_ORDER.get(x.get("rarity") or "-", 9), (x.get("name") or "").lower()))
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
        primary_path = CHAR_META_PATH if os.path.isfile(CHAR_META_PATH) else (CHAR_JSON_PATH if os.path.isfile(CHAR_JSON_PATH) else None)
        if not primary_path:
            raise RuntimeError("캐릭터 JSON을 찾지 못했습니다: characters_meta.json 또는 characters.json")

        fallback_path = None
        if primary_path == CHAR_META_PATH and os.path.isfile(CHAR_JSON_PATH):
            fallback_path = CHAR_JSON_PATH
        elif primary_path == CHAR_JSON_PATH and os.path.isfile(CHAR_META_PATH):
            fallback_path = CHAR_META_PATH

        CACHE["source"]["characters_primary"] = f"public/data/zone-nova/{os.path.basename(primary_path)}"
        CACHE["source"]["characters_fallback"] = f"public/data/zone-nova/{os.path.basename(fallback_path)}" if fallback_path else None

        raw_primary = read_json_file(primary_path)
        raw_fallback = read_json_file(fallback_path) if fallback_path else None

        # (1) 병합 후 normalize
        merged_list = merge_raw_chars(raw_primary, raw_fallback) if raw_fallback is not None else extract_char_list(raw_primary)
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
    """
    입력 문자열(owned/required/banned)을 캐릭터 id로 최대한 안정적으로 해석.
    - id 직접 매칭
    - name 매칭
    - aliases(동의어/원본명/표시명) 매칭
    - slug fallback
    """
    if not input_list:
        return []

    by_id = {}
    by_key = {}

    for c in chars:
        cid = (c.get("id") or "").strip()
        if not cid:
            continue
        cid_low = cid.lower()
        by_id[cid_low] = cid

        nm = normalize_char_name(c.get("name") or "")
        if nm:
            by_key[nm.lower()] = cid
            by_key[slug_id(nm)] = cid

        nraw = normalize_char_name(c.get("name_raw") or "")
        if nraw:
            by_key[nraw.lower()] = cid
            by_key[slug_id(nraw)] = cid

        als = c.get("aliases")
        if isinstance(als, list):
            for a in als:
                a = normalize_char_name(str(a))
                if not a:
                    continue
                by_key[a.lower()] = cid
                by_key[slug_id(a)] = cid

    out = []
    for x in input_list:
        raw = (x or "").strip()
        if not raw:
            continue

        k = normalize_char_name(raw).lower()
        sid = slug_id(raw)

        out.append(
            by_id.get(k)
            or by_key.get(k)
            or by_key.get(sid)
            or sid
        )

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

    by_id = {c["id"]: c for c in chars if c.get("id")}
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
            "faction": c.get("faction") or None,
            "class": c.get("class") or "-",
            "role": c.get("role") or "-",
            "class_display": c.get("class_display") or "-",
            "role_display": c.get("role_display") or "-",
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
        "source": CACHE["source"],
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
    - 둘 다 만족하면 과대가중 방지를 위해 max 적용
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
