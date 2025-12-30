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
# Element advantage / weakness weights
# =========================
ALL_ELEMENTS = ["Fire", "Ice", "Wind", "Holy", "Chaos"]

ELEMENT_ADVANTAGE = {
    "Fire": ["Wind"],
    "Wind": ["Ice"],
    "Ice": ["Holy"],
    "Holy": ["Chaos"],
    "Chaos": ["Fire"],
}

WEIGHT_MATCH_WEAKNESS = 8.0
WEIGHT_ADV_OVER_ENEMY = 5.0
WEIGHT_FOCUS_INCLUDED = 6.0

# =========================
# In-memory cache
# =========================
CACHE: Dict[str, Any] = {
    "zone_nova": {
        "characters": [],
        "count": 0,
        "last_refresh_iso": None,
        "error": None,
        "source": None,
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
    rr = (role or "").strip().lower()
    if mode == "boss":
        return {"dps": 12.0, "debuffer": 9.0, "buffer": 6.0, "tank": 3.0, "healer": 3.0}.get(rr, 0.0)
    if mode == "pvp":
        return {"tank": 12.0, "healer": 12.0, "debuffer": 8.0, "buffer": 7.0, "dps": 5.0}.get(rr, 0.0)
    return {"tank": 10.0, "healer": 10.0, "dps": 9.0, "debuffer": 7.0, "buffer": 7.0}.get(rr, 0.0)


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

    if boss_weakness:
        hit = sum(1 for c in party if (c.get("element") or "") == boss_weakness)
        score += hit * WEIGHT_MATCH_WEAKNESS
        reasons.append(f"약점속성({boss_weakness}) 매칭 {hit}/4")

    if enemy_element:
        hit = sum(1 for c in party if element_advantage(c.get("element"), enemy_element))
        score += hit * WEIGHT_ADV_OVER_ENEMY
        reasons.append(f"상성우위(Enemy={enemy_element}) {hit}/4")

    if focus_ids:
        hit = sum(1 for c in party if c["id"] in focus_ids)
        score += hit * WEIGHT_FOCUS_INCLUDED
        reasons.append(f"Focus 포함 {hit}/{len(focus_ids)}")

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
    banned_set = set(banned)
    pool = [c for c in owned_chars if c["id"] not in banned_set]

    required_set = set(required)
    missing_required = [rid for rid in required if rid not in {c["id"] for c in pool}]
    issues: List[str] = []
    if missing_required:
        issues.append(f"필수 캐릭 미포함/미보유: {missing_required}")

    pool.sort(key=lambda c: score_character(c, mode), reverse=True)
    candidate = pool[:22]

    by_id = {c["id"]: c for c in pool}
    for rid in required_set:
        if rid in by_id and all(c["id"] != rid for c in candidate):
            candidate.append(by_id[rid])

    if len(candidate) < PARTY_SIZE:
        return {"ok": False, "error": f"후보 부족({len(candidate)}명)", "issues": issues, "parties": []}

    results: List[Tuple[float, Dict[str, Any]]] = []
    focus_ids = list(dict.fromkeys(focus))

    for comb in itertools.combinations(candidate, PARTY_SIZE):
        ids = {c["id"] for c in comb}
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
  "mode": "pve",
  "top_k": 5,
  "owned": ["nina","freya","..."],
  "required": ["nina"],
  "focus": ["freya"],
  "banned": ["apep"],
  "boss_weakness": "Fire",
  "enemy_element": "Wind"
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


# =========================
# UI (JSON -> JS render 방식: 문법오류 방지)
# =========================
@app.get("/ui/select")
def ui_select() -> Response:
    ensure_cache_loaded()
    chars = CACHE["zone_nova"]["characters"]

    # JS에 주입할 JSON(스크립트 종료 태그 방지)
    chars_json = json.dumps(chars, ensure_ascii=False).replace("</", "<\\/")

    advantage_line = " · ".join([f"{k}→{','.join(v)}" for k, v in ELEMENT_ADVANTAGE.items()])

    html = f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{APP_TITLE}</title>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: rgba(255,255,255,.06);
      --panel2: rgba(255,255,255,.08);
      --border: rgba(255,255,255,.10);
      --muted: rgba(255,255,255,.65);
      --muted2: rgba(255,255,255,.50);
      --text: rgba(255,255,255,.92);
      --brand: #6ea8ff;
      --brand2: #7c5cff;
      --danger: #ff5d6c;
      --ok: #3ddc97;
      --shadow: 0 10px 30px rgba(0,0,0,.35);
      --r: 14px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, "Apple SD Gothic Neo", "Noto Sans KR", "Malgun Gothic", Arial;
      background: radial-gradient(1200px 600px at 10% 0%, rgba(110,168,255,.18), transparent 55%),
                  radial-gradient(800px 500px at 90% 10%, rgba(124,92,255,.16), transparent 60%),
                  var(--bg);
      color: var(--text);
    }}
    a {{ color: var(--brand); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    .topbar {{
      position: sticky; top: 0; z-index: 20;
      backdrop-filter: blur(10px);
      background: linear-gradient(to bottom, rgba(11,16,32,.88), rgba(11,16,32,.70));
      border-bottom: 1px solid var(--border);
    }}
    .topbarInner {{
      max-width: 1280px; margin: 0 auto;
      padding: 16px 18px;
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    }}
    .title {{ display:flex; align-items:baseline; gap:10px; margin-right:auto; }}
    .title h1 {{ font-size: 18px; margin: 0; letter-spacing: .2px; }}
    .meta {{ font-size: 12px; color: var(--muted); }}
    .pill {{
      display:inline-flex; align-items:center; gap:6px;
      padding: 6px 10px; border: 1px solid var(--border);
      background: var(--panel); border-radius: 999px; font-size: 12px; color: var(--muted);
    }}
    .dot {{
      width: 7px; height: 7px; border-radius: 50%;
      background: var(--ok);
      box-shadow: 0 0 0 3px rgba(61,220,151,.18);
    }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 16px 18px 34px; }}
    .grid {{ display: grid; grid-template-columns: 420px 1fr; gap: 14px; align-items: start; }}
    @media (max-width: 980px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--r);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .cardHeader {{
      padding: 14px 14px 12px;
      border-bottom: 1px solid var(--border);
      display:flex; align-items:center; justify-content: space-between; gap: 12px;
    }}
    .cardTitle {{ font-size: 13px; font-weight: 800; letter-spacing: .2px; }}
    .cardBody {{ padding: 14px; }}

    .row {{ display:flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .field {{ display:flex; flex-direction: column; gap: 6px; }}
    .label {{ font-size: 12px; color: var(--muted); }}
    select, input {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(0,0,0,.25);
      color: var(--text);
      outline: none;
    }}
    select:focus, input:focus {{
      border-color: rgba(110,168,255,.55);
      box-shadow: 0 0 0 4px rgba(110,168,255,.14);
    }}
    .btn {{
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,.06);
      color: var(--text);
      cursor: pointer;
      font-weight: 800;
      letter-spacing: .2px;
    }}
    .btn:hover {{ background: rgba(255,255,255,.10); }}
    .btnPrimary {{
      border: 1px solid rgba(110,168,255,.45);
      background: linear-gradient(135deg, rgba(110,168,255,.22), rgba(124,92,255,.18));
    }}
    .btnDanger {{
      border: 1px solid rgba(255,93,108,.50);
      background: rgba(255,93,108,.10);
      color: #ffd7db;
    }}
    .btnGhost {{ background: transparent; border: 1px solid var(--border); }}
    .hint {{ font-size: 12px; color: var(--muted); line-height: 1.55; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}

    .listTools {{
      display:flex; gap: 10px; flex-wrap: wrap; align-items: center;
      margin-bottom: 12px;
    }}
    .stat {{
      font-size: 12px; color: var(--muted);
      border: 1px solid var(--border);
      background: rgba(0,0,0,.18);
      padding: 8px 10px;
      border-radius: 12px;
      display:flex; gap: 8px; align-items:center;
    }}
    .stat b {{ color: var(--text); }}

    .charGrid {{ display:grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
    @media (max-width: 980px) {{ .charGrid {{ grid-template-columns: repeat(2, 1fr); }} }}
    @media (max-width: 520px) {{ .charGrid {{ grid-template-columns: 1fr; }} }}

    .charCard {{
      border: 1px solid var(--border);
      background: rgba(0,0,0,.18);
      border-radius: 14px;
      overflow: hidden;
      transition: transform .08s ease, background .12s ease, border-color .12s ease;
      cursor: pointer;
    }}
    .charCard:hover {{
      transform: translateY(-1px);
      background: rgba(0,0,0,.24);
      border-color: rgba(110,168,255,.32);
    }}
    .charInner {{ display:flex; align-items:center; gap: 10px; padding: 10px; }}
    .charInner input {{ width: 16px; height: 16px; accent-color: var(--brand); cursor: pointer; }}
    .avatarWrap {{
      width: 46px; height: 46px;
      border-radius: 14px;
      overflow: hidden;
      background: rgba(255,255,255,.06);
      border: 1px solid rgba(255,255,255,.10);
      flex: 0 0 auto;
    }}
    .avatar {{ width: 100%; height: 100%; object-fit: cover; display:block; }}
    .avatarFallback {{
      width:100%; height:100%;
      background: linear-gradient(135deg, rgba(110,168,255,.25), rgba(124,92,255,.20));
    }}
    .charText {{ min-width: 0; width: 100%; }}
    .charTop {{ display:flex; align-items:flex-start; justify-content: space-between; gap: 10px; }}
    .charName {{
      font-weight: 900; font-size: 13px; letter-spacing: .2px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px;
    }}
    .chipRow {{ display:flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }}
    .chip {{
      font-size: 11px; padding: 4px 8px; border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,.06);
      color: var(--muted);
      white-space: nowrap;
    }}
    .chipElem {{ color: rgba(110,168,255,.92); border-color: rgba(110,168,255,.25); }}
    .chipRole {{ color: rgba(61,220,151,.92); border-color: rgba(61,220,151,.22); }}
    .charSub {{
      margin-top: 6px; font-size: 12px; color: var(--muted2);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .charCard.selected {{
      border-color: rgba(110,168,255,.55);
      box-shadow: 0 0 0 4px rgba(110,168,255,.12);
    }}

    .resultArea {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
    .resultCard {{
      border: 1px solid var(--border);
      background: rgba(0,0,0,.20);
      border-radius: 16px;
      overflow: hidden;
    }}
    .resultHead {{
      display:flex; align-items:center; justify-content: space-between; gap: 10px;
      padding: 12px 12px;
      border-bottom: 1px solid var(--border);
    }}
    .rank {{ display:flex; align-items:center; gap: 10px; }}
    .badge {{
      width: 34px; height: 34px; border-radius: 12px;
      display:flex; align-items:center; justify-content:center;
      background: linear-gradient(135deg, rgba(110,168,255,.28), rgba(124,92,255,.20));
      border: 1px solid rgba(110,168,255,.30);
      font-weight: 900;
    }}
    .score {{ font-weight: 900; font-size: 14px; letter-spacing: .2px; }}
    .scoreSub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
    .resultBody {{ padding: 12px; }}
    .members {{ display:grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 10px; }}
    @media (max-width: 980px) {{ .members {{ grid-template-columns: repeat(2, minmax(160px, 1fr)); }} }}
    @media (max-width: 520px) {{ .members {{ grid-template-columns: 1fr; }} }}
    .mcard {{
      display:flex; gap: 10px; align-items:center;
      padding: 10px;
      border: 1px solid var(--border);
      background: rgba(0,0,0,.16);
      border-radius: 14px;
    }}
    .mimg {{
      width: 44px; height: 44px;
      border-radius: 14px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.06);
      flex: 0 0 auto;
    }}
    .mimg img {{ width:100%; height:100%; object-fit: cover; display:block; }}
    .mtext {{ min-width:0; width:100%; }}
    .mname {{ font-weight: 900; font-size: 13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .mmeta {{ margin-top: 4px; font-size: 12px; color: var(--muted); display:flex; gap: 6px; flex-wrap: wrap; }}
    .mmeta span {{ padding: 3px 7px; border-radius: 999px; border: 1px solid var(--border); background: rgba(255,255,255,.06); }}
    .reasons {{
      margin-top: 10px;
      padding: 10px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(0,0,0,.18);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }}
    .reasons ul {{ margin: 0; padding-left: 18px; }}
    .toast {{
      position: fixed; right: 16px; bottom: 16px; z-index: 50;
      background: rgba(0,0,0,.65);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 10px 12px;
      border-radius: 12px;
      box-shadow: var(--shadow);
      display:none;
      font-size: 12px;
    }}
  </style>
</head>
<body>

  <div class="topbar">
    <div class="topbarInner">
      <div class="title">
        <h1>{APP_TITLE}</h1>
        <div class="pill"><span class="dot"></span> Ready</div>
      </div>
      <div class="meta">
        cached <b>{len(chars)}</b> · refreshed <span class="mono">{CACHE["zone_nova"]["last_refresh_iso"] or "N/A"}</span> · source <b>{CACHE["zone_nova"]["source"] or "N/A"}</b>
      </div>
      <div style="display:flex; gap:8px; align-items:center;">
        <a class="pill" href="/">Meta</a>
        <a class="pill" href="/refresh">Refresh</a>
        <a class="pill" href="/zones/zone-nova/characters">JSON</a>
      </div>
    </div>
  </div>

  <div class="wrap">
    <div class="grid">

      <div class="card">
        <div class="cardHeader">
          <div class="cardTitle">추천 옵션</div>
          <div class="pill mono" title="기본 상성표">{advantage_line}</div>
        </div>
        <div class="cardBody">

          <div class="row">
            <div class="field" style="flex:1; min-width: 120px;">
              <div class="label">Mode</div>
              <select id="mode">
                <option value="pve">pve</option>
                <option value="boss">boss</option>
                <option value="pvp">pvp</option>
              </select>
            </div>
            <div class="field" style="width: 110px;">
              <div class="label">Top</div>
              <select id="top_k">
                <option value="3">3</option>
                <option value="5" selected>5</option>
                <option value="10">10</option>
              </select>
            </div>
          </div>

          <div style="height: 10px;"></div>

          <div class="row">
            <div class="field" style="flex:1; min-width: 160px;">
              <div class="label">Boss Weakness</div>
              <select id="boss_weakness">
                <option value="">(none)</option>
                {''.join([f'<option value="{e}">{e}</option>' for e in ALL_ELEMENTS])}
              </select>
            </div>
            <div class="field" style="flex:1; min-width: 160px;">
              <div class="label">Enemy Element</div>
              <select id="enemy_element">
                <option value="">(none)</option>
                {''.join([f'<option value="{e}">{e}</option>' for e in ALL_ELEMENTS])}
              </select>
            </div>
          </div>

          <div style="height: 12px;"></div>

          <div class="row">
            <button class="btn btnGhost" id="btnReq">선택 → Required</button>
            <button class="btn btnGhost" id="btnFocus">선택 → Focus</button>
            <button class="btn btnGhost" id="btnBan">선택 → Banned</button>
          </div>

          <div style="height: 12px;"></div>

          <div class="field">
            <div class="label">Required (id/name, comma)</div>
            <input id="required" placeholder="ex) nina, freya" />
          </div>

          <div style="height: 10px;"></div>

          <div class="field">
            <div class="label">Focus (id/name, comma)</div>
            <input id="focus" placeholder="ex) lavinia" />
          </div>

          <div style="height: 10px;"></div>

          <div class="field">
            <div class="label">Banned (id/name, comma)</div>
            <input id="banned" placeholder="ex) apep" />
          </div>

          <div style="height: 14px;"></div>

          <div class="row">
            <button class="btn btnPrimary" id="btnRun">Recommend</button>
            <button class="btn btnDanger" id="btnClear">Clear</button>
          </div>

          <div style="height: 12px;"></div>
          <div class="hint">
            팁: 검색/필터로 좁힌 뒤 “필터된 항목만 선택”을 쓰면 빠릅니다.
          </div>

        </div>
      </div>

      <div class="card">
        <div class="cardHeader">
          <div class="cardTitle">Owned 선택</div>
          <div class="row" style="margin-left:auto;">
            <div class="stat" id="selectedStat">Selected <b>0</b></div>
          </div>
        </div>

        <div class="cardBody">

          <div class="listTools">
            <div class="field" style="flex: 1; min-width: 180px;">
              <div class="label">Search</div>
              <input id="q" placeholder="name 또는 id 검색" />
            </div>

            <div class="field" style="width: 140px;">
              <div class="label">Element</div>
              <select id="f_element">
                <option value="">All</option>
                {''.join([f'<option value="{e}">{e}</option>' for e in ALL_ELEMENTS])}
                <option value="-">-</option>
              </select>
            </div>

            <div class="field" style="width: 140px;">
              <div class="label">Role</div>
              <select id="f_role">
                <option value="">All</option>
                <option value="tank">tank</option>
                <option value="healer">healer</option>
                <option value="dps">dps</option>
                <option value="buffer">buffer</option>
                <option value="debuffer">debuffer</option>
                <option value="-">-</option>
              </select>
            </div>

            <div class="field" style="width: 120px;">
              <div class="label">Rarity</div>
              <select id="f_rarity">
                <option value="">All</option>
                <option value="SSR">SSR</option>
                <option value="SR">SR</option>
                <option value="R">R</option>
                <option value="-">-</option>
              </select>
            </div>

            <div class="field" style="width: 160px;">
              <div class="label">Sort</div>
              <select id="sort">
                <option value="name" selected>Name</option>
                <option value="rarity">Rarity</option>
                <option value="element">Element</option>
                <option value="role">Role</option>
              </select>
            </div>
          </div>

          <div class="row" style="margin-bottom: 12px;">
            <button class="btn" id="btnAllOn">전체 선택</button>
            <button class="btn" id="btnAllOff">전체 해제</button>
            <button class="btn" id="btnVisOn">필터된 항목만 선택</button>
            <button class="btn" id="btnVisOff">필터된 항목만 해제</button>
          </div>

          <div class="charGrid" id="charGrid"></div>

          <div style="height: 16px;"></div>

          <div class="card" style="box-shadow:none;">
            <div class="cardHeader" style="border-bottom: 1px solid var(--border);">
              <div class="cardTitle">Result</div>
              <div class="row" style="margin-left:auto;">
                <button class="btn btnGhost" id="btnCopy">Copy JSON</button>
              </div>
            </div>
            <div class="cardBody">
              <div id="out" class="hint">(아직 없음)</div>
            </div>
          </div>

        </div>
      </div>

    </div>
  </div>

  <div class="toast" id="toast"></div>

<script>
const CHARS = {chars_json};

let LAST_JSON = null;

function toast(msg) {{
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {{ t.style.display = "none"; }}, 1600);
}}

function csv(v) {{
  v = (v || "").trim();
  if (!v) return [];
  return v.split(",").map(x => x.trim()).filter(Boolean);
}}
function uniq(arr) {{
  const s = new Set();
  arr.forEach(x => s.add(x));
  return Array.from(s);
}}

function setSelectedStat() {{
  const n = document.querySelectorAll(".owned:checked").length;
  document.getElementById("selectedStat").innerHTML = "Selected <b>" + n + "</b>";
}}

function applyFilter() {{
  const q = (document.getElementById("q").value || "").trim().toLowerCase();
  const fe = document.getElementById("f_element").value;
  const fr = (document.getElementById("f_role").value || "").toLowerCase();
  const frr = document.getElementById("f_rarity").value;

  document.querySelectorAll(".charCard").forEach(card => {{
    const id = card.dataset.id || "";
    const name = card.dataset.name || "";
    const el = card.dataset.element || "-";
    const role = (card.dataset.role || "-").toLowerCase();
    const rar = card.dataset.rarity || "-";

    let ok = true;
    if (q) ok = (id.includes(q) || name.includes(q));
    if (ok && fe) ok = (el === fe);
    if (ok && fr) ok = (role === fr);
    if (ok && frr) ok = (rar === frr);

    card.style.display = ok ? "" : "none";
  }});
}}

function applySort() {{
  const sortKey = document.getElementById("sort").value;
  const grid = document.getElementById("charGrid");
  const cards = Array.from(grid.children);

  const rarityOrder = {{ "SSR": 1, "SR": 2, "R": 3, "-": 9 }};
  const roleOrder = {{ "tank": 1, "healer": 2, "dps": 3, "debuffer": 4, "buffer": 5, "-": 9 }};
  const elemOrder = {{ "Fire": 1, "Ice": 2, "Wind": 3, "Holy": 4, "Chaos": 5, "-": 9 }};

  function key(card) {{
    if (sortKey === "rarity") return rarityOrder[card.dataset.rarity] || 9;
    if (sortKey === "role") return roleOrder[(card.dataset.role || "-").toLowerCase()] || 9;
    if (sortKey === "element") return elemOrder[card.dataset.element] || 9;
    return (card.dataset.name || "");
  }}

  cards.sort((a, b) => {{
    const ka = key(a);
    const kb = key(b);
    if (typeof ka === "number" && typeof kb === "number") return ka - kb;
    return String(ka).localeCompare(String(kb));
  }});

  cards.forEach(c => grid.appendChild(c));
  applyFilter();
}}

function syncSelectedCards() {{
  document.querySelectorAll(".charCard").forEach(card => {{
    const cb = card.querySelector(".owned");
    if (cb && cb.checked) card.classList.add("selected");
    else card.classList.remove("selected");
  }});
}}

function selectAll(flag) {{
  document.querySelectorAll(".owned").forEach(cb => cb.checked = flag);
  syncSelectedCards();
  setSelectedStat();
}}

function visibleCards() {{
  return Array.from(document.querySelectorAll(".charCard")).filter(card => card.style.display !== "none");
}}
function selectVisible(flag) {{
  visibleCards().forEach(card => {{
    const cb = card.querySelector(".owned");
    cb.checked = flag;
  }});
  syncSelectedCards();
  setSelectedStat();
}}

function checkedOwned() {{
  return Array.from(document.querySelectorAll(".owned:checked")).map(x => x.value);
}}

function pushTo(target) {{
  const owned = checkedOwned();
  if (owned.length === 0) {{
    toast("먼저 Owned 체크하세요.");
    return;
  }}
  const el = document.getElementById(target);
  const cur = csv(el.value);
  el.value = uniq(cur.concat(owned)).join(", ");
  toast(target + "에 추가됨 (" + owned.length + ")");
}}

function clearAll() {{
  document.querySelectorAll(".owned").forEach(b => b.checked = false);
  ["required", "focus", "banned"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("boss_weakness").value = "";
  document.getElementById("enemy_element").value = "";
  document.getElementById("q").value = "";
  document.getElementById("f_element").value = "";
  document.getElementById("f_role").value = "";
  document.getElementById("f_rarity").value = "";
  syncSelectedCards();
  setSelectedStat();
  document.getElementById("out").innerHTML = "(아직 없음)";
  LAST_JSON = null;
  toast("초기화 완료");
}}

function renderResult(data) {{
  LAST_JSON = data;

  if (!data.ok) {{
    document.getElementById("out").innerHTML =
      "<pre class='mono' style='white-space:pre-wrap;'>" + JSON.stringify(data, null, 2) + "</pre>";
    return;
  }}

  const parties = data.parties || [];
  if (parties.length === 0) {{
    document.getElementById("out").innerHTML = "<div class='hint'>조건을 만족하는 파티가 없습니다.</div>";
    return;
  }}

  let html = "";
  if ((data.issues || []).length) {{
    html += "<div class='hint' style='margin-bottom:10px; color: rgba(255,93,108,.9);'>issues: "
         + data.issues.join(" / ") + "</div>";
  }}

  html += "<div class='hint mono' style='margin-bottom:12px;'>inputs: " + JSON.stringify(data.inputs) + "</div>";
  html += "<div class='resultArea'>";

  parties.forEach((p, idx) => {{
    html += "<div class='resultCard'>";
    html += "<div class='resultHead'>";
    html += "<div class='rank'>";
    html += "<div class='badge'>#" + (idx + 1) + "</div>";
    html += "<div>";
    html += "<div class='score'>Score " + p.score + "</div>";
    html += "<div class='scoreSub'>members 4 · mode " + data.mode + "</div>";
    html += "</div></div>";
    html += "<div class='pill'>Top " + data.top_k + "</div>";
    html += "</div>";

    html += "<div class='resultBody'>";
    html += "<div class='members'>";
    (p.members || []).forEach(m => {{
      html += "<div class='mcard'>";
      html += "<div class='mimg'>";
      if (m.image) {{
        html += "<img src='" + m.image + "' onerror='this.style.display=\"none\"' />";
      }}
      html += "</div>";
      html += "<div class='mtext'>";
      html += "<div class='mname'>" + (m.name || m.id) + "</div>";
      html += "<div class='mmeta'>";
      html += "<span>" + (m.rarity || "-") + "</span>";
      html += "<span>" + (m.element || "-") + "</span>";
      html += "<span>" + (m.role || "-") + "</span>";
      html += "<span>score " + (m.score ?? "-") + "</span>";
      html += "</div></div></div>";
    }});
    html += "</div>";

    html += "<div class='reasons'><b>Analysis</b><ul>";
    (p.reasons || []).forEach(r => html += "<li>" + r + "</li>");
    html += "</ul></div>";

    html += "</div></div>";
  }});

  html += "</div>";
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

  if ((payload.owned || []).length < 4) {{
    toast("Owned는 최소 4명 필요합니다.");
    return;
  }}

  document.getElementById("out").innerHTML = "<div class='hint'>계산 중...</div>";
  const res = await fetch("/recommend/v3", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(payload)
  }});

  const json = await res.json();
  renderResult(json);
  toast("추천 완료");
}}

async function copyLast() {{
  if (!LAST_JSON) {{
    toast("복사할 결과가 없습니다.");
    return;
  }}
  try {{
    await navigator.clipboard.writeText(JSON.stringify(LAST_JSON, null, 2));
    toast("JSON 복사 완료");
  }} catch (e) {{
    toast("복사 실패(브라우저 권한 확인)");
  }}
}}

function buildCard(c) {{
  const id = c.id || "";
  const name = c.name || id;
  const rarity = c.rarity || "-";
  const element = c.element || "-";
  const role = c.role || "-";
  const img = c.image || "";

  const card = document.createElement("div");
  card.className = "charCard";
  card.dataset.id = id;
  card.dataset.name = String(name).toLowerCase();
  card.dataset.rarity = rarity;
  card.dataset.element = element;
  card.dataset.role = role;

  const label = document.createElement("label");
  label.className = "charInner";

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "owned";
  cb.value = id;

  const avatarWrap = document.createElement("div");
  avatarWrap.className = "avatarWrap";

  if (img) {{
    const im = document.createElement("img");
    im.className = "avatar";
    im.src = img;
    im.onerror = () => {{ im.style.display = "none"; }};
    avatarWrap.appendChild(im);
  }} else {{
    const fb = document.createElement("div");
    fb.className = "avatarFallback";
    avatarWrap.appendChild(fb);
  }}

  const text = document.createElement("div");
  text.className = "charText";

  const top = document.createElement("div");
  top.className = "charTop";

  const nm = document.createElement("div");
  nm.className = "charName";
  nm.textContent = name;

  const chipRow = document.createElement("div");
  chipRow.className = "chipRow";
  chipRow.innerHTML =
    "<span class='chip'>" + rarity + "</span>" +
    "<span class='chip chipElem'>" + element + "</span>" +
    "<span class='chip chipRole'>" + role + "</span>";

  top.appendChild(nm);
  top.appendChild(chipRow);

  const sub = document.createElement("div");
  sub.className = "charSub mono";
  sub.textContent = id;

  text.appendChild(top);
  text.appendChild(sub);

  label.appendChild(cb);
  label.appendChild(avatarWrap);
  label.appendChild(text);

  card.appendChild(label);

  // 클릭 토글(체크박스는 그대로)
  card.addEventListener("click", (ev) => {{
    if (ev.target && ev.target.tagName === "INPUT") return;
    cb.checked = !cb.checked;
    syncSelectedCards();
    setSelectedStat();
  }});
  cb.addEventListener("change", () => {{
    syncSelectedCards();
    setSelectedStat();
  }});

  return card;
}}

function renderChars() {{
  const grid = document.getElementById("charGrid");
  grid.innerHTML = "";
  CHARS.forEach(c => grid.appendChild(buildCard(c)));
}}

document.addEventListener("DOMContentLoaded", () => {{
  renderChars();

  document.getElementById("q").addEventListener("input", applyFilter);
  document.getElementById("f_element").addEventListener("change", applyFilter);
  document.getElementById("f_role").addEventListener("change", applyFilter);
  document.getElementById("f_rarity").addEventListener("change", applyFilter);
  document.getElementById("sort").addEventListener("change", applySort);

  document.getElementById("btnAllOn").addEventListener("click", () => selectAll(true));
  document.getElementById("btnAllOff").addEventListener("click", () => selectAll(false));
  document.getElementById("btnVisOn").addEventListener("click", () => selectVisible(true));
  document.getElementById("btnVisOff").addEventListener("click", () => selectVisible(false));

  document.getElementById("btnReq").addEventListener("click", () => pushTo("required"));
  document.getElementById("btnFocus").addEventListener("click", () => pushTo("focus"));
  document.getElementById("btnBan").addEventListener("click", () => pushTo("banned"));

  document.getElementById("btnRun").addEventListener("click", run);
  document.getElementById("btnClear").addEventListener("click", clearAll);
  document.getElementById("btnCopy").addEventListener("click", copyLast);

  applySort();
  applyFilter();
  syncSelectedCards();
  setSelectedStat();
}});
</script>

</body>
</html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    refresh_zone_nova_cache()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    app.run(host="0.0.0.0", port=port, debug=True)
