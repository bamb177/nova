import os
import re
import json
from datetime import datetime, timezone

from flask import Flask, jsonify, request, Response, redirect


# =========================
# App / Config
# =========================
app = Flask(__name__)
APP_TITLE = os.getenv("APP_TITLE", "Nova")

# GitHub 이미지 소스 (Render에서 /images 404를 피하기 위해 GitHub에서 직접 로드)
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "boring877")
GITHUB_REPO = os.getenv("GITHUB_REPO", "gacha-wiki")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

JSDELIVR_BASE = f"https://cdn.jsdelivr.net/gh/{GITHUB_OWNER}/{GITHUB_REPO}@{GITHUB_BRANCH}/public/images/games/zone-nova/characters/"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/public/images/games/zone-nova/characters/"

# (선택) 로컬 리포 스캔용 - 데이터가 없을 때 최소 캐릭터 리스트 생성
DEFAULT_REPO_PATH = os.getenv("REPO_PATH", os.path.join(os.getcwd(), "gacha-wiki"))
DEFAULT_IMAGE_DIR = os.getenv(
    "IMAGE_DIR",
    os.path.join(DEFAULT_REPO_PATH, "public", "images", "games", "zone-nova", "characters")
)

# 속성 상성 (A -> B : A가 B에 유리)
ELEMENT_ADVANTAGE = {
    "Fire": "Wind",
    "Wind": "Ice",
    "Ice": "Holy",
    "Holy": "Chaos",
    "Chaos": "Fire",
}

RARITY_SCORE = {"SSR": 30, "SR": 18, "R": 10, "-": 0}
ROLE_NEED_ORDER = ["tank", "healer"]  # 최소 요구

CACHE = {
    "zone_nova": {
        "characters": [],
        "last_refresh_iso": None,
        "source": None,
        "cache_error": None,
    }
}


# =========================
# Helpers
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_json(obj) -> Response:
    return Response(json.dumps(obj, ensure_ascii=False, indent=2), mimetype="application/json; charset=utf-8")


def slug_id(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)  # 공백/따옴표 제거
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def normalize_char_names(chars: list[dict]) -> list[dict]:
    """
    캐릭터 이름 표기 흔들림 표준화 + 이미지 탐색용 aliases 부여 + 중복 제거
    - Jeanne D Arc / Joanof Arc 등은 Jeanne D Arc로 통일
    """
    out: list[dict] = []
    seen_key: set[str] = set()

    arc_variants = {
        "jeanne darc", "jeanne d arc", "jeanne d'arc",
        "joanofarc", "joanof arc", "joan of arc", "joanof  arc",
        "jeannedarc",
    }

    for c in chars:
        c = dict(c)
        name = (c.get("name") or "").replace("’", "'").strip()
        name = " ".join(name.split())
        cid = (c.get("id") or "").strip()

        aliases = set(c.get("aliases") or [])

        # Arc 계열 표준화
        name_key = slug_id(name)
        id_key = slug_id(cid)
        if name_key in arc_variants or id_key in arc_variants:
            # 표준명
            std_name = "Jeanne D Arc"
            std_id = "jeannedarc"

            # 기존 표기 모두 aliases에 보관
            if name:
                aliases.add(name)
            if cid:
                aliases.add(cid)

            aliases.update([
                "Jeanne D Arc",
                "Jeanne D arc",
                "Jeanne D'Arc",
                "Joanof Arc",
                "Joan of Arc",
                "JoanofArc",
                "JeanneDArc",
            ])

            c["name"] = std_name
            c["id"] = std_id
        else:
            c["name"] = name or cid
            c["id"] = cid or slug_id(name)

        c["aliases"] = sorted({a.strip() for a in aliases if isinstance(a, str) and a.strip()})

        # 중복 제거는 "표준 name" 기준
        key = (c["name"] or "").strip().lower()
        if not key:
            continue
        if key in seen_key:
            continue
        seen_key.add(key)

        out.append(c)

    return out


