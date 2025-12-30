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
from flask import Flask, request, Response, jsonify, send_from_directory, abort

# =========================
# Flask
# =========================
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
try:
    app.json.ensure_ascii = False  # Flask 2.3+
except Exception:
    pass

KST = timezone(timedelta(hours=9))
DEFAULT_PORT = 40000
PARTY_SIZE = 4

# =========================
# Remote (Render에서 성공 기대)
# =========================
ZONE_NOVA_DB_URL = "https://gachawiki.info/guides/zone-nova/characters/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
FORCE_LOCAL_ONLY = os.environ.get("FORCE_LOCAL_ONLY", "").strip() in {"1", "true", "TRUE", "yes", "YES"}

# =========================
# Paths (Render/Local 공통)
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 이미지 폴더 후보: 둘 중 하나만 있어도 동작
IMAGE_DIR_CANDIDATES = [
    os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters"),
    os.path.join(BASE_DIR, "gacha-wiki", "public", "images", "games", "zone-nova", "characters"),
]

# /images/ 파일 제공용 base 후보(폴더 자체)
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
        "image_dir": None,           # 실제 사용한 이미지 디렉터리
        "image_count": 0,            # 이미지 파일 수
        "remote_ok": False,
        "remote_error": None,
        "remote_count": 0,
        "force_local_only": FORCE_LOCAL_ONLY,
    }
}


# =========================
# Utils
# =========================
def now_iso_kst() -> str:
    return datetime.now(tz=KST).isoformat(timespec="seconds")


def slugify(s: str) -> str:
    s = s.strip()
    s = s.replace("’", "").replace("'", "")
    s = re.sub(r"[^A-Za-z0-9\s\-_]", "", s)
    s = s.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def prettify_name_from_stem(stem: str) -> str:
    s = stem.strip()
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)  # CamelCase split
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

    # 1) verify=True
    try:
        r = requests.get(url, headers=headers, timeout=timeout, verify=True)
        r.raise_for_status()
        return r.text
    except SSLError:
        # 2) verify=False fallback (일부 환경에서 필요)
        warnings.simplefilter("ignore", InsecureRequestWarning)
        r = requests.get(url, headers=headers, timeout=timeout, verify=False)
        r.raise_for_status()
        return r.text


def html_to_text_lines(html: str) -> List[str]:
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    txt = re.sub(r"(?is)<[^>]+>", " ", html)
    txt = (
        txt.replace("&nbsp;", " ")
           .replace("&amp;", "&")
           .replace("&quot;", '"')
           .replace("&#39;", "'")
    )
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r"\n+", "\n", txt)
    lines = [ln.strip() for ln in txt.split("\n")]
    return [ln for ln in lines if ln]


