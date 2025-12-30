import os
import json
import re
from datetime import datetime, timezone
from flask import Flask, request, Response, redirect, jsonify, render_template

APP_TITLE = os.getenv("APP_TITLE", "Nova")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "public", "data", "zone-nova")
IMG_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters")

CHAR_JSON_CANDIDATES = [
    os.path.join(DATA_DIR, "characters.json"),
    os.path.join(DATA_DIR, "characters_meta.json"),
]

ELEM_JSON = os.path.join(DATA_DIR, "element_chart.json")
BOSS_JSON = os.path.join(DATA_DIR, "bosses.json")

app = Flask(__name__, static_folder="public", static_url_path="")

RARITY_SCORE = {"SSR": 30, "SR": 18, "R": 10, "-": 0}
VALID_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

CACHE = {
    "chars": [],
    "bosses": [],
    "element_adv": {"Fire": "Wind", "Wind": "Ice", "Ice": "Holy", "Holy": "Chaos", "Chaos": "Fire"},
    "last_refresh": None,
    "source": {"characters": None, "element_chart": "public/data/zone-nova/element_chart.json", "bosses": "public/data/zone-nova/bosses.json"},
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


def pick_char_json_path() -> str:
    for p in CHAR_JSON_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise RuntimeError(f"캐릭터 JSON을 찾지 못했습니다: {', '.join(CHAR_JSON_CANDIDATES)}")


def build_image_map() -> dict:
    """
    IMG_DIR를 스캔해서 실제 파일명을 기준으로 매핑.
    - key: basename(lower) (확장자 제외)
    - value: 실제 파일명 (대소문자 포함)
    """
    m = {}
    if not os.path.isdir(IMG_DIR):
        return m

    pri = {".jpg": 3, ".jpeg": 3, ".png": 2, ".webp": 1}

    for fn in os.listdir(IMG_DIR):
        ext = os.path.splitext(fn)[1].lower()
        if ext not in VALID_IMG_EXT:
            continue
        base = os.path.splitext(fn)[0].lower()
        if base not in m:
            m[base] = fn
        else:
            cur_ext = os.path.splitext(m[base])[1].lower()
            if pri.get(ext, 0) > pri.get(cur_ext, 0):
                m[base] = fn
    return m


def normalize_char_name(name: str) -> str:
    name = (name or "").replace("’", "'").strip()
    name = " ".join(name.split())
    return name


def candidate_image_bases(char_id: str, name: str) -> list[str]:
    out = []
    cid = (char_id or "").strip()
    nm = (name or "").strip()

    for v in [nm, nm.replace(" ", ""), nm.replace("'", ""), nm.replace("’", ""), cid]:
        v = v.strip()
        if not v:
            continue
        out.append(v.lower())

    # Jeanne D Arc / Joanof Arc 통합
    if slug_id(nm) in {"jeannedarc", "joanofarc"} or slug_id(cid) in {"jeannedarc", "joanofarc"}:
        out = ["jeanne d arc", "jeannedarc", "joanofarc", "joanof arc"]

    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def extract_char_list(raw) -> list[dict]:
    """
    characters_meta.json 포맷이 여러 형태일 수 있어 유연하게 list로 변환.
    지원:
    1) [ {...}, {...} ]
    2) { "characters": [ ... ] } / { "data": [ ... ] } 등
    3) { "Nina": {...}, "Freya": {...} }  (맵 형태)
    4) { "characters_meta": { "Nina": {...} } } (중첩 맵)
    """
    if isinstance(raw, list):
        return raw

    if not isinstance(raw, dict):
        raise RuntimeError("캐릭터 JSON 포맷 오류: 최상위가 list/dict가 아닙니다.")

    # 2) dict 안에 list가 들어있는 케이스 우선
    for k in ["characters", "characters_meta", "data", "items"]:
        v = raw.get(k)
        if isinstance(v, list):
            return v

    # 4) dict 안에 dict(맵)가 중첩되어 있는 케이스
    for k in ["characters", "characters_meta", "data", "items"]:
        v = raw.get(k)
        if isinstance(v, dict):
            # 중첩 dict가 맵 형태면 아래 3)로 처리되도록 넘김
            raw = v
            break

    # 3) 맵 형태: { "Nina": {...}, "Freya": {...} }
    # 값들이 dict인 엔트리가 어느 정도 있으면 맵으로 판단
    dict_values = list(raw.values())
    dict_like_cnt = sum(1 for x in dict_values if isinstance(x, dict))
    if dict_like_cnt >= 3:  # 너무 빡세게 잡지 않음
        out = []
        for key, val in raw.items():
            if not isinstance(val, dict):
                continue
            item = dict(val)
            # name/id 보정
            if not item.get("name"):
                item["name"] = key
            if not item.get("id"):
                item["id"] = slug_id(item.get("name") or key)
            out.append(item)
        return out

    raise RuntimeError("캐릭터 JSON 포맷 오류: list 또는 {characters:[...]} 또는 {Name:{...}}(맵) 형태여야 합니다.")


def normalize_chars(raw) -> list[dict]:
    chars = extract_char_list(raw)
    img_map = build_image_map()

    out = []
    seen = set()

    for c in chars:
        if not isinstance(c, dict):
            continue

        name = normalize_char_name(c.get("name") or "")
        cid = (c.get("id") or "").strip()
        if not cid:
            cid = slug_id(name)

        # Jeanne D Arc 정규화
        if slug_id(name) in {"jeannedarc", "joanofarc"} or slug_id(cid) in {"jeannedarc", "joanofarc"}:
            cid = "jeannedarc"
            name = "Jeanne D Arc"

        cid = slug_id(cid)
        if not cid or cid in seen:
            continue
        seen.add(cid)

        rarity = c.get("rarity") or "-"
        element = c.get("element") or "-"
        role = c.get("role") or "-"

        image_url = None
        img_file = c.get("img_file")

        if isinstance(img_file, str) and img_file:
            base = os.path.splitext(img_file)[0].lower()
            real = img_map.get(base)
            if real:
                image_url = f"/images/games/zone-nova/characters/{real}"

        if not image_url:
            for base in candidate_image_bases(cid, name):
                real = img_map.get(base)
                if real:
                    image_url = f"/images/games/zone-nova/characters/{real}"
                    break

        out.append({
            "id": cid,
            "name": name or cid,   # 캐릭터 이름 영어 유지
            "rarity": rarity,
            "element": element,
            "role": role,
            "image": image_url,
        })

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
        char_path = pick_char_json_path()
        CACHE["source"]["characters"] = f"public/data/zone-nova/{os.path.basename(char_path)}"
        raw_chars = read_json_file(char_path)
        CACHE["chars"] = normalize_chars(raw_chars)

        edata = read_json_file(ELEM_JSON)
        adv = edata.get("adv") if isinstance(edata, dict) else None
        if not (isinstance(adv, dict) and adv):
            raise RuntimeError("element_chart.json 포맷 오류: { adv:{...} } 형태가 필요합니다.")
        for k in ["Fire", "Wind", "Ice", "Holy", "Chaos"]:
            if k not in adv:
                raise RuntimeError(f"element_chart.json adv 누락: {k}")
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
        "image_dir_exists": os.path.isdir(IMG_DIR),
        "image_dir": IMG_DIR,
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