def scan_local_images(image_dir: str) -> list[dict]:
    """
    로컬 리포의 characters 폴더에서 파일명을 기반으로 최소 캐릭터 리스트 생성.
    (이미지는 GitHub에서 로드하지만, 데이터가 비었을 때 캐릭터 목록 확보용)
    """
    chars: list[dict] = []
    if not image_dir or not os.path.isdir(image_dir):
        return chars

    exts = (".png", ".jpg", ".jpeg", ".webp")
    for fn in os.listdir(image_dir):
        if not fn.lower().endswith(exts):
            continue
        stem = os.path.splitext(fn)[0]
        # 파일명 그대로 name로
        name = stem
        cid = slug_id(stem)
        # 최소 정보
        chars.append({
            "id": cid,
            "name": name,
            "rarity": "-",   # 추후 보강 가능
            "element": "-",
            "role": "-",
            "aliases": [name, cid],
            # image는 굳이 넣지 않아도 UI에서 GitHub base로 후보를 만듦
        })
    return chars


def ensure_cache_loaded(force: bool = False) -> None:
    if CACHE["zone_nova"]["characters"] and not force:
        return

    CACHE["zone_nova"]["cache_error"] = None

    try:
        # 1) 로컬 이미지 폴더 스캔(최소 캐릭터 확보)
        chars = scan_local_images(DEFAULT_IMAGE_DIR)

        # 2) normalize (Arc 표준화/중복 제거 포함)
        chars = normalize_char_names(chars)

        CACHE["zone_nova"]["characters"] = chars
        CACHE["zone_nova"]["last_refresh_iso"] = now_iso()
        CACHE["zone_nova"]["source"] = "local_image_scan(normalized)"

    except Exception as e:
        CACHE["zone_nova"]["characters"] = []
        CACHE["zone_nova"]["last_refresh_iso"] = now_iso()
        CACHE["zone_nova"]["source"] = "error"
        CACHE["zone_nova"]["cache_error"] = str(e)


def resolve_ids(input_list: list[str], chars: list[dict]) -> list[str]:
    """
    입력이 id 또는 name 일 수 있으므로 chars를 기준으로 id로 정규화.
    """
    if not input_list:
        return []
    by_id = {c["id"].lower(): c["id"] for c in chars if c.get("id")}
    by_name = { (c.get("name") or "").lower(): c["id"] for c in chars if c.get("id")}
    # aliases도 name 매핑에 포함
    for c in chars:
        cid = c.get("id")
        for a in c.get("aliases") or []:
            if isinstance(a, str) and a.strip():
                by_name[a.strip().lower()] = cid

    out = []
    for x in input_list:
        if not x:
            continue
        k = x.strip().lower()
        if k in by_id:
            out.append(by_id[k])
        elif k in by_name:
            out.append(by_name[k])
        else:
            # 알 수 없는 값은 slug로라도 넣어둠(사용자 입력 방어)
            out.append(slug_id(x))
    # 중복 제거(순서 유지)
    seen = set()
    uniq = []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def element_bonus(char_element: str, enemy_element: str | None, boss_weakness: str | None) -> int:
    bonus = 0
    ce = (char_element or "-")
    if boss_weakness and ce == boss_weakness:
        bonus += 25
    if enemy_element:
        # enemy를 이기는 요소를 찾는다: ELEMENT_ADVANTAGE[x] == enemy_element 인 x가 유리
        advantagers = [k for k, v in ELEMENT_ADVANTAGE.items() if v == enemy_element]
        if ce in advantagers:
            bonus += 20
        # 불리(내가 enemy에게 먹힘)
        if ELEMENT_ADVANTAGE.get(enemy_element) == ce:
            bonus -= 10
    return bonus


