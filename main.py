import os
import json
import re
from datetime import datetime, timezone
from flask import Flask, request, Response, redirect, jsonify, render_template

APP_TITLE = os.getenv("APP_TITLE", "Nova")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "public", "data", "zone-nova")
IMG_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters")
ELEM_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "element")
CLASS_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "classes")

# ✅ 캐릭터 데이터는 characters_ko만 사용
CHAR_KO_DIR = os.path.join(DATA_DIR, "characters_ko")

# (유지) rename/override, 보스/상성 데이터
OVERRIDE_NAMES = os.path.join(DATA_DIR, "overrides_names.json")
OVERRIDE_FACTIONS = os.path.join(DATA_DIR, "overrides_factions.json")
ELEM_JSON = os.path.join(DATA_DIR, "element_chart.json")
BOSS_JSON = os.path.join(DATA_DIR, "bosses.json")

app = Flask(__name__, static_folder="public", static_url_path="")

RARITY_SCORE = {"SSR": 30, "SR": 18, "R": 10, "-": 0}
VALID_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

CLASS_SET = {"buffer", "debuffer", "guardian", "healer", "mage", "rogue", "warrior"}
ROLE_SET = {"buffer", "dps", "debuffer", "healer", "tank"}

# ✅ debuffer(=Disruptor)는 역할에서 DPS 취급
CLASS_TO_ROLE = {
    "buffer": "buffer",
    "debuffer": "dps",
    "healer": "healer",
    "guardian": "tank",
    "mage": "dps",
    "rogue": "dps",
    "warrior": "dps",
}

# ✅ 속성명 변경 반영 (업스트림 Wind/Fire/Ice -> UI 표기 Storm/Blaze/Frost)
ELEMENT_RENAME = {"Fire": "Blaze", "Wind": "Storm", "Ice": "Frost"}

