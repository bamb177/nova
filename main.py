from __future__ import annotations

import os
import re
import json
import time
import warnings
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning
from flask import Flask, request, Response, jsonify

# =========================
# App
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
# Remote Source (Render에서 성공 가능)
# =========================
ZONE_NOVA_DB_URL = "https://gachawiki.info/guides/zone-nova/characters/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 회사망에서 원격 스크랩을 확실히 끄고 싶으면:
# setx FORCE_LOCAL_ONLY 1
FORCE_LOCAL_ONLY = os.environ.get("FORCE_LOCAL_ONLY", "").strip() in {"1", "true", "TRUE", "yes", "YES"}

# =========================
# Repo Path (local clone)
# =========================
def find_repo_path() -> str:
    env = os.environ.get("GACHA_WIKI_REPO")
    if env and os.path.isdir(env):
        return env

    # 사용자 케이스 우선
    if os.path.isdir(r"C:\nova\gacha-wiki"):
        return r"C:\nova\gacha-wiki"

    cwd = os.getcwd()
    p = os.path.join(cwd, "gacha-wiki")
    return p

REPO_PATH = find_repo_path()
ZONE_NOVA_IMAGE_DIR = os.path.join(REPO_PATH, "public", "images", "games", "zone-nova", "characters")

# =========================
# Cache (메모리만)
# =========================
CACHE: Dict[str, Any] = {
    "zone_nova": {
        "characters": [],
        "count": 0,
        "last_refresh_iso": None,
        "error": None,
        "repo_path": REPO_PATH,
        "image_dir": ZONE_NOVA_IMAGE_DIR,
        "source": None,              # images_only | images+remote
        "remote_ok": False,
        "remote_error": None,
        "remote_count": 0,
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


def http_get(url: str, timeout: int = 25) -> str:
    """
    Render 환경에서는 대체로 정상, 회사망에서는 SSL/프록시로 실패할 수 있음.
    실패 시 예외를 올려서 상위에서 remote_error에 기록.
    """
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    # 1) verify=True 시도
    try:
        r = requests.get(url, headers=headers, timeout=timeout, verify=True)
        r.raise_for_status()
        return r.text
    except SSLError as e:
        # 2) 진단/개발용 fallback (회사망에서는 이것도 막힐 수 있음)
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
    """
    gachawiki Zone Nova Character Database 페이지의 테이블 텍스트를 파싱.
    - 의존성 최소화를 위해 bs4 없이 line 기반으로 파싱
    """
    lines = html_to_text_lines(html)

    # 테이블 헤더 찾기 (페이지 텍스트가 약간 바뀔 수 있어 유연하게)
    start_idx = None
    for i, ln in enumerate(lines):
        if ("Name" in ln and "Rarity" in ln and "Element" in ln and "Role" in ln and "HP" in ln and "Attack" in ln):
            if "Defense" in ln or "DEF" in ln:
                start_idx = i + 1
                break
    if start_idx is None:
        raise ValueError("원격 테이블 헤더를 찾지 못했습니다(페이지 구조 변경 가능).")

    # faction은 공백 포함 가능하므로 자주 쓰는 패턴을 폭넓게 허용(끝에서 stats로 끊기)
    # name (가변) + rarity + element + role + class + faction(가변) + hp + atk + def + crit
    # 여기서 faction은 숫자 토큰 나오기 전까지를 전부 흡수
    rarity_pat = r"(SSR|SR|R)"
    element_pat = r"(Chaos|Fire|Holy|Ice|Wind)"
    role_pat = r"(Buffer|DPS|Debuffer|Healer|Tank)"
    class_pat = r"(Buffer|Debuffer|Guardian|Healer|Mage|Rogue|Warrior)"
    num_pat = r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)"  # hp atk def crit

    pattern = re.compile(
        rf"^(?P<name>.+?)\s+{rarity_pat}\s+{element_pat}\s+{role_pat}\s+{class_pat}\s+(?P<faction>.+?)\s+{num_pat}$"
    )

    out: List[Dict[str, Any]] = []

    for ln in lines[start_idx:]:
        if re.fullmatch(r"\d+", ln):
            continue
        if ln.startswith("Image:"):
            continue

        cleaned = re.sub(r"\bImage:\s*[A-Za-z0-9\-_]+\b", "", ln).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        m = pattern.match(cleaned)
        if not m:
            continue

        name = m.group("name").strip()
        rarity = m.group(2)  # SSR/SR/R
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
            "source": {"type": "remote_scrape", "url": ZONE_NOVA_DB_URL},
        })

    # 중복 제거
    uniq = {c["id"]: c for c in out}
    return list(uniq.values())


def load_from_images() -> List[Dict[str, Any]]:
    """
    public/images/games/zone-nova/characters 폴더에서 항상 성공하는 46명 목록 생성
    """
    if not os.path.isdir(ZONE_NOVA_IMAGE_DIR):
        raise FileNotFoundError(f"Zone Nova 캐릭터 이미지 폴더가 없습니다: {ZONE_NOVA_IMAGE_DIR}")

    chars: List[Dict[str, Any]] = []
    for fn in os.listdir(ZONE_NOVA_IMAGE_DIR):
        low = fn.lower()
        if not (low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png") or low.endswith(".webp")):
            continue

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
            "source": {"type": "image_index", "path": ZONE_NOVA_IMAGE_DIR},
        })

    uniq = {c["id"]: c for c in chars}
    out = list(uniq.values())
    out.sort(key=lambda x: x["name"].lower())
    return out