def recommend_party(payload: dict, chars: list[dict]) -> dict:
    """
    간단/안정형 추천(초대형 조합 전수 탐색 대신 휴리스틱)
    - 4인 고정
    - required는 반드시 포함
    - banned 제외
    - tank/healer 우선 확보
    - 나머지는 점수 상위로 채움
    """
    mode = payload.get("mode") or "pve"
    top_k = int(payload.get("top_k") or 5)

    owned_in = payload.get("owned") or []
    required_in = payload.get("required") or []
    focus_in = payload.get("focus") or []
    banned_in = payload.get("banned") or []

    enemy_element = payload.get("enemy_element") or None
    boss_weakness = payload.get("boss_weakness") or None

    owned = resolve_ids(owned_in, chars)
    required = resolve_ids(required_in, chars)
    focus = set(resolve_ids(focus_in, chars))
    banned = set(resolve_ids(banned_in, chars))

    by_id = {c["id"]: c for c in chars if c.get("id")}

    # owned에서만 후보
    pool = [by_id[i] for i in owned if i in by_id and i not in banned]

    issues = []
    if len(pool) < 4:
        issues.append("보유(Owned) 선택 인원이 4명 미만입니다.")
        return {"ok": False, "issues": issues, "best_party": None}

    # required가 pool에 있는지 확인
    for rid in required:
        if rid not in {c["id"] for c in pool}:
            issues.append(f"필수 포함 캐릭터({rid})가 보유 목록에 없습니다.")

    # 점수 계산
    def score(c: dict) -> int:
        s = 0
        s += RARITY_SCORE.get(c.get("rarity") or "-", 0)
        s += element_bonus(c.get("element") or "-", enemy_element, boss_weakness)

        role = (c.get("role") or "-").lower()
        # 모드별 약간 가중치
        if mode == "pvp":
            if role in ("tank", "healer"):
                s += 6
        elif mode == "boss":
            if role in ("debuffer", "buffer"):
                s += 6

        if c["id"] in focus:
            s += 18

        return s

    # required 먼저 담기
    party_ids: list[str] = []
    for rid in required:
        if rid in by_id and rid not in banned and rid in {c["id"] for c in pool}:
            if rid not in party_ids:
                party_ids.append(rid)

    # 역할 충족(탱/힐) 우선
    def pick_best(filter_role: str) -> str | None:
        best_id = None
        best_sc = -10**9
        for c in pool:
            cid = c["id"]
            if cid in party_ids:
                continue
            if (c.get("role") or "-").lower() != filter_role:
                continue
            sc = score(c)
            if sc > best_sc:
                best_sc = sc
                best_id = cid
        return best_id

    current_roles = {(by_id[i].get("role") or "-").lower() for i in party_ids if i in by_id}

    for needed in ROLE_NEED_ORDER:
        if needed in current_roles:
            continue
        picked = pick_best(needed)
        if picked:
            party_ids.append(picked)
            current_roles.add(needed)

    # 남은 자리: 점수 상위
    remain = [c for c in pool if c["id"] not in party_ids]
    remain.sort(key=lambda c: score(c), reverse=True)

    while len(party_ids) < 4 and remain:
        party_ids.append(remain.pop(0)["id"])

    if len(party_ids) < 4:
        issues.append("조건(필수/제외/역할) 때문에 4인 구성이 불가능합니다.")
        return {"ok": False, "issues": issues, "best_party": None}

    # 결과 구성
    party = []
    role_count = {"tank": 0, "healer": 0, "dps": 0, "buffer": 0, "debuffer": 0, "-": 0}
    for pid in party_ids[:4]:
        c = by_id.get(pid)
        if not c:
            continue
        role = (c.get("role") or "-").lower()
        role_count[role] = role_count.get(role, 0) + 1
        party.append({
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "rarity": c.get("rarity") or "-",
            "element": c.get("element") or "-",
            "role": c.get("role") or "-",
            "score": score(c),
        })

    # 안정성 코멘트
    if role_count.get("tank", 0) == 0:
        issues.append("탱커가 없습니다.")
    if role_count.get("healer", 0) == 0:
        issues.append("힐러가 없습니다.")

    return {
        "ok": True,
        "mode": mode,
        "input": {
            "owned": owned,
            "required": required,
            "focus": sorted(list(focus)),
            "banned": sorted(list(banned)),
            "enemy_element": enemy_element,
            "boss_weakness": boss_weakness,
            "top_k": top_k,
        },
        "best_party": {
            "party_size": 4,
            "members": party,
            "roles": role_count,
            "analysis": issues if issues else ["조건 충족(4인 구성)"],
        }
    }


# =========================
# Routes
# =========================
@app.get("/")
def home():
    # 루트 Not Found 방지
    return redirect("/ui/select")


@app.get("/refresh")
def refresh():
    ensure_cache_loaded(force=True)
    return redirect("/ui/select")


@app.get("/zones/zone-nova/characters")
def zone_nova_characters():
    ensure_cache_loaded()
    return jsonify({
        "game": "zone-nova",
        "count": len(CACHE["zone_nova"]["characters"]),
        "last_refresh": CACHE["zone_nova"]["last_refresh_iso"],
        "source": CACHE["zone_nova"]["source"],
        "cache_error": CACHE["zone_nova"]["cache_error"],
        "characters": CACHE["zone_nova"]["characters"],
    })