CACHE = {
    "chars": [],
    "details": {},  # cid -> raw detail json
    "bosses": [],
    "element_adv": {"Blaze": "Storm", "Storm": "Frost", "Frost": "Holy", "Holy": "Chaos", "Chaos": "Blaze"},
    "last_refresh": None,
    "source": {
        "characters": "public/data/zone-nova/characters_ko/*.json",
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

def safe_load_json(path: str):
    if not os.path.isfile(path):
        return None
    try:
        return read_json_file(path)
    except Exception:
        return None

def build_file_map(folder: str) -> dict:
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

        keys = {base_low, slug_id(base)}
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

def normalize_element(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return "-"
    s2 = s[:1].upper() + s[1:].lower()  # Wind, Fire, Ice, Holy, Chaos
    return ELEMENT_RENAME.get(s2, s2)

def normalize_class(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return "-"
    low = s.lower()

    alias = {
        "guard": "guardian",
        "tank": "guardian",
        "support": "buffer",
        "attacker": "warrior",
    }
    if low in CLASS_SET:
        return low
    if low in alias:
        return alias[low]
    return "-"

def normalize_role(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return "-"
    low = s.lower()
    # file에서 DPS/Tank/Healer 등으로 들어오는 케이스 대응
    if low in ROLE_SET:
        if low == "debuffer":
            return "dps"
        return low
    if low == "dps":
        return "dps"
    return "-"

def role_from_class(cls: str, cid: str) -> str:
    if not cls or cls == "-":
        return "-"
    # Apep: warrior여도 tank 가능(사용자 룰 유지)
    if cid == "apep" and cls == "warrior":
        return "tank"
    return CLASS_TO_ROLE.get(cls, "-")

def candidate_image_keys(cid: str, name: str, image_hint: str | None = None) -> list[str]:
    out = []
    cid = (cid or "").strip()
    nm = (name or "").strip()
    ih = (image_hint or "").strip()

    def add(x: str):
        x = (x or "").strip()
        if not x:
            return
        out.append(x.lower())
        out.append(slug_id(x))
        out.append(re.sub(r"[\s\-_]+", "", x.lower()))
        out.append(slug_id(re.sub(r"[\s\-_]+", "", x)))

    # ✅ characters_ko에 image:"Apep" 같은 힌트가 있으니 최우선으로 포함
    add(ih)
    add(nm)
    add(nm.replace("'", ""))
    add(nm.replace("’", ""))
    add(nm.replace(" ", ""))
    add(cid)

    if cid == "jeannedarc" or "jeanne" in cid:
        add("Jeanne D Arc")
        add("JeanneDArc")
        add("Joanof Arc")
        add("JoanofArc")

    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq

def _load_overrides():
    names = safe_load_json(OVERRIDE_NAMES)
    factions = safe_load_json(OVERRIDE_FACTIONS)
    return (names if isinstance(names, dict) else {}), (factions if isinstance(factions, dict) else {})

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

        weak = b.get("weakness") or None
        if isinstance(weak, str):
            weak = normalize_element(weak)

        enemy = b.get("enemy_element") or None
        if isinstance(enemy, str):
            enemy = normalize_element(enemy)

        out.append({
            "id": bid,
            "name": (b.get("name") or bid).strip(),
            "weakness": weak,
            "enemy_element": enemy,
        })
    return out

def load_characters_from_characters_ko() -> tuple[list[dict], dict]:
    if not os.path.isdir(CHAR_KO_DIR):
        raise RuntimeError(f"characters_ko 디렉터리 없음: {CHAR_KO_DIR}")

    overrides_names, overrides_factions = _load_overrides()
    char_img_map = build_file_map(IMG_DIR)
    elem_icon_map = build_file_map(ELEM_ICON_DIR)
    class_icon_map = build_file_map(CLASS_ICON_DIR)

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

        name = normalize_char_name(d.get("name") or cid)
        if name in overrides_names:
            name = overrides_names[name]

        rarity = (d.get("rarity") or "-").strip().upper()
        element = normalize_element(str(d.get("element") or "-"))
        faction = (d.get("faction") or "-")
        faction = (str(faction).strip() if faction is not None else "-") or "-"
        if faction in overrides_factions:
            faction = overrides_factions[faction]

        cls = normalize_class(str(d.get("class") or "-"))
        role_raw = normalize_role(str(d.get("role") or "-"))
        role = role_raw if role_raw != "-" else role_from_class(cls, cid)

        # image mapping
        image_url = None
        image_hint = d.get("image")
        if isinstance(image_hint, str):
            image_hint = image_hint.strip()

        for k in candidate_image_keys(cid, name, image_hint=image_hint):
            real = char_img_map.get(k)
            if real:
                image_url = f"/images/games/zone-nova/characters/{real}"
                break

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

        chars.append({
            "id": cid,
            "name": name,
            "rarity": rarity,
            "element": element,
            "faction": faction,
            "class": cls,
            "role": role,
            "image": image_url,
            "element_icon": elem_icon,
            "class_icon": class_icon,
        })

    rarity_order = {"SSR": 0, "SR": 1, "R": 2, "-": 9}
    chars.sort(key=lambda x: (rarity_order.get(x.get("rarity","-"), 9), (x.get("name") or "").lower()))
    return chars, details

def load_all(force: bool = False) -> None:
    if CACHE["chars"] and CACHE["bosses"] and not force:
        return

    CACHE["error"] = None
    try:
        # ✅ 캐릭터: characters_ko만
        chars, details = load_characters_from_characters_ko()
        CACHE["chars"] = chars
        CACHE["details"] = details

        # 상성
        edata = read_json_file(ELEM_JSON)
        adv = edata.get("adv") if isinstance(edata, dict) else None
        if not (isinstance(adv, dict) and adv):
            raise RuntimeError("element_chart.json 포맷 오류: { adv:{...} } 형태가 필요합니다.")

        adv2 = {}
        for k, v in adv.items():
            kk = normalize_element(str(k))
            vv = normalize_element(str(v))
            adv2[kk] = vv
        CACHE["element_adv"] = adv2

        # 보스
        bdata = read_json_file(BOSS_JSON)
        CACHE["bosses"] = normalize_bosses(bdata)

        CACHE["last_refresh"] = now_iso()

    except Exception as e:
        CACHE["chars"] = []
        CACHE["details"] = {}
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
    if mode == "boss" and role in ("buffer",):
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
            "faction": c.get("faction") or "-",
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
        "characters_ko_dir": CHAR_KO_DIR,
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

# ✅ 상세 조회도 characters_ko에서만
@app.get("/zones/zone-nova/characters/<cid>")
def api_char_detail(cid: str):
    load_all()
    cid2 = slug_id(cid)

    by_id = {c["id"]: c for c in CACHE["chars"]}
    base = by_id.get(cid2)

    detail = CACHE["details"].get(cid2)
    if not isinstance(detail, dict):
        # 캐시에 없으면 파일에서 직접 시도
        detail_path = os.path.join(CHAR_KO_DIR, f"{cid2}.json")
        detail = safe_load_json(detail_path)

    if not isinstance(detail, dict):
        return jsonify({"ok": False, "error": f"characters_ko json not found: {cid2}.json"}), 404

    # base가 없더라도 detail만으로 최소 base 생성
    if not base:
        base = {
            "id": cid2,
            "name": detail.get("name") or cid2,
            "rarity": (detail.get("rarity") or "-").strip().upper(),
            "element": normalize_element(str(detail.get("element") or "-")),
            "faction": (detail.get("faction") or "-") or "-",
            "class": normalize_class(str(detail.get("class") or "-")),
            "role": normalize_role(str(detail.get("role") or "-")),
            "image": None,
            "element_icon": None,
            "class_icon": None,
        }

    return jsonify({
        "ok": True,
        "id": cid2,
        "character": base,
        "detail": detail,
        "detail_source": f"public/data/zone-nova/characters_ko/{cid2}.json",
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
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
