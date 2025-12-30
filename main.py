from __future__ import annotations

import os
import re
import json
import warnings
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning
from flask import Flask, request, Response, send_from_directory, abort

from bs4 import BeautifulSoup  # requirements.txt에 beautifulsoup4 필요


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


def parse_remote_zone_nova_characters_bs4(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return []

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).lower()

    target = None
    headers: List[str] = []

    # "Name/Rarity/Element/Role/HP" 조합을 가진 테이블 찾기
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
            and any("hp" in h for h in headers)
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
            # 원격에만 있는 캐릭터(이미지 없음)도 포함
            rc2 = dict(rc)
            rc2["image"] = None
            by_id[rid] = rc2

    out = list(by_id.values())
    out.sort(key=lambda x: x["name"].lower())
    return out


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
    # gunicorn 환경에서 최초 요청 시 자동 로드
    if CACHE["zone_nova"]["count"] == 0 and CACHE["zone_nova"]["last_refresh_iso"] is None:
        refresh_zone_nova_cache()


# =========================
# Scoring / Recommend
# =========================
def rarity_bonus(r: Optional[str]) -> float:
    r = (r or "").upper().strip()
    return {"SSR": 20.0, "SR": 10.0, "R": 0.0}.get(r, 0.0)


def role_bonus(role: Optional[str], mode: str) -> float:
    r = (role or "").strip().lower()

    # 모드별 역할 선호
    if mode == "boss":
        # 보스전: 딜/디버프 우선
        if r == "dps":
            return 12.0
        if r == "debuffer":
            return 9.0
        if r == "buffer":
            return 6.0
        if r == "tank":
            return 3.0
        if r == "healer":
            return 3.0
        return 0.0

    if mode == "pvp":
        # PVP: 생존(탱/힐) 우선
        if r == "tank":
            return 12.0
        if r == "healer":
            return 12.0
        if r == "debuffer":
            return 8.0
        if r == "buffer":
            return 7.0
        if r == "dps":
            return 5.0
        return 0.0

    # pve(기본): 밸런스
    if r == "tank":
        return 10.0
    if r == "healer":
        return 10.0
    if r == "dps":
        return 9.0
    if r == "debuffer":
        return 7.0
    if r == "buffer":
        return 7.0
    return 0.0


def score_character(c: Dict[str, Any], mode: str) -> float:
    st = c.get("stats") or {}
    hp = st.get("hp") or 0
    atk = st.get("atk") or 0
    df = st.get("def") or 0
    crit = st.get("crit") or 0.0

    hp_s = float(hp) / 1000.0
    atk_s = float(atk) / 100.0
    df_s = float(df) / 100.0

    # 모드별 스탯 비중
    if mode == "boss":
        base = 0.75 * atk_s + 0.10 * df_s + 0.15 * hp_s
    elif mode == "pvp":
        base = 0.20 * atk_s + 0.45 * df_s + 0.35 * hp_s
    else:  # pve
        base = 0.55 * atk_s + 0.20 * df_s + 0.25 * hp_s

    try:
        base += float(crit) / 10.0
    except Exception:
        pass

    base += rarity_bonus(c.get("rarity"))
    base += role_bonus(c.get("role"), mode)
    return base


def build_party(owned_chars: List[Dict[str, Any]], mode: str) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """
    4인 고정, 탱 1 + 힐 1 우선.
    반환: (party, issues, alternatives_top3)
    """
    issues: List[str] = []

    tanks = [c for c in owned_chars if (c.get("role") or "").strip().lower() == "tank"]
    healers = [c for c in owned_chars if (c.get("role") or "").strip().lower() == "healer"]
    others = [c for c in owned_chars if c not in tanks and c not in healers]

    tanks.sort(key=lambda c: score_character(c, mode), reverse=True)
    healers.sort(key=lambda c: score_character(c, mode), reverse=True)
    others.sort(key=lambda c: score_character(c, mode), reverse=True)

    party: List[Dict[str, Any]] = []
    used: set[str] = set()

    if tanks:
        party.append(tanks[0]); used.add(tanks[0]["id"])
    else:
        issues.append("탱커 없음")

    if healers:
        if healers[0]["id"] not in used:
            party.append(healers[0]); used.add(healers[0]["id"])
    else:
        issues.append("힐러 없음")

    # 나머지 2자리(모드 점수순)
    for c in others:
        if c["id"] in used:
            continue
        party.append(c); used.add(c["id"])
        if len(party) == PARTY_SIZE:
            break

    # 부족하면 남은 탱/힐로 채움
    if len(party) < PARTY_SIZE:
        pool = tanks[1:] + healers[1:]
        pool.sort(key=lambda c: score_character(c, mode), reverse=True)
        for c in pool:
            if c["id"] in used:
                continue
            party.append(c); used.add(c["id"])
            if len(party) == PARTY_SIZE:
                break

    if len(party) < PARTY_SIZE:
        issues.append(f"보유 풀 부족: {len(party)}명만 구성")

    remaining = [c for c in owned_chars if c["id"] not in used]
    remaining.sort(key=lambda c: score_character(c, mode), reverse=True)
    alternatives = remaining[:3]

    return party, issues, alternatives


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
    <div class="row">BS4 available: <code>{zn.get("remote_bs4_available")}</code></div>
    <div class="row">FORCE_LOCAL_ONLY: <code>{zn.get("force_local_only")}</code></div>

    <div class="row">Remote error:</div>
    <div class="err">{zn.get("remote_error") or "None"}</div>

    <div class="row">Cache error:</div>
    <div class="err">{zn.get("error") or "None"}</div>

    <div class="row" style="margin-top: 12px;">
      <a href="/refresh" style="margin-right:10px;">Refresh</a>
      <a href="/zones/zone-nova/characters" style="margin-right:10px;">/zones/zone-nova/characters</a>
      <a href="/ui/select" style="margin-right:10px;">/ui/select</a>
      <a href="/recommend">/recommend</a>
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