def parse_remote_zone_nova_characters(html: str) -> List[Dict[str, Any]]:
    lines = html_to_text_lines(html)

    # 테이블 헤더 탐색(유연)
    start_idx = None
    for i, ln in enumerate(lines):
        if ("Name" in ln and "Rarity" in ln and "Element" in ln and "Role" in ln and "HP" in ln and "Attack" in ln):
            if "Defense" in ln or "DEF" in ln:
                start_idx = i + 1
                break
    if start_idx is None:
        raise ValueError("원격 테이블 헤더를 찾지 못했습니다(페이지 구조 변경 가능).")

    rarity_pat = r"(SSR|SR|R)"
    element_pat = r"(Chaos|Fire|Holy|Ice|Wind)"
    role_pat = r"(Buffer|DPS|Debuffer|Healer|Tank)"
    class_pat = r"(Buffer|Debuffer|Guardian|Healer|Mage|Rogue|Warrior)"
    num_pat = r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)"

    pattern = re.compile(
        rf"^(?P<name>.+?)\s+{rarity_pat}\s+{element_pat}\s+{role_pat}\s+{class_pat}\s+(?P<faction>.+?)\s+{num_pat}$"
    )

    out: List[Dict[str, Any]] = []
    for ln in lines[start_idx:]:
        cleaned = re.sub(r"\bImage:\s*[A-Za-z0-9\-_]+\b", "", ln).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        m = pattern.match(cleaned)
        if not m:
            continue

        name = m.group("name").strip()
        rarity = m.group(2)
        element = m.group(3)
        role = m.group(4)
        clazz = m.group(5)
        faction = m.group("faction").strip()

        hp = int(m.group(6).replace(",", ""))
        atk = int(m.group(7).replace(",", ""))
        df = int(m.group(8).replace(",", ""))
        crit = float(m.group(9))

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
    files = []
    for fn in os.listdir(image_dir):
        low = fn.lower()
        if low.endswith((".jpg", ".jpeg", ".png", ".webp")):
            files.append(fn)

    chars: List[Dict[str, Any]] = []
    for fn in files:
        stem = os.path.splitext(fn)[0]
        name = prettify_name_from_stem(stem)
        cid = slugify(name)

        # 웹에서 요청하는 경로는 /images/... 로 통일
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
            # 이미지 없는 원격 캐릭터(있으면 추가)
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
                "Zone Nova 캐릭터 이미지 폴더를 찾지 못했습니다.\n"
                + "\n".join([f"- {p}" for p in IMAGE_DIR_CANDIDATES])
            )

        image_chars, img_count = load_from_images(image_dir)

        remote_ok = False
        remote_err = None
        remote_chars: List[Dict[str, Any]] = []

        if not FORCE_LOCAL_ONLY:
            try:
                html = http_get(ZONE_NOVA_DB_URL, timeout=25)
                remote_chars = parse_remote_zone_nova_characters(html)
                if len(remote_chars) >= 10:
                    remote_ok = True
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
    # Gunicorn에서는 __main__이 안 돌아가므로, 최초 요청 시 자동 로드
    if CACHE["zone_nova"]["count"] == 0 and CACHE["zone_nova"]["last_refresh_iso"] is None:
        refresh_zone_nova_cache()


def score_character(c: Dict[str, Any], mode: str) -> float:
    st = c.get("stats") or {}
    hp = st.get("hp") or 0
    atk = st.get("atk") or 0
    df = st.get("def") or 0
    crit = st.get("crit") or 0.0

    hp_s = float(hp) / 1000.0
    atk_s = float(atk) / 100.0
    df_s = float(df) / 100.0

    if mode == "pvp":
        base = 0.55 * df_s + 0.35 * hp_s + 0.10 * atk_s
    elif mode == "boss":
        base = 0.65 * atk_s + 0.20 * df_s + 0.15 * hp_s
    else:
        base = 0.60 * atk_s + 0.20 * hp_s + 0.20 * df_s

    try:
        base += float(crit) / 10.0
    except Exception:
        pass

    role = (c.get("role") or "").lower()
    role_adj = 0.0
    if "tank" in role:
        role_adj = 8.0
    elif "healer" in role:
        role_adj = 8.0
    elif "dps" in role:
        role_adj = 6.0
    elif "debuff" in role:
        role_adj = 4.0
    elif "buff" in role:
        role_adj = 4.0

    rarity = (c.get("rarity") or "").upper()
    rarity_adj = {"SSR": 20.0, "SR": 10.0, "R": 0.0}.get(rarity, 0.0)

    return base + role_adj + rarity_adj