@app.post("/recommend/v3")
def recommend_v3():
    ensure_cache_loaded()
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        payload = {}

    result = recommend_party(payload, CACHE["zone_nova"]["characters"])
    return safe_json(result)


@app.get("/ui/select")
def ui_select() -> Response:
    ensure_cache_loaded()
    chars = CACHE["zone_nova"]["characters"]

    chars_json = json.dumps(chars, ensure_ascii=False)
    adv_json = json.dumps(ELEMENT_ADVANTAGE, ensure_ascii=False)

    refreshed = CACHE["zone_nova"]["last_refresh_iso"] or "N/A"
    source = CACHE["zone_nova"]["source"] or "N/A"
    cached_n = len(chars)

    # 이미지 베이스는 GitHub만 사용(로컬 서빙 제거)
    img_bases = json.dumps([JSDELIVR_BASE, RAW_BASE], ensure_ascii=False)

    html = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>__APP_TITLE__</title>
  <style>
    :root{
      --bg:#0b1020; --panel:rgba(255,255,255,.06); --border:rgba(255,255,255,.12);
      --muted:rgba(255,255,255,.65); --text:rgba(255,255,255,.92);
      --brand:#6ea8ff; --danger:#ff5d6c; --ok:#3ddc97;
      --shadow:0 10px 30px rgba(0,0,0,.35); --r:14px;
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
      <a class="pill" href="/">메인</a>
      <a class="pill" href="/refresh">새로고침</a>
      <a class="pill" href="/zones/zone-nova/characters">JSON</a>
      <span class="pill mono">IMG: __IMG_BASE_SHORT__</span>
    </div>
  </div>
</div>

<div class="wrap">
  <div class="grid">

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
        <div class="hint">오른쪽에서 <b>이미지로 체크</b> 후, “필수/고정/제외” 버튼으로 넣고 추천을 실행하세요.</div>

      </div>
    </div>

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
            <div class="cardTitle">결과(JSON)</div>
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
<script type="application/json" id="img-bases">__IMG_BASES__</script>

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

  const CHARS = getJson('chars-data') || [];
  const IMG_BASES = getJson('img-bases') || [];
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
    visibleCards().forEach(card=>{ card.querySelector('.owned').checked=flag; });
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
      focus: csv(document.getElementById('fixed').value),   // fixed -> focus로 전달
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
      '<pre class="mono" style="white-space:pre-wrap;margin:0;">'+
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

  // ✅ GitHub 이미지 후보 생성: base(여러개) + (name/id/aliases) + (png/jpg/jpeg) 조합
  function imageCandidates(c){
    const name = (c.name || '').trim();     // 캐릭터명(영어)
    const id = (c.id || '').trim();
    const aliases = Array.isArray(c.aliases) ? c.aliases : [];

    const names = [];
    if(name) names.push(name);
    if(id) names.push(id);
    if(id) names.push(id[0].toUpperCase() + id.slice(1));
    for(const a of aliases){
      if(a && typeof a === 'string'){
        const t = a.trim();
        if(t) names.push(t);
      }
    }

    const exts = ['.png', '.jpg', '.jpeg'];
    const cand = [];
    for(const base of IMG_BASES){
      for(const n of names){
        const enc = encodeURIComponent(n);
        for(const ext of exts){
          cand.push(base + enc + ext);
        }
      }
    }

    const seen = new Set();
    return cand.filter(u => (!seen.has(u) && seen.add(u)));
  }

  function loadWithFallback(imgEl, candidates, placeholderEl){
    let idx = 0;
    function tryNext(){
      if(idx >= candidates.length){
        if(placeholderEl) placeholderEl.textContent = 'NO IMAGE';
        imgEl.remove();
        return;
      }
      imgEl.src = candidates[idx++];
    }
    imgEl.onerror = () => tryNext();
    tryNext();
  }

  function buildCard(c){
    const id=c.id || '';
    const name=c.name || id; // ✅ 캐릭터 이름은 영어만 표시
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
    loadWithFallback(img, imageCandidates(c), thumb);
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
    html = html.replace("__IMG_BASES__", img_bases)
    html = html.replace("__IMG_BASE_SHORT__", f"{GITHUB_OWNER}/{GITHUB_REPO}@{GITHUB_BRANCH}")

    return Response(html, mimetype="text/html; charset=utf-8")


# =========================
# Local run
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "40000"))
    app.run(host="0.0.0.0", port=port, debug=True)