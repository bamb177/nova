from __future__ import annotations

import os
import re
import json
import warnings
import itertools
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning
from flask import Flask, request, Response, send_from_directory, abort

from bs4 import BeautifulSoup  # requirements.txt: beautifulsoup4


# =========================
# Flask
# =========================
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
try:
    app.json.ensure_ascii = False
except Exception:
    pass

KST = timezone(timedelta(hours=9))
DEFAULT_PORT = 40000
PARTY_SIZE = 4
APP_TITLE = "Nova"

# =========================
# Remote
# =========================
ZONE_NOVA_DB_URL = "https://gachawiki.info/guides/zone-nova/characters/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
FORCE_LOCAL_ONLY = os.environ.get("FORCE_LOCAL_ONLY", "").strip() in {"1", "true", "TRUE", "yes", "YES"}

# =========================
# Paths (Render/Local)
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

IMAGE_DIR_CANDIDATES = [
    os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters"),
    os.path.join(BASE_DIR, "gacha-wiki", "public", "images", "games", "zone-nova", "characters"),
]

IMAGES_BASE_CANDIDATES = [
    os.path.join(BASE_DIR, "public", "images"),
    os.path.join(BASE_DIR, "gacha-wiki", "public", "images"),
]

# =========================
# Element advantage / weakness weights (customizable)
# =========================
ALL_ELEMENTS = ["Fire", "Ice", "Wind", "Holy", "Chaos"]

# attacker -> [defender it is strong against]
ELEMENT_ADVANTAGE = {
    "Fire": ["Wind"],
    "Wind": ["Ice"],
    "Ice": ["Holy"],
    "Holy": ["Chaos"],
    "Chaos": ["Fire"],
}

WEIGHT_MATCH_WEAKNESS = 8.0         # party member element == boss_weakness
WEIGHT_ADV_OVER_ENEMY = 5.0         # party member element strong vs enemy_element
WEIGHT_FOCUS_INCLUDED = 6.0         # focus character included bonus (per focus member)

# =========================
# In-memory cache
# =========================
CACHE: Dict[str, Any] = {
    "zone_nova": {
        "characters": [],
        "count": 0,
        "last_refresh_iso": None,
        "error": None,
        "source": None,              # images_only | images+remote
        "image_dir": None,
        "image_count": 0,
        "remote_ok": False,
        "remote_error": None,
        "remote_count": 0,
        "force_local_only": FORCE_LOCAL_ONLY,
        "remote_bs4_available": True,
    }
}


# =========================
# Utils
# =========================
def now_iso_kst() -> str:
    return datetime.now(tz=KST).isoformat(timespec="seconds")