def merge_image_and_remote(image_chars: List[Dict[str, Any]], remote_chars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    id(=slugified name) 기준으로 merge.
    - 이미지가 제공하는 image 경로는 유지
    - 원격이 제공하는 rarity/element/role/class/faction/stats를 덮어씀
    """
    by_id = {c["id"]: c for c in image_chars}

    for rc in remote_chars:
        rid = rc["id"]
        if rid in by_id:
            ic = by_id[rid]
            # 원격 메타 덮어쓰기
            for k in ["rarity", "element", "role", "class", "faction"]:
                ic[k] = rc.get(k)
            ic["stats"] = rc.get("stats") or ic.get("stats")
            ic["source"] = {"type": "images+remote", "image": ic["source"], "remote": rc.get("source")}
        else:
            # 원격에만 있는 캐릭터가 있으면 추가(이미지는 없을 수 있음)
            rc2 = dict(rc)
            rc2["image"] = None
            by_id[rid] = rc2

    out = list(by_id.values())
    out.sort(key=lambda x: x["name"].lower())
    return out


# =========================
# Refresh
# =========================
def refresh_zone_nova_cache() -> Tuple[bool, str]:
    try:
        # 1) 이미지 기반 목록은 항상 로드 (회사망에서도 성공)
        image_chars = load_from_images()

        remote_ok = False
        remote_err = None
        remote_chars: List[Dict[str, Any]] = []

        # 2) 원격 스크랩은 가능한 환경(Render 등)에서만 성공할 것
        if not FORCE_LOCAL_ONLY:
            try:
                html = http_get(ZONE_NOVA_DB_URL, timeout=25)
                remote_chars = parse_remote_zone_nova_characters(html)
                if len(remote_chars) >= 10:
                    remote_ok = True
            except Exception as e:
                remote_ok = False
                remote_err = str(e)

        # 3) merge
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
        CACHE["zone_nova"]["repo_path"] = REPO_PATH
        CACHE["zone_nova"]["image_dir"] = ZONE_NOVA_IMAGE_DIR
        CACHE["zone_nova"]["remote_ok"] = remote_ok
        CACHE["zone_nova"]["remote_error"] = remote_err
        CACHE["zone_nova"]["remote_count"] = len(remote_chars)

        return True, f"ok: {CACHE['zone_nova']['count']} characters (source={CACHE['zone_nova']['source']})"
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


def get_zone_nova_characters() -> List[Dict[str, Any]]:
    if CACHE["zone_nova"]["characters"]:
        return CACHE["zone_nova"]["characters"]
    refresh_zone_nova_cache()
    return CACHE["zone_nova"]["characters"]


# =========================
# Recommend (4인 고정: Tank+Healer 우선)
# =========================
def rarity_bonus(r: Optional[str]) -> float:
    if not r:
        return 0.0
    r = r.upper()
    return {"SSR": 20.0, "SR": 10.0, "R": 0.0}.get(r, 0.0)


def score_character(c: Dict[str, Any], mode: str) -> float:
    """
    - 원격 메타가 있으면 stats 기반으로 점수가 의미있어짐
    - 로컬만(이미지)인 경우도 rarity/role이 없으므로 큰 차이는 없음(그래도 동작)
    """
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

    base += (float(crit) / 10.0) if isinstance(crit, (int, float)) else 0.0

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

    return base + role_adj + rarity_bonus(c.get("rarity"))


def recommend_party(owned_ids_or_names: List[str], mode: str) -> Dict[str, Any]:
    chars = get_zone_nova_characters()
    by_id = {c["id"].lower(): c for c in chars}
    by_name = {c["name"].lower(): c for c in chars}

    owned: List[Dict[str, Any]] = []
    for x in owned_ids_or_names:
        k = (x or "").strip().lower()
        if not k:
            continue
        if k in by_id:
            owned.append(by_id[k])
        elif k in by_name:
            owned.append(by_name[k])

    uniq = {c["id"]: c for c in owned}
    owned = list(uniq.values())

    if not owned:
        return {"error": "owned 배열이 비어 있습니다.", "mode": mode}

    tanks = [c for c in owned if (c.get("role") or "").lower() == "tank"]
    healers = [c for c in owned if (c.get("role") or "").lower() == "healer"]
    others = [c for c in owned if c not in tanks and c not in healers]

    tanks.sort(key=lambda c: score_character(c, mode), reverse=True)
    healers.sort(key=lambda c: score_character(c, mode), reverse=True)
    others.sort(key=lambda c: score_character(c, mode), reverse=True)

    party: List[Dict[str, Any]] = []
    issues: List[str] = []

    if tanks:
        party.append(tanks[0])
    else:
        issues.append("탱커 없음")

    if healers:
        party.append(healers[0])
    else:
        issues.append("힐러 없음")

    used = {c["id"] for c in party}
    for c in others:
        if c["id"] in used:
            continue
        party.append(c)
        used.add(c["id"])
        if len(party) == PARTY_SIZE:
            break

    # 부족하면 남은 탱/힐로 보충
    if len(party) < PARTY_SIZE:
        pool = tanks[1:] + healers[1:]
        pool.sort(key=lambda c: score_character(c, mode), reverse=True)
        for c in pool:
            if c["id"] in used:
                continue
            party.append(c)
            used.add(c["id"])
            if len(party) == PARTY_SIZE:
                break

    if len(party) < PARTY_SIZE:
        issues.append(f"보유 풀 부족: {len(party)}명만 구성")

    out_party = []
    for c in party:
        out_party.append({
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

    return {
        "mode": mode,
        "best_party": out_party,
        "issues": issues,
        "data_source": CACHE["zone_nova"]["source"],
        "remote_ok": CACHE["zone_nova"]["remote_ok"],
        "remote_count": CACHE["zone_nova"]["remote_count"],
        "remote_error": CACHE["zone_nova"]["remote_error"],
    }


# =========================
# Routes
# =========================
@app.get("/")
def home() -> Response:
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
    .small {{ font-size: 12px; color: #555; }}
  </style>
</head>
<body>
  <h1>Zone Nova Meta</h1>
  <div class="box">
    <div class="row">Repo path: <code>{zn.get("repo_path")}</code></div>
    <div class="row">Image dir: <code>{zn.get("image_dir")}</code></div>
    <div class="row">Last refresh: <code>{zn.get("last_refresh_iso") or "N/A"}</code></div>
    <div class="row">Characters cached: <code>{zn.get("count")}</code></div>
    <div class="row">Source: <code>{zn.get("source") or "N/A"}</code></div>
    <div class="row">Remote scrape: <code>{zn.get("remote_ok")}</code> (remote_count=<code>{zn.get("remote_count")}</code>)</div>
    <div class="row small">FORCE_LOCAL_ONLY: <code>{FORCE_LOCAL_ONLY}</code></div>
    <div class="row">Remote error:</div>
    <div class="err">{zn.get("remote_error") or "None"}</div>

    <div class="row" style="margin-top: 12px;">
      <form method="post" action="/refresh" style="display:inline;">
        <button type="submit">Refresh</button>
      </form>
      &nbsp;
      <a href="/zones/zone-nova/characters">/zones/zone-nova/characters</a>
      &nbsp;|&nbsp;
      <a href="/ui/select">/ui/select</a>
      &nbsp;|&nbsp;
      <a href="/recommend">/recommend</a>
    </div>
  </div>
</body>
</html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.post("/refresh")
def refresh() -> Response:
    ok, msg = refresh_zone_nova_cache()
    status = 200 if ok else 500
    return jsonify({"ok": ok, "message": msg, "zone_nova": CACHE["zone_nova"]}), status


@app.get("/zones/zone-nova/characters")
def zone_nova_characters() -> Response:
    chars = get_zone_nova_characters()
    payload = {
        "game": "zone-nova",
        "count": len(chars),
        "last_refresh": CACHE["zone_nova"]["last_refresh_iso"],
        "source": CACHE["zone_nova"]["source"],
        "remote_ok": CACHE["zone_nova"]["remote_ok"],
        "remote_count": CACHE["zone_nova"]["remote_count"],
        "remote_error": CACHE["zone_nova"]["remote_error"],
        "characters": chars,
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
  <p>POST로 추천 결과를 반환합니다(4인 고정: Tank+Healer 우선).</p>
  <pre>
POST /recommend
Content-Type: application/json

{
  "mode": "pve",   // pve | boss | pvp
  "owned": ["nina", "freya", "lavinia", "apep"]
}
  </pre>
</body>
</html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.post("/recommend")
def recommend() -> Response:
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "pve").strip().lower()
    if mode not in {"pve", "boss", "pvp"}:
        mode = "pve"

    owned = data.get("owned") or []
    if not isinstance(owned, list):
        return Response(json.dumps({"error": "owned는 배열이어야 합니다."}, ensure_ascii=False),
                        mimetype="application/json; charset=utf-8", status=400)

    result = recommend_party([str(x) for x in owned], mode)
    return Response(json.dumps(result, ensure_ascii=False), mimetype="application/json; charset=utf-8")


# 간단 UI: 캐릭터 체크 → 추천 호출
@app.get("/ui/select")
def ui_select() -> Response:
    chars = get_zone_nova_characters()
    # 간단 목록(이미지 + 이름)
    items = []
    for c in chars:
        img = c.get("image")
        img_tag = f'<img src="{img}" style="width:48px;height:48px;object-fit:cover;border-radius:8px;margin-right:8px;" />' if img else ""
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


# =========================
# Boot
# =========================
if __name__ == "__main__":
    refresh_zone_nova_cache()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    app.run(host="0.0.0.0", port=port, debug=True)