# =========================
# Static images serving
# =========================
@app.get("/images/<path:filename>")
def serve_images(filename: str):
    # filename 예: games/zone-nova/characters/Apep.jpg
    for base in IMAGES_BASE_CANDIDATES:
        full = os.path.join(base, filename)
        if os.path.isfile(full):
            # send_from_directory는 base + filename을 조합해 제공
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
  <title>Zone Nova Meta</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    code {{ background: #f5f5f5; padding: 2px 6px; }}
    .box {{ border: 1px solid #ddd; padding: 16px; border-radius: 8px; max-width: 1100px; }}
    .row {{ margin: 8px 0; }}
    .err {{ color: #b00020; white-space: pre-wrap; }}
    button {{ padding: 8px 12px; }}
    a {{ text-decoration: none; }}
  </style>
</head>
<body>
  <h1>Zone Nova Meta</h1>
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
    html = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>Recommend</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    pre { background: #f5f5f5; padding: 12px; border-radius: 8px; }
  </style>
</head>
<body>
  <h2>/recommend</h2>
  <p>POST로 추천 결과를 반환합니다(4인 고정).</p>
  <pre>
POST /recommend
Content-Type: application/json

{
  "mode": "pve",
  "owned": ["nina", "freya", "lavinia", "apep"]
}
  </pre>
</body>
</html>
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

    owned_chars = []
    for x in owned:
        k = str(x).strip().lower()
        if k in by_id:
            owned_chars.append(by_id[k])
        elif k in by_name:
            owned_chars.append(by_name[k])

    uniq = {c["id"]: c for c in owned_chars}
    owned_chars = list(uniq.values())

    if len(owned_chars) < PARTY_SIZE:
        return Response(
            json.dumps({"error": f"최소 {PARTY_SIZE}명 필요", "count_owned": len(owned_chars)}, ensure_ascii=False),
            mimetype="application/json; charset=utf-8",
            status=400,
        )

    # (간단) 점수 상위 4명
    owned_chars.sort(key=lambda c: score_character(c, mode), reverse=True)
    party = owned_chars[:PARTY_SIZE]

    out = []
    for c in party:
        out.append({
            "id": c["id"],
            "name": c["name"],
            "rarity": c.get("rarity"),
            "element": c.get("element"),
            "role": c.get("role"),
            "class": c.get("class"),
            "faction": c.get("faction"),
            "image": c.get("image"),
            "score": round(score_character(c, mode), 2),
        })

    result = {
        "mode": mode,
        "best_party": out,
        "data_source": CACHE["zone_nova"]["source"],
        "remote_ok": CACHE["zone_nova"]["remote_ok"],
        "remote_count": CACHE["zone_nova"]["remote_count"],
        "remote_error": CACHE["zone_nova"]["remote_error"],
        "error": CACHE["zone_nova"]["error"],
    }
    return Response(json.dumps(result, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.get("/ui/select")
def ui_select() -> Response:
    ensure_cache_loaded()
    chars = CACHE["zone_nova"]["characters"]

    items = []
    for c in chars:
        img = c.get("image")
        img_tag = f'<img src="{img}" style="width:44px;height:44px;object-fit:cover;border-radius:8px;margin-right:8px;" />' if img else ""
        items.append(f"""
          <label style="display:flex;align-items:center;gap:8px;padding:6px 0;">
            <input type="checkbox" name="owned" value="{c['id']}" />
            {img_tag}
            <span>{c['name']}</span>
            <span style="color:#777;font-size:12px;">({c.get('rarity') or '-'} / {c.get('element') or '-'} / {c.get('role') or '-'})</span>
          </label>
        """)

    html = f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>Select Characters</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    .box {{ border: 1px solid #ddd; padding: 16px; border-radius: 8px; max-width: 1100px; }}
    button {{ padding: 8px 12px; }}
    pre {{ background: #f5f5f5; padding: 12px; border-radius: 8px; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h2>내가 가진 캐릭터 선택</h2>
  <div class="box">
    <div style="margin-bottom:12px;">
      Mode:
      <select id="mode">
        <option value="pve">pve</option>
        <option value="boss">boss</option>
        <option value="pvp">pvp</option>
      </select>
      <button onclick="submitRecommend()">추천</button>
      <a href="/" style="margin-left:12px;">홈</a>
    </div>
    <div style="max-height:520px;overflow:auto;border:1px solid #eee;padding:10px;border-radius:8px;">
      {''.join(items)}
    </div>
    <h3>결과</h3>
    <pre id="out">(아직 없음)</pre>
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


# 로컬 실행용
if __name__ == "__main__":
    refresh_zone_nova_cache()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    app.run(host="0.0.0.0", port=port, debug=True)