@app.get("/recommend")
def recommend_help() -> Response:
    html = f"""
<!doctype html>
<html lang="ko">
<head><meta charset="utf-8" /><title>{APP_TITLE}</title></head>
<body style="font-family: Arial, sans-serif; margin: 24px;">
  <h2>{APP_TITLE} /recommend</h2>
  <p>POST로 추천 결과를 반환합니다(4인 고정, 탱커/힐러 우선).</p>
  <pre style="background:#f5f5f5;padding:12px;border-radius:8px;">
POST /recommend
Content-Type: application/json

{{
  "mode": "pve",   // pve | boss | pvp
  "owned": ["nina", "freya", "lavinia", "apep"]
}}
  </pre>
</body></html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.post("/recommend")
def recommend() -> Response:
    ensure_cache_loaded()
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "pve").strip().lower()
    if mode not in {"pve", "boss", "pvp"}:
        mode = "pve"

    owned = data.get("owned") or []
    if not isinstance(owned, list):
        return Response(
            json.dumps({"error": "owned는 배열이어야 합니다."}, ensure_ascii=False),
            mimetype="application/json; charset=utf-8",
            status=400,
        )

    chars = CACHE["zone_nova"]["characters"]
    by_id = {c["id"].lower(): c for c in chars}
    by_name = {c["name"].lower(): c for c in chars}

    owned_chars: List[Dict[str, Any]] = []
    for x in owned:
        k = str(x).strip().lower()
        if not k:
            continue
        if k in by_id:
            owned_chars.append(by_id[k])
        elif k in by_name:
            owned_chars.append(by_name[k])

    # 중복 제거
    owned_chars = list({c["id"]: c for c in owned_chars}.values())

    if not owned_chars:
        return Response(
            json.dumps({"error": "owned에서 유효한 캐릭터를 찾지 못했습니다."}, ensure_ascii=False),
            mimetype="application/json; charset=utf-8",
            status=400,
        )

    party, issues, alternatives = build_party(owned_chars, mode)

    def pack(c: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": c["id"],
            "name": c["name"],
            "rarity": c.get("rarity"),
            "element": c.get("element"),
            "role": c.get("role"),
            "class": c.get("class"),
            "faction": c.get("faction"),
            "image": c.get("image"),
            "score": round(score_character(c, mode), 2),
        }

    result = {
        "mode": mode,
        "best_party": [pack(c) for c in party],
        "issues": issues,
        "alternatives": [pack(c) for c in alternatives],
        "data_source": CACHE["zone_nova"]["source"],
        "remote_ok": CACHE["zone_nova"]["remote_ok"],
        "remote_count": CACHE["zone_nova"]["remote_count"],
        "remote_error": CACHE["zone_nova"]["remote_error"],
    }

    return Response(json.dumps(result, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.get("/ui/select")
def ui_select() -> Response:
    ensure_cache_loaded()
    chars = CACHE["zone_nova"]["characters"]

    items = []
    for c in chars:
        img = c.get("image")
        img_tag = (
            f'<img src="{img}" style="width:44px;height:44px;object-fit:cover;border-radius:8px;margin-right:8px;" />'
            if img else ""
        )
        items.append(f"""
          <label style="display:flex;align-items:center;gap:8px;padding:6px 0;">
            <input type="checkbox" name="owned" value="{c['id']}" />
            {img_tag}
            <span>{c['name']}</span>
            <span style="color:#777;font-size:12px;">
              ({c.get('rarity') or '-'} / {c.get('element') or '-'} / {c.get('role') or '-'})
            </span>
          </label>
        """)

    html = f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>{APP_TITLE}</title>
</head>
<body style="font-family: Arial, sans-serif; margin: 24px;">
  <h2>{APP_TITLE} - 캐릭터 선택</h2>
  <div style="border:1px solid #ddd;padding:16px;border-radius:8px;max-width:1100px;">
    <div style="margin-bottom:12px;">
      Mode:
      <select id="mode">
        <option value="pve">pve</option>
        <option value="boss">boss</option>
        <option value="pvp">pvp</option>
      </select>
      <button onclick="submitRecommend()" style="padding:8px 12px;">추천</button>
      <a href="/" style="margin-left:12px;">홈</a>
    </div>

    <div style="max-height:520px;overflow:auto;border:1px solid #eee;padding:10px;border-radius:8px;">
      {''.join(items)}
    </div>

    <h3>결과</h3>
    <pre id="out" style="background:#f5f5f5;padding:12px;border-radius:8px;white-space:pre-wrap;">(아직 없음)</pre>
  </div>

<script>
async function submitRecommend() {{
  const mode = document.getElementById("mode").value;
  const checked = Array.from(document.querySelectorAll('input[name="owned"]:checked')).map(x => x.value);
  const res = await fetch("/recommend", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ mode, owned: checked }})
  }});
  const json = await res.json();
  document.getElementById("out").textContent = JSON.stringify(json, null, 2);
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
