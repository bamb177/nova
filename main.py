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

@app.get("/ui/select")
def ui_select() -> Response:
    ensure_cache_loaded()
    chars = CACHE["zone_nova"]["characters"]

    chars_json = json.dumps(chars, ensure_ascii=False)
    adv_json = json.dumps(ELEMENT_ADVANTAGE, ensure_ascii=False)

    refreshed = CACHE["zone_nova"]["last_refresh_iso"] or "N/A"
    source = CACHE["zone_nova"]["source"] or "N/A"
    cached_n = len(chars)

    html = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>__APP_TITLE__</title>
  <style>
    :root{
      --bg:#0b1020;
      --panel:rgba(255,255,255,.06);
      --border:rgba(255,255,255,.12);
      --muted:rgba(255,255,255,.65);
      --text:rgba(255,255,255,.92);
      --brand:#6ea8ff;
      --danger:#ff5d6c;
      --ok:#3ddc97;
      --shadow:0 10px 30px rgba(0,0,0,.35);
      --r:14px;
    }
    *{box-sizing:border-box;}
    body{
      margin:0;
      font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",Arial;
      background:
        radial-gradient(1200px 600px at 10% 0%, rgba(110,168,255,.18), transparent 55%),
        radial-gradient(800px 500px at 90% 10%, rgba(124,92,255,.16), transparent 60%),
        var(--bg);
      color:var(--text);
    }
    a{color:var(--brand);text-decoration:none;}
    a:hover{text-decoration:underline;}

    .topbar{
      position:sticky;top:0;z-index:20;
      backdrop-filter:blur(10px);
      background:linear-gradient(to bottom, rgba(11,16,32,.88), rgba(11,16,32,.70));
      border-bottom:1px solid var(--border);
    }
    .topbarInner{
      max-width:1280px;margin:0 auto;padding:14px 18px;
      display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    }
    .title{display:flex;align-items:center;gap:10px;margin-right:auto;}
    .title h1{font-size:18px;margin:0;}
    .pill{
      display:inline-flex;align-items:center;gap:8px;
      padding:6px 10px;border:1px solid var(--border);
      background:var(--panel);border-radius:999px;font-size:12px;color:var(--muted);
    }
    .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px rgba(61,220,151,.18);}
    .meta{font-size:12px;color:var(--muted);}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}

    .wrap{max-width:1280px;margin:0 auto;padding:16px 18px 26px;}
    .grid{display:grid;grid-template-columns:420px 1fr;gap:14px;align-items:start;}
    @media(max-width:980px){.grid{grid-template-columns:1fr;}}

    .card{
      background:var(--panel);
      border:1px solid var(--border);
      border-radius:var(--r);
      box-shadow:var(--shadow);
      overflow:hidden;
    }
    .cardHeader{
      padding:14px 14px 12px;border-bottom:1px solid var(--border);
      display:flex;align-items:center;justify-content:space-between;gap:12px;
    }
    .cardTitle{font-size:13px;font-weight:900;letter-spacing:.2px;}
    .cardBody{padding:14px;}

    .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;}
    .field{display:flex;flex-direction:column;gap:6px;}
    .label{font-size:12px;color:var(--muted);}

    select,input{
      width:100%;
      padding:10px 12px;border-radius:12px;
      border:1px solid var(--border);
      background:rgba(0,0,0,.25);
      color:var(--text);
      outline:none;
    }

    .btn{
      padding:10px 12px;border-radius:12px;
      border:1px solid var(--border);
      background:rgba(255,255,255,.06);
      color:var(--text);
      cursor:pointer;
      font-weight:900;
      white-space:nowrap;
    }
    .btn:hover{background:rgba(255,255,255,.10);}
    .btnPrimary{
      border:1px solid rgba(110,168,255,.45);
      background:linear-gradient(135deg, rgba(110,168,255,.22), rgba(124,92,255,.18));
    }
    .btnDanger{
      border:1px solid rgba(255,93,108,.55);
      background:rgba(255,93,108,.10);
      color:#ffd7db;
    }
    .btnGhost{background:transparent;}

    .hint{font-size:12px;color:var(--muted);line-height:1.55;}

    .rightTop{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-bottom:12px;}
    .stat{
      margin-left:auto;
      font-size:12px;color:var(--muted);
      border:1px solid var(--border);
      background:rgba(0,0,0,.18);
      padding:8px 10px;border-radius:999px;
    }
    .stat b{color:var(--text);}

    .gridWrap{
      border:1px solid var(--border);
      background:rgba(0,0,0,.18);
      border-radius:14px;
      padding:12px;
      height:calc(100vh - 250px);
      min-height:420px;
      overflow:auto;
    }

    .charGrid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;}
    @media(max-width:1100px){.charGrid{grid-template-columns:repeat(5,1fr);}}
    @media(max-width:980px){.charGrid{grid-template-columns:repeat(4,1fr);} .gridWrap{height:auto;}}
    @media(max-width:680px){.charGrid{grid-template-columns:repeat(3,1fr);}}
    @media(max-width:520px){.charGrid{grid-template-columns:repeat(2,1fr);}}

    .charCard{
      border:1px solid var(--border);
      background:rgba(0,0,0,.16);
      border-radius:14px;
      overflow:hidden;
      cursor:pointer;
      transition:transform .08s ease, background .12s ease, border-color .12s ease;
      position:relative;
    }
    .charCard:hover{transform:translateY(-1px);background:rgba(0,0,0,.24);border-color:rgba(110,168,255,.35);}
    .charCard.selected{border-color:rgba(110,168,255,.60);box-shadow:0 0 0 4px rgba(110,168,255,.12);}

    .thumb{
      width:100%;
      aspect-ratio:1/1;
      background:rgba(255,255,255,.06);
      overflow:hidden;
      display:flex;align-items:center;justify-content:center;
      color:rgba(255,255,255,.35);
      font-weight:900;
      letter-spacing:.2px;
    }
    .thumb img{width:100%;height:100%;object-fit:cover;display:block;}

    .check{
      position:absolute;top:8px;left:8px;
      width:22px;height:22px;border-radius:7px;
      border:1px solid rgba(255,255,255,.18);
      background:rgba(0,0,0,.40);
      display:flex;align-items:center;justify-content:center;
    }
    .check input{width:16px;height:16px;margin:0;accent-color:var(--brand);cursor:pointer;}

    .nameBar{
      padding:10px 10px;
      display:flex;flex-direction:column;gap:6px;
      border-top:1px solid rgba(255,255,255,.06);
    }
    .cname{font-weight:900;font-size:12px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    .chips{display:flex;gap:6px;flex-wrap:wrap;}
    .chip{
      font-size:11px;padding:3px 7px;border-radius:999px;
      border:1px solid rgba(255,255,255,.14);
      background:rgba(255,255,255,.05);
      color:rgba(255,255,255,.78);
      white-space:nowrap;
    }
    .chipElem{border-color:rgba(110,168,255,.28);color:rgba(110,168,255,.95);}
    .chipRole{border-color:rgba(61,220,151,.22);color:rgba(61,220,151,.92);}

    .resultBox{
      margin-top:12px;
      border:1px solid var(--border);
      background:rgba(0,0,0,.18);
      border-radius:14px;
      padding:12px;
    }

    .toast{
      position:fixed;right:16px;bottom:16px;z-index:50;
      background:rgba(0,0,0,.65);
      border:1px solid var(--border);
      color:var(--text);
      padding:10px 12px;border-radius:12px;
      box-shadow:var(--shadow);
      display:none;font-size:12px;
    }
  </style>
</head>
<body>

<div class="topbar">
  <div class="topbarInner">
    <div class="title">
      <h1>__APP_TITLE__</h1>
      <div class="pill"><span class="dot"></span> 준비됨</div>
    </div>
    <div class="meta">
      캐시 <b>__CACHED_N__</b> · 갱신 <span class="mono">__REFRESHED__</span> · 소스 <b>__SOURCE__</b>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <a class="pill" href="/">메타</a>
      <a class="pill" href="/refresh">새로고침</a>
      <a class="pill" href="/zones/zone-nova/characters">JSON</a>
    </div>
  </div>
</div>

<div class="wrap">
  <div class="grid">

    <!-- LEFT -->
    <div class="card">
      <div class="cardHeader">
        <div class="cardTitle">추천 옵션</div>
        <div class="pill mono">속성 상성: Fire→Wind · Wind→Ice · Ice→Holy · Holy→Chaos · Chaos→Fire</div>
      </div>
      <div class="cardBody">

        <div class="row">
          <div class="field" style="flex:1;min-width:140px;">
            <div class="label">모드</div>
            <select id="mode">
              <option value="pve">일반(PvE)</option>
              <option value="boss">보스</option>
              <option value="pvp">PvP</option>
            </select>
          </div>
          <div class="field" style="width:120px;">
            <div class="label">추천 개수</div>
            <select id="top_k">
              <option value="3">3</option>
              <option value="5" selected>5</option>
              <option value="10">10</option>
            </select>
          </div>
        </div>

        <div style="height:10px;"></div>

        <div class="row">
          <div class="field" style="flex:1;min-width:160px;">
            <div class="label">보스 약점 속성</div>
            <select id="boss_weakness">
              <option value="">(없음)</option>
              <option value="Fire">Fire</option>
              <option value="Ice">Ice</option>
              <option value="Wind">Wind</option>
              <option value="Holy">Holy</option>
              <option value="Chaos">Chaos</option>
            </select>
          </div>
          <div class="field" style="flex:1;min-width:160px;">
            <div class="label">상대(적) 속성</div>
            <select id="enemy_element">
              <option value="">(없음)</option>
              <option value="Fire">Fire</option>
              <option value="Ice">Ice</option>
              <option value="Wind">Wind</option>
              <option value="Holy">Holy</option>
              <option value="Chaos">Chaos</option>
            </select>
          </div>
        </div>

        <div style="height:12px;"></div>

        <div class="row">
          <button class="btn btnGhost" id="btnReq">선택 → 필수 포함</button>
          <button class="btn btnGhost" id="btnFix">선택 → 고정 포함</button>
          <button class="btn btnGhost" id="btnBan">선택 → 제외</button>
        </div>

        <div style="height:12px;"></div>

        <div class="field">
          <div class="label">필수 포함 (id/name, 쉼표)</div>
          <input id="required" placeholder="예) nina, freya" />
        </div>

        <div style="height:10px;"></div>

        <div class="field">
          <div class="label">고정 포함 (id/name, 쉼표)</div>
          <input id="fixed" placeholder="예) lavinia" />
        </div>

        <div style="height:10px;"></div>

        <div class="field">
          <div class="label">제외 (id/name, 쉼표)</div>
          <input id="banned" placeholder="예) apep" />
        </div>

        <div style="height:14px;"></div>

        <div class="row">
          <button class="btn btnPrimary" id="btnRun">추천 실행</button>
          <button class="btn btnDanger" id="btnClear">초기화</button>
        </div>

        <div style="height:10px;"></div>
        <div class="hint">
          오른쪽에서 <b>이미지로 체크</b> 후, “필수/고정/제외” 버튼으로 넣고 추천을 실행하세요.
        </div>
      </div>
    </div>

    <!-- RIGHT -->
    <div class="card">
      <div class="cardHeader">
        <div class="cardTitle">보유 캐릭터 선택 (이미지 체크)</div>
        <div class="stat" id="selectedStat">선택됨 <b>0</b>명</div>
      </div>
      <div class="cardBody">

        <div class="rightTop">
          <div class="field" style="width:160px;">
            <div class="label">속성</div>
            <select id="f_element">
              <option value="">전체</option>
              <option value="Fire">Fire</option>
              <option value="Ice">Ice</option>
              <option value="Wind">Wind</option>
              <option value="Holy">Holy</option>
              <option value="Chaos">Chaos</option>
              <option value="-">-</option>
            </select>
          </div>

          <div class="field" style="width:160px;">
            <div class="label">역할</div>
            <select id="f_role">
              <option value="">전체</option>
              <option value="tank">tank</option>
              <option value="healer">healer</option>
              <option value="dps">dps</option>
              <option value="buffer">buffer</option>
              <option value="debuffer">debuffer</option>
              <option value="-">-</option>
            </select>
          </div>

          <div class="field" style="width:140px;">
            <div class="label">등급</div>
            <select id="f_rarity">
              <option value="">전체</option>
              <option value="SSR">SSR</option>
              <option value="SR">SR</option>
              <option value="R">R</option>
              <option value="-">-</option>
            </select>
          </div>

          <div class="field" style="width:160px;">
            <div class="label">정렬</div>
            <select id="sort">
              <option value="name" selected>이름</option>
              <option value="rarity">등급</option>
              <option value="element">속성</option>
              <option value="role">역할</option>
            </select>
          </div>

          <div class="row" style="width:100%;">
            <button class="btn" id="btnAllOn">전체 선택</button>
            <button class="btn" id="btnAllOff">전체 해제</button>
            <button class="btn" id="btnVisOn">필터된 항목만 선택</button>
            <button class="btn" id="btnVisOff">필터된 항목만 해제</button>
          </div>
        </div>

        <div class="gridWrap">
          <div class="charGrid" id="charGrid"></div>
        </div>

        <div class="resultBox">
          <div class="row" style="justify-content:space-between;">
            <div class="cardTitle">결과</div>
            <button class="btn btnGhost" id="btnCopy">JSON 복사</button>
          </div>
          <div style="height:10px;"></div>
          <div id="out" class="hint">(아직 없음)</div>
        </div>

      </div>
    </div>

  </div>
</div>

<div class="toast" id="toast"></div>

<script type="application/json" id="chars-data">__CHARS_JSON__</script>
<script type="application/json" id="adv-data">__ADV_JSON__</script>

<script>
  function toast(msg){
    const t=document.getElementById('toast');
    t.textContent=msg;
    t.style.display='block';
    clearTimeout(window.__toastTimer);
    window.__toastTimer=setTimeout(()=>{t.style.display='none';},1600);
  }
  function getJson(id){
    const el=document.getElementById(id);
    try{ return JSON.parse(el.textContent); } catch(e){ return null; }
  }
  function normUrl(u){
    if(!u) return '';
    // mixed content 방지 (http -> https)
    if(u.startsWith('http://')) return 'https://' + u.slice(7);
    return u;
  }

  const CHARS = getJson('chars-data') || [];
  let LAST_JSON = null;

  function syncSelectedStat(){
    const n=document.querySelectorAll('.owned:checked').length;
    document.getElementById('selectedStat').innerHTML='선택됨 <b>'+n+'</b>명';
  }

  function applyFilter(){
    const fe=document.getElementById('f_element').value;
    const fr=(document.getElementById('f_role').value||'').toLowerCase();
    const frr=document.getElementById('f_rarity').value;

    document.querySelectorAll('.charCard').forEach(card=>{
      const el=card.dataset.element||'-';
      const role=(card.dataset.role||'-').toLowerCase();
      const rar=card.dataset.rarity||'-';

      let ok=true;
      if(fe) ok=(el===fe);
      if(ok && fr) ok=(role===fr);
      if(ok && frr) ok=(rar===frr);

      card.style.display = ok ? '' : 'none';
    });
  }

  function applySort(){
    const sortKey=document.getElementById('sort').value;
    const grid=document.getElementById('charGrid');
    const cards=Array.from(grid.children);

    const rarityOrder={SSR:1, SR:2, R:3, '-':9};
    const roleOrder={tank:1, healer:2, dps:3, debuffer:4, buffer:5, '-':9};
    const elemOrder={Fire:1, Ice:2, Wind:3, Holy:4, Chaos:5, '-':9};

    function key(card){
      if(sortKey==='rarity') return rarityOrder[card.dataset.rarity] ?? 9;
      if(sortKey==='role') return roleOrder[(card.dataset.role||'-').toLowerCase()] ?? 9;
      if(sortKey==='element') return elemOrder[card.dataset.element] ?? 9;
      return (card.dataset.name || '');
    }

    cards.sort((a,b)=>{
      const ka=key(a), kb=key(b);
      if(typeof ka==='number' && typeof kb==='number') return ka-kb;
      return String(ka).localeCompare(String(kb));
    });

    cards.forEach(c=>grid.appendChild(c));
    applyFilter();
  }

  function syncSelectedCards(){
    document.querySelectorAll('.charCard').forEach(card=>{
      const cb=card.querySelector('.owned');
      if(cb && cb.checked) card.classList.add('selected');
      else card.classList.remove('selected');
    });
  }

  function selectAll(flag){
    document.querySelectorAll('.owned').forEach(cb=>cb.checked=flag);
    syncSelectedCards(); syncSelectedStat();
  }
  function visibleCards(){
    return Array.from(document.querySelectorAll('.charCard')).filter(card=>card.style.display!=='none');
  }
  function selectVisible(flag){
    visibleCards().forEach(card=>{
      card.querySelector('.owned').checked=flag;
    });
    syncSelectedCards(); syncSelectedStat();
  }
  function checkedOwned(){
    return Array.from(document.querySelectorAll('.owned:checked')).map(x=>x.value);
  }
  function csv(v){
    v=(v||'').trim();
    if(!v) return [];
    return v.split(',').map(x=>x.trim()).filter(Boolean);
  }
  function uniq(arr){
    const s=new Set(); arr.forEach(x=>s.add(x));
    return Array.from(s);
  }

  function addCheckedTo(inputId){
    const ids=checkedOwned();
    if(!ids.length){ toast('먼저 보유 캐릭터를 체크하세요.'); return; }
    const now=uniq(csv(document.getElementById(inputId).value).concat(ids));
    document.getElementById(inputId).value=now.join(', ');
    toast('추가됨: '+ids.length+'명');
  }

  function clearAll(){
    document.querySelectorAll('.owned').forEach(b=>b.checked=false);
    document.getElementById('boss_weakness').value='';
    document.getElementById('enemy_element').value='';
    document.getElementById('f_element').value='';
    document.getElementById('f_role').value='';
    document.getElementById('f_rarity').value='';
    document.getElementById('required').value='';
    document.getElementById('fixed').value='';
    document.getElementById('banned').value='';
    syncSelectedCards(); syncSelectedStat();
    document.getElementById('out').innerHTML='(아직 없음)';
    LAST_JSON=null;
    toast('초기화 완료');
  }

  async function run(){
    const payload={
      mode: document.getElementById('mode').value,
      top_k: parseInt(document.getElementById('top_k').value,10),
      owned: checkedOwned(),
      required: csv(document.getElementById('required').value),
      focus: csv(document.getElementById('fixed').value),   // ✅ 서버는 focus 키를 쓰므로 fixed 입력을 focus로 전달
      banned: csv(document.getElementById('banned').value),
      boss_weakness: document.getElementById('boss_weakness').value || null,
      enemy_element: document.getElementById('enemy_element').value || null
    };

    if((payload.owned||[]).length < 4){
      toast('보유 캐릭터는 최소 4명 선택해야 합니다.');
      return;
    }

    document.getElementById('out').innerHTML='<div class="hint">계산 중...</div>';

    const res=await fetch('/recommend/v3',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)
    });

    const json=await res.json();
    LAST_JSON=json;

    document.getElementById('out').innerHTML =
      '<pre class="mono" style="white-space:pre-wrap;margin:0;">' +
      JSON.stringify(json, null, 2) + '</pre>';

    toast('추천 완료');
  }

  async function copyLast(){
    if(!LAST_JSON){ toast('복사할 결과가 없습니다.'); return; }
    try{
      await navigator.clipboard.writeText(JSON.stringify(LAST_JSON, null, 2));
      toast('JSON 복사 완료');
    }catch(e){
      toast('복사 실패(브라우저 권한 확인)');
    }
  }

  // ✅ 이미지 로드 fallback: (1) 데이터의 image (2) 로컬 /images/.../NAME.jpg/png (3) id 기반 jpg/png
  function buildImageCandidates(c){
    const base = '/images/games/zone-nova/characters/';
    const cand = [];

    const raw = normUrl(c.image || '');
    if(raw) cand.push(raw);

    const name = (c.name || '').trim();
    const id = (c.id || '').trim();

    function addName(n){
      if(!n) return;
      // 파일명이 공백 포함일 수 있으니 encode
      const enc = encodeURIComponent(n);
      cand.push(base + enc);
      // 확장자 없는 경우도 대비
      cand.push(base + enc + '.jpg');
      cand.push(base + enc + '.png');
    }

    addName(name);
    addName(id);
    addName(id ? (id[0].toUpperCase() + id.slice(1)) : '');

    // 중복 제거
    const seen = new Set();
    return cand.filter(u => {
      if(!u) return false;
      if(seen.has(u)) return false;
      seen.add(u);
      return true;
    });
  }

  function loadWithFallback(imgEl, candidates, placeholderEl, label){
    let idx = 0;

    function tryNext(){
      if(idx >= candidates.length){
        // 전부 실패 -> placeholder 텍스트
        if(placeholderEl){
          placeholderEl.textContent = 'NO IMAGE';
          placeholderEl.title = label || '';
        }
        imgEl.remove();
        return;
      }
      const u = candidates[idx++];
      imgEl.src = u;
      imgEl.dataset.srcTried = u;
    }

    imgEl.onerror = () => tryNext();
    tryNext();
  }

  function buildCard(c){
    const id=c.id || '';
    const name=c.name || id; // ✅ 캐릭터 이름은 영어 그대로
    const rarity=c.rarity || '-';
    const element=c.element || '-';
    const role=c.role || '-';

    const card=document.createElement('div');
    card.className='charCard';
    card.dataset.id=id;
    card.dataset.name=String(name).toLowerCase();
    card.dataset.rarity=rarity;
    card.dataset.element=element;
    card.dataset.role=role;

    const thumb=document.createElement('div');
    thumb.className='thumb';

    const img=document.createElement('img');
    const candidates = buildImageCandidates(c);
    // 후보가 있어도 실패할 수 있으니 placeholder는 thumb 자체를 사용
    loadWithFallback(img, candidates, thumb, name + ' (' + id + ')');
    thumb.appendChild(img);
    card.appendChild(thumb);

    const check=document.createElement('div');
    check.className='check';
    const cb=document.createElement('input');
    cb.type='checkbox';
    cb.className='owned';
    cb.value=id;
    check.appendChild(cb);
    card.appendChild(check);

    const nameBar=document.createElement('div');
    nameBar.className='nameBar';

    const cname=document.createElement('div');
    cname.className='cname';
    cname.title=name + ' (' + id + ')';
    cname.textContent=name;
    nameBar.appendChild(cname);

    const chips=document.createElement('div');
    chips.className='chips';
    chips.innerHTML =
      '<span class="chip">'+rarity+'</span>' +
      '<span class="chip chipElem">'+element+'</span>' +
      '<span class="chip chipRole">'+role+'</span>';
    nameBar.appendChild(chips);

    card.appendChild(nameBar);

    card.addEventListener('click',(ev)=>{
      if(ev.target && ev.target.tagName==='INPUT') return;
      cb.checked=!cb.checked;
      syncSelectedCards(); syncSelectedStat();
    });
    cb.addEventListener('change',()=>{
      syncSelectedCards(); syncSelectedStat();
    });

    return card;
  }

  function renderChars(){
    const grid=document.getElementById('charGrid');
    grid.innerHTML='';
    CHARS.forEach(c=>grid.appendChild(buildCard(c)));
  }

  document.addEventListener('DOMContentLoaded', ()=>{
    renderChars();
    syncSelectedStat();
    syncSelectedCards();

    document.getElementById('f_element').addEventListener('change', applyFilter);
    document.getElementById('f_role').addEventListener('change', applyFilter);
    document.getElementById('f_rarity').addEventListener('change', applyFilter);
    document.getElementById('sort').addEventListener('change', applySort);

    document.getElementById('btnAllOn').addEventListener('click', ()=>selectAll(true));
    document.getElementById('btnAllOff').addEventListener('click', ()=>selectAll(false));
    document.getElementById('btnVisOn').addEventListener('click', ()=>selectVisible(true));
    document.getElementById('btnVisOff').addEventListener('click', ()=>selectVisible(false));

    document.getElementById('btnReq').addEventListener('click', ()=>addCheckedTo('required'));
    document.getElementById('btnFix').addEventListener('click', ()=>addCheckedTo('fixed'));
    document.getElementById('btnBan').addEventListener('click', ()=>addCheckedTo('banned'));

    document.getElementById('btnRun').addEventListener('click', run);
    document.getElementById('btnClear').addEventListener('click', clearAll);
    document.getElementById('btnCopy').addEventListener('click', copyLast);

    applySort();
    applyFilter();
  });
</script>

</body>
</html>
"""

    html = html.replace("__APP_TITLE__", str(APP_TITLE))
    html = html.replace("__CACHED_N__", str(cached_n))
    html = html.replace("__REFRESHED__", str(refreshed))
    html = html.replace("__SOURCE__", str(source))
    html = html.replace("__CHARS_JSON__", chars_json)
    html = html.replace("__ADV_JSON__", adv_json)

    return Response(html, mimetype="text/html; charset=utf-8")

if __name__ == "__main__":
    refresh_zone_nova_cache()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    app.run(host="0.0.0.0", port=port, debug=True)