def slugify(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("’", "").replace("'", "")
    s = re.sub(r"[^A-Za-z0-9\s\-_]", "", s)
    s = s.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def prettify_name_from_stem(stem: str) -> str:
    s = (stem or "").strip()
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = s.strip()
    if not s:
        return stem
    if s.islower():
        s = s.title()
    return s


def pick_existing_dir(candidates: List[str]) -> Optional[str]:
    for p in candidates:
        if os.path.isdir(p):
            return p
    return None


def http_get(url: str, timeout: int = 25) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout, verify=True, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except SSLError:
        warnings.simplefilter("ignore", InsecureRequestWarning)
        r = requests.get(url, headers=headers, timeout=timeout, verify=False, allow_redirects=True)
        r.raise_for_status()
        return r.text


# =========================
# Remote parse (bs4 table)
# =========================
def parse_remote_zone_nova_characters_bs4(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return []

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).lower()

    target = None
    headers: List[str] = []

    for t in tables:
        thead = t.find("thead")
        if thead and thead.find_all("th"):
            headers = [norm(th.get_text(" ", strip=True)) for th in thead.find_all("th")]
        else:
            first_tr = t.find("tr")
            if not first_tr:
                continue
            headers = [norm(x.get_text(" ", strip=True)) for x in first_tr.find_all(["th", "td"])]

        if not headers:
            continue

        if (
            any("name" in h for h in headers)
            and any("rarity" in h for h in headers)
            and any("element" in h for h in headers)
            and any("role" in h for h in headers)
        ):
            target = t
            break

    if not target or not headers:
        return []

    def find_col(keys: List[str]) -> int:
        for i, h in enumerate(headers):
            for k in keys:
                if k in h:
                    return i
        return -1

    idx_name = find_col(["name"])
    idx_rarity = find_col(["rarity"])
    idx_element = find_col(["element"])
    idx_role = find_col(["role"])
    idx_class = find_col(["class"])
    idx_faction = find_col(["faction"])
    idx_hp = find_col(["hp"])
    idx_atk = find_col(["attack", "atk"])
    idx_def = find_col(["defense", "def"])
    idx_crit = find_col(["crit"])

    tbody = target.find("tbody")
    rows = tbody.find_all("tr") if tbody else target.find_all("tr")[1:]

    def pick(tds, idx: int) -> str:
        if idx < 0 or idx >= len(tds):
            return ""
        return tds[idx].get_text(" ", strip=True)

    def to_int(s: str) -> Optional[int]:
        s = (s or "").replace(",", "").strip()
        if not s:
            return None
        m = re.search(r"\d+", s)
        return int(m.group(0)) if m else None

    def to_float(s: str) -> Optional[float]:
        s = (s or "").replace(",", "").replace("%", "").strip()
        if not s:
            return None
        m = re.search(r"\d+(\.\d+)?", s)
        return float(m.group(0)) if m else None

    out: List[Dict[str, Any]] = []
    for tr in rows:
        tds = tr.find_all("td")
        if not tds:
            continue

        name = pick(tds, idx_name)
        if not name:
            continue

        rarity = pick(tds, idx_rarity) or None
        element = pick(tds, idx_element) or None
        role = pick(tds, idx_role) or None
        clazz = pick(tds, idx_class) or None
        faction = pick(tds, idx_faction) or None

        hp = to_int(pick(tds, idx_hp))
        atk = to_int(pick(tds, idx_atk))
        df = to_int(pick(tds, idx_def))
        crit = to_float(pick(tds, idx_crit))

        out.append({
            "id": slugify(name),
            "name": name,
            "rarity": rarity,
            "element": element,
            "role": role,
            "class": clazz,
            "faction": faction,
            "stats": {"hp": hp, "atk": atk, "def": df, "crit": crit},
        })

    uniq = {c["id"]: c for c in out}
    return list(uniq.values())


# =========================
# Local images -> characters
# =========================
def load_from_images(image_dir: str) -> Tuple[List[Dict[str, Any]], int]:
    files = [fn for fn in os.listdir(image_dir) if fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    chars: List[Dict[str, Any]] = []

    for fn in files:
        stem = os.path.splitext(fn)[0]
        name = prettify_name_from_stem(stem)
        cid = slugify(name)
        rel_img = f"/images/games/zone-nova/characters/{fn}"

        chars.append({
            "id": cid,
            "name": name,
            "rarity": None,
            "element": None,
            "role": None,
            "class": None,
            "faction": None,
            "stats": {"hp": None, "atk": None, "def": None, "crit": None},
            "image": rel_img,
        })

    uniq = {c["id"]: c for c in chars}
    out = list(uniq.values())
    out.sort(key=lambda x: x["name"].lower())
    return out, len(files)


def merge_image_and_remote(image_chars: List[Dict[str, Any]], remote_chars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {c["id"]: c for c in image_chars}

    for rc in remote_chars:
        rid = rc["id"]
        if rid in by_id:
            ic = by_id[rid]
            for k in ["rarity", "element", "role", "class", "faction"]:
                ic[k] = rc.get(k)
            ic["stats"] = rc.get("stats") or ic.get("stats")
        else:
            rc2 = dict(rc)
            rc2["image"] = None
            by_id[rid] = rc2

    out = list(by_id.values())
    out.sort(key=lambda x: x["name"].lower())
    return out


# =========================
# Refresh cache
# =========================
def refresh_zone_nova_cache() -> Tuple[bool, str]:
    try:
        image_dir = pick_existing_dir(IMAGE_DIR_CANDIDATES)
        if not image_dir:
            raise FileNotFoundError(
                "Zone Nova 캐릭터 이미지 폴더를 찾지 못했습니다.\n" +
                "\n".join([f"- {p}" for p in IMAGE_DIR_CANDIDATES])
            )

        image_chars, img_count = load_from_images(image_dir)

        remote_ok = False
        remote_err: Optional[str] = None
        remote_chars: List[Dict[str, Any]] = []

        if not FORCE_LOCAL_ONLY:
            try:
                html = http_get(ZONE_NOVA_DB_URL, timeout=25)
                remote_chars = parse_remote_zone_nova_characters_bs4(html)
                if len(remote_chars) >= 10:
                    remote_ok = True
                else:
                    remote_ok = False
                    remote_err = f"Fetched OK but parsed {len(remote_chars)} rows."
            except Exception as e:
                remote_ok = False
                remote_err = str(e)

        if remote_ok:
            merged = merge_image_and_remote(image_chars, remote_chars)
            CACHE["zone_nova"]["characters"] = merged
            CACHE["zone_nova"]["count"] = len(merged)
            CACHE["zone_nova"]["source"] = "images+remote"
        else:
            CACHE["zone_nova"]["characters"] = image_chars
            CACHE["zone_nova"]["count"] = len(image_chars)
            CACHE["zone_nova"]["source"] = "images_only"

        CACHE["zone_nova"]["last_refresh_iso"] = now_iso_kst()
        CACHE["zone_nova"]["error"] = None
        CACHE["zone_nova"]["image_dir"] = image_dir
        CACHE["zone_nova"]["image_count"] = img_count
        CACHE["zone_nova"]["remote_ok"] = remote_ok
        CACHE["zone_nova"]["remote_error"] = remote_err
        CACHE["zone_nova"]["remote_count"] = len(remote_chars)
        CACHE["zone_nova"]["force_local_only"] = FORCE_LOCAL_ONLY

        return True, f"ok: {CACHE['zone_nova']['count']} (source={CACHE['zone_nova']['source']})"

    except Exception as e:
        CACHE["zone_nova"]["characters"] = []
        CACHE["zone_nova"]["count"] = 0
        CACHE["zone_nova"]["last_refresh_iso"] = None
        CACHE["zone_nova"]["error"] = str(e)
        CACHE["zone_nova"]["source"] = None
        CACHE["zone_nova"]["remote_ok"] = False
        CACHE["zone_nova"]["remote_error"] = None
        CACHE["zone_nova"]["remote_count"] = 0
        return False, f"error: {e}"


def ensure_cache_loaded() -> None:
    if CACHE["zone_nova"]["count"] == 0 and CACHE["zone_nova"]["last_refresh_iso"] is None:
        refresh_zone_nova_cache()


# =========================
# Static images serving
# =========================
@app.get("/images/<path:filename>")
def serve_images(filename: str):
    for base in IMAGES_BASE_CANDIDATES:
        full = os.path.join(base, filename)
        if os.path.isfile(full):
            return send_from_directory(base, filename)
    abort(404)


# =========================
# Scoring
# =========================
def rarity_bonus(r: Optional[str]) -> float:
    r = (r or "").upper().strip()
    return {"SSR": 20.0, "SR": 10.0, "R": 0.0}.get(r, 0.0)


def role_bonus(role: Optional[str], mode: str) -> float:
    r = (role or "").strip().lower()
    if mode == "boss":
        return {"dps": 12.0, "debuffer": 9.0, "buffer": 6.0, "tank": 3.0, "healer": 3.0}.get(r, 0.0)
    if mode == "pvp":
        return {"tank": 12.0, "healer": 12.0, "debuffer": 8.0, "buffer": 7.0, "dps": 5.0}.get(r, 0.0)
    # pve
    return {"tank": 10.0, "healer": 10.0, "dps": 9.0, "debuffer": 7.0, "buffer": 7.0}.get(r, 0.0)


def score_character(c: Dict[str, Any], mode: str) -> float:
    st = c.get("stats") or {}
    hp = st.get("hp") or 0
    atk = st.get("atk") or 0
    df = st.get("def") or 0
    crit = st.get("crit") or 0.0

    hp_s = float(hp) / 1000.0
    atk_s = float(atk) / 100.0
    df_s = float(df) / 100.0

    if mode == "boss":
        base = 0.75 * atk_s + 0.10 * df_s + 0.15 * hp_s
    elif mode == "pvp":
        base = 0.20 * atk_s + 0.45 * df_s + 0.35 * hp_s
    else:
        base = 0.55 * atk_s + 0.20 * df_s + 0.25 * hp_s

    try:
        base += float(crit) / 10.0
    except Exception:
        pass

    base += rarity_bonus(c.get("rarity"))
    base += role_bonus(c.get("role"), mode)
    return base


def element_advantage(attacker: Optional[str], defender: Optional[str]) -> bool:
    if not attacker or not defender:
        return False
    return defender in ELEMENT_ADVANTAGE.get(attacker, [])


def team_score(
    party: List[Dict[str, Any]],
    mode: str,
    boss_weakness: Optional[str],
    enemy_element: Optional[str],
    focus_ids: List[str],
) -> Tuple[float, List[str]]:
    score = sum(score_character(c, mode) for c in party)
    reasons: List[str] = []

    roles = [(c.get("role") or "").strip().lower() for c in party]
    if "tank" not in roles:
        score -= 25.0
        reasons.append("탱커 없음(패널티)")
    else:
        score += 8.0
        reasons.append("탱커 포함")

    if "healer" not in roles:
        score -= 25.0
        reasons.append("힐러 없음(패널티)")
    else:
        score += 8.0
        reasons.append("힐러 포함")

    # weakness bonus
    if boss_weakness:
        hit = sum(1 for c in party if (c.get("element") or "") == boss_weakness)
        score += hit * WEIGHT_MATCH_WEAKNESS
        reasons.append(f"약점속성({boss_weakness}) 매칭 {hit}/4")

    # advantage bonus
    if enemy_element:
        hit = sum(1 for c in party if element_advantage(c.get("element"), enemy_element))
        score += hit * WEIGHT_ADV_OVER_ENEMY
        reasons.append(f"상성우위(Enemy={enemy_element}) {hit}/4")

    # focus
    if focus_ids:
        hit = sum(1 for c in party if c["id"] in focus_ids)
        score += hit * WEIGHT_FOCUS_INCLUDED
        reasons.append(f"Focus 포함 {hit}/{len(focus_ids)}")

    # keep it short
    return score, reasons[:6]


def resolve_ids(all_chars: List[Dict[str, Any]], xs: Any) -> List[str]:
    if not isinstance(xs, list):
        return []
    by_id = {c["id"].lower(): c["id"] for c in all_chars}
    by_name = {c["name"].lower(): c["id"] for c in all_chars if c.get("name")}
    out: List[str] = []
    for x in xs:
        k = str(x).strip().lower()
        if not k:
            continue
        if k in by_id:
            out.append(by_id[k])
        elif k in by_name:
            out.append(by_name[k])
    return list(dict.fromkeys(out))


def top_parties_v3(
    owned_chars: List[Dict[str, Any]],
    mode: str,
    top_k: int,
    required: List[str],
    banned: List[str],
    focus: List[str],
    boss_weakness: Optional[str],
    enemy_element: Optional[str],
) -> Dict[str, Any]:
    # filter banned
    banned_set = set(banned)
    pool = [c for c in owned_chars if c["id"] not in banned_set]

    required_set = set(required)
    missing_required = [rid for rid in required if rid not in {c["id"] for c in pool}]
    issues: List[str] = []
    if missing_required:
        issues.append(f"필수 캐릭 미포함/미보유: {missing_required}")

    # Candidate pool limit (avoid explosion)
    pool.sort(key=lambda c: score_character(c, mode), reverse=True)
    candidate = pool[:22]

    # Ensure required in candidate
    by_id = {c["id"]: c for c in pool}
    for rid in required_set:
        if rid in by_id and all(c["id"] != rid for c in candidate):
            candidate.append(by_id[rid])

    # if too small
    if len(candidate) < PARTY_SIZE:
        return {"ok": False, "error": f"후보 부족({len(candidate)}명)", "issues": issues, "parties": []}

    # Enumerate combinations of 4 from candidate
    results: List[Tuple[float, Dict[str, Any]]] = []
    focus_ids = list(dict.fromkeys(focus))

    for comb in itertools.combinations(candidate, PARTY_SIZE):
        ids = {c["id"] for c in comb}
        # required must be included
        if required_set and not required_set.issubset(ids):
            continue
        score, reasons = team_score(list(comb), mode, boss_weakness, enemy_element, focus_ids)

        entry = {
            "score": round(score, 2),
            "reasons": reasons,
            "members": [{
                "id": c["id"],
                "name": c.get("name"),
                "rarity": c.get("rarity"),
                "element": c.get("element"),
                "role": c.get("role"),
                "class": c.get("class"),
                "image": c.get("image"),
                "score": round(score_character(c, mode), 2),
            } for c in sorted(comb, key=lambda x: score_character(x, mode), reverse=True)]
        }
        results.append((score, entry))

    results.sort(key=lambda x: x[0], reverse=True)
    top = [e for _, e in results[:max(1, min(10, top_k))]]

    return {"ok": True, "issues": issues, "parties": top}


# =========================
# Routes
# =========================
@app.get("/")
def home() -> Response:
    ensure_cache_loaded()
    zn = CACHE["zone_nova"]

    html = f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>{APP_TITLE}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    code {{ background: #f5f5f5; padding: 2px 6px; }}
    .box {{ border: 1px solid #ddd; padding: 16px; border-radius: 8px; max-width: 1100px; }}
    .row {{ margin: 8px 0; }}
    .err {{ color: #b00020; white-space: pre-wrap; }}
    a {{ text-decoration: none; }}
  </style>
</head>
<body>
  <h1>{APP_TITLE}</h1>
  <div class="box">
    <div class="row">Image dir: <code>{zn.get("image_dir") or "N/A"}</code></div>
    <div class="row">Image files: <code>{zn.get("image_count")}</code></div>
    <div class="row">Last refresh: <code>{zn.get("last_refresh_iso") or "N/A"}</code></div>
    <div class="row">Characters cached: <code>{zn.get("count")}</code></div>
    <div class="row">Source: <code>{zn.get("source") or "N/A"}</code></div>

    <div class="row">Remote scrape: <code>{zn.get("remote_ok")}</code> (remote_count=<code>{zn.get("remote_count")}</code>)</div>
    <div class="row">FORCE_LOCAL_ONLY: <code>{zn.get("force_local_only")}</code></div>

    <div class="row">Remote error:</div>
    <div class="err">{zn.get("remote_error") or "None"}</div>

    <div class="row">Cache error:</div>
    <div class="err">{zn.get("error") or "None"}</div>

    <div class="row" style="margin-top: 12px;">
      <a href="/refresh" style="margin-right:10px;">Refresh</a>
      <a href="/zones/zone-nova/characters" style="margin-right:10px;">/zones/zone-nova/characters</a>
      <a href="/ui/select" style="margin-right:10px;">/ui/select</a>
      <a href="/recommend/v3">/recommend/v3</a>
    </div>
  </div>
</body>
</html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.get("/refresh")
@app.post("/refresh")
def refresh() -> Response:
    ok, msg = refresh_zone_nova_cache()
    status = 200 if ok else 500
    return Response(
        json.dumps({"ok": ok, "message": msg, "zone_nova": CACHE["zone_nova"]}, ensure_ascii=False),
        mimetype="application/json; charset=utf-8",
        status=status,
    )


@app.get("/zones/zone-nova/characters")
def zone_nova_characters() -> Response:
    ensure_cache_loaded()
    payload = {
        "game": "zone-nova",
        "count": CACHE["zone_nova"]["count"],
        "last_refresh": CACHE["zone_nova"]["last_refresh_iso"],
        "source": CACHE["zone_nova"]["source"],
        "image_dir": CACHE["zone_nova"]["image_dir"],
        "image_count": CACHE["zone_nova"]["image_count"],
        "remote_ok": CACHE["zone_nova"]["remote_ok"],
        "remote_count": CACHE["zone_nova"]["remote_count"],
        "remote_error": CACHE["zone_nova"]["remote_error"],
        "error": CACHE["zone_nova"]["error"],
        "characters": CACHE["zone_nova"]["characters"],
    }
    return Response(json.dumps(payload, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.get("/recommend/v3")
def recommend_v3_help() -> Response:
    html = f"""
<!doctype html>
<html lang="ko">
<head><meta charset="utf-8" /><title>{APP_TITLE}</title></head>
<body style="font-family: Arial, sans-serif; margin: 24px;">
  <h2>{APP_TITLE} /recommend/v3</h2>
  <pre style="background:#f5f5f5;padding:12px;border-radius:8px;">
POST /recommend/v3
Content-Type: application/json

{{
  "mode": "pve",                     // pve | boss | pvp
  "top_k": 5,                        // 1~10
  "owned": ["nina","freya","..."],    // id 또는 name
  "required": ["nina"],              // 반드시 포함
  "focus": ["freya"],                // 포함 시 점수 가산
  "banned": ["apep"],                // 제외
  "boss_weakness": "Fire",           // (선택) Fire/Ice/Wind/Holy/Chaos
  "enemy_element": "Wind"            // (선택) 상성 계산용
}}
  </pre>
  <p><a href="/ui/select">UI로 이동</a></p>
</body></html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.post("/recommend/v3")
def recommend_v3() -> Response:
    ensure_cache_loaded()
    data = request.get_json(silent=True) or {}

    mode = (data.get("mode") or "pve").strip().lower()
    if mode not in {"pve", "boss", "pvp"}:
        mode = "pve"

    top_k = data.get("top_k", 5)
    try:
        top_k = int(top_k)
    except Exception:
        top_k = 5
    top_k = max(1, min(10, top_k))

    boss_weakness = (data.get("boss_weakness") or "").strip()
    if boss_weakness and boss_weakness not in ALL_ELEMENTS:
        boss_weakness = ""

    enemy_element = (data.get("enemy_element") or "").strip()
    if enemy_element and enemy_element not in ALL_ELEMENTS:
        enemy_element = ""

    chars = CACHE["zone_nova"]["characters"]

    owned_ids = resolve_ids(chars, data.get("owned") or [])
    required_ids = resolve_ids(chars, data.get("required") or [])
    focus_ids = resolve_ids(chars, data.get("focus") or [])
    banned_ids = resolve_ids(chars, data.get("banned") or [])

    by_id = {c["id"]: c for c in chars}
    owned_chars = [by_id[i] for i in owned_ids if i in by_id]

    if len(owned_chars) < PARTY_SIZE:
        return Response(
            json.dumps({"ok": False, "error": f"owned는 최소 {PARTY_SIZE}명 필요", "count_owned": len(owned_chars)}, ensure_ascii=False),
            mimetype="application/json; charset=utf-8",
            status=400,
        )

    out = top_parties_v3(
        owned_chars=owned_chars,
        mode=mode,
        top_k=top_k,
        required=required_ids,
        banned=banned_ids,
        focus=focus_ids,
        boss_weakness=boss_weakness or None,
        enemy_element=enemy_element or None,
    )

    out.update({
        "mode": mode,
        "top_k": top_k,
        "inputs": {
            "owned": owned_ids,
            "required": required_ids,
            "focus": focus_ids,
            "banned": banned_ids,
            "boss_weakness": boss_weakness or None,
            "enemy_element": enemy_element or None,
        },
        "data_source": CACHE["zone_nova"]["source"],
        "remote_ok": CACHE["zone_nova"]["remote_ok"],
        "remote_count": CACHE["zone_nova"]["remote_count"],
    })

    return Response(json.dumps(out, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.get("/ui/select")
def ui_select() -> Response:
    ensure_cache_loaded()
    chars = CACHE["zone_nova"]["characters"]

    # UI는 JS로 /recommend/v3 호출해서 결과를 "표"로 렌더
    # (서버 템플릿 엔진 최소화해서 500 리스크 낮춤)
    items_html = []
    for c in chars:
        img = c.get("image")
        img_tag = f'<img src="{img}" style="width:44px;height:44px;object-fit:cover;border-radius:10px;margin-right:8px;" />' if img else ""
        items_html.append(f"""
          <label style="display:flex;align-items:center;gap:10px;padding:6px 0;">
            <input type="checkbox" class="owned" value="{c['id']}" />
            {img_tag}
            <div>
              <div style="font-weight:700">{c.get('name')}</div>
              <div style="color:#777;font-size:12px;">
                {c.get('rarity') or '-'} / {c.get('element') or '-'} / {c.get('role') or '-'}
              </div>
            </div>
          </label>
        """)

    html = f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>{APP_TITLE}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    .box {{ border: 1px solid #ddd; padding: 16px; border-radius: 10px; max-width: 1200px; }}
    .row {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom: 10px; }}
    select, input {{ padding: 8px 10px; border-radius: 10px; border: 1px solid #ccc; }}
    button {{ padding: 8px 12px; border-radius: 10px; border: 1px solid #ccc; background: #f7f7f7; cursor:pointer; }}
    .list {{ max-height:520px; overflow:auto; border:1px solid #eee; padding:10px; border-radius:10px; }}
    table {{ width:100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 10px; vertical-align: top; }}
    th {{ text-align:left; background:#fafafa; position:sticky; top:0; }}
    .members {{ display:grid; grid-template-columns: repeat(4, minmax(140px,1fr)); gap:10px; }}
    .mcard {{ display:flex; gap:10px; border:1px solid #eee; border-radius:10px; padding:8px; }}
    .mcard img {{ width:40px; height:40px; border-radius:10px; object-fit:cover; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .small {{ font-size: 12px; color:#666; }}
  </style>
</head>
<body>
  <h2>{APP_TITLE} - Party Builder</h2>

  <div class="box">
    <div class="row">
      <label>Mode</label>
      <select id="mode">
        <option value="pve">pve</option>
        <option value="boss">boss</option>
        <option value="pvp">pvp</option>
      </select>

      <label>Top</label>
      <select id="top_k">
        <option value="3">3</option>
        <option value="5" selected>5</option>
        <option value="10">10</option>
      </select>

      <label>Boss Weakness</label>
      <select id="boss_weakness">
        <option value="">(none)</option>
        {''.join([f'<option value="{e}">{e}</option>' for e in ALL_ELEMENTS])}
      </select>

      <label>Enemy Element</label>
      <select id="enemy_element">
        <option value="">(none)</option>
        {''.join([f'<option value="{e}">{e}</option>' for e in ALL_ELEMENTS])}
      </select>
    </div>

    <div class="row">
      <button onclick="pushTo('required')">선택 → Required</button>
      <button onclick="pushTo('focus')">선택 → Focus</button>
      <button onclick="pushTo('banned')">선택 → Banned</button>
      <a href="/" style="margin-left:auto;">메타</a>
    </div>

    <div class="row">
      <label>Required</label><input id="required" style="width:320px" placeholder="nina, freya" />
      <label>Focus</label><input id="focus" style="width:320px" placeholder="lavinia" />
      <label>Banned</label><input id="banned" style="width:320px" placeholder="apep" />
      <button onclick="run()">Recommend</button>
    </div>

    <div class="small">
      상성표(기본): {', '.join([f"{k}→{v[0]}" for k,v in ELEMENT_ADVANTAGE.items() if v])}
    </div>

    <h3 style="margin-top:16px;">Owned</h3>
    <div class="list">
      {''.join(items_html)}
    </div>

    <h3 style="margin-top:16px;">Result</h3>
    <div id="out" class="small">(아직 없음)</div>
  </div>

<script>
function checkedOwned() {{
  return Array.from(document.querySelectorAll('.owned:checked')).map(x => x.value);
}}
function csv(v) {{
  v = (v||"").trim();
  if(!v) return [];
  return v.split(',').map(x => x.trim()).filter(Boolean);
}}
function uniq(arr) {{
  const s = new Set();
  arr.forEach(x => s.add(x));
  return Array.from(s);
}}
function pushTo(target) {{
  const owned = checkedOwned();
  if(owned.length === 0) {{
    alert("먼저 Owned 체크하세요.");
    return;
  }}
  const el = document.getElementById(target);
  const cur = csv(el.value);
  el.value = uniq(cur.concat(owned)).join(", ");
}}

function renderTable(data) {{
  if(!data.ok) {{
    document.getElementById("out").innerHTML = "<div class='mono'>"+JSON.stringify(data,null,2)+"</div>";
    return;
  }}
  const parties = data.parties || [];
  if(parties.length === 0) {{
    document.getElementById("out").innerText = "조건을 만족하는 파티가 없습니다.";
    return;
  }}

  let html = "";
  html += "<div class='small mono'>inputs: "+JSON.stringify(data.inputs)+"</div>";
  if((data.issues||[]).length) {{
    html += "<div class='small'>issues: "+(data.issues||[]).join(" / ")+"</div>";
  }}
  html += "<div style='margin-top:10px;max-height:640px;overflow:auto;border:1px solid #eee;border-radius:10px;'>";
  html += "<table><thead><tr><th style='width:70px'>Rank</th><th style='width:90px'>Score</th><th>Party</th><th style='width:320px'>Reasons</th></tr></thead><tbody>";

  parties.forEach((p, idx) => {{
    html += "<tr>";
    html += "<td><b>#"+(idx+1)+"</b></td>";
    html += "<td><b>"+p.score+"</b></td>";
    html += "<td><div class='members'>";
    (p.members||[]).forEach(m => {{
      html += "<div class='mcard'>";
      if(m.image) html += "<img src='"+m.image+"' onerror=\\"this.style.display='none'\\" />";
      html += "<div><div style='font-weight:700;font-size:12px;'>"+m.name+"</div>";
      html += "<div class='small'>"+(m.role||'-')+" | "+(m.element||'-')+" | score "+m.score+"</div></div>";
      html += "</div>";
    }});
    html += "</div></td>";
    html += "<td><ul style='margin:0;padding-left:18px;'>";
    (p.reasons||[]).forEach(r => html += "<li>"+r+"</li>");
    html += "</ul></td>";
    html += "</tr>";
  }});

  html += "</tbody></table></div>";
  document.getElementById("out").innerHTML = html;
}}

async function run() {{
  const payload = {{
    mode: document.getElementById("mode").value,
    top_k: parseInt(document.getElementById("top_k").value, 10),
    owned: checkedOwned(),
    required: csv(document.getElementById("required").value),
    focus: csv(document.getElementById("focus").value),
    banned: csv(document.getElementById("banned").value),
    boss_weakness: document.getElementById("boss_weakness").value || null,
    enemy_element: document.getElementById("enemy_element").value || null
  }};
  document.getElementById("out").innerText = "계산 중...";
  const res = await fetch("/recommend/v3", {{
    method: "POST",
    headers: {{ "Content-Type":"application/json" }},
    body: JSON.stringify(payload)
  }});
  const json = await res.json();
  renderTable(json);
}}
</script>

</body>
</html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    refresh_zone_nova_cache()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    app.run(host="0.0.0.0", port=port, debug=True)
