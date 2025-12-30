import os
import re
import json
from datetime import datetime, timezone
from flask import Flask, request, Response, redirect, jsonify

APP_TITLE = os.getenv("APP_TITLE", "Nova")

# (fallback) GitHub 이미지 소스
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "boring877")
GITHUB_REPO = os.getenv("GITHUB_REPO", "gacha-wiki")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

JSDELIVR_BASE = f"https://cdn.jsdelivr.net/gh/{GITHUB_OWNER}/{GITHUB_REPO}@{GITHUB_BRANCH}/public/images/games/zone-nova/characters/"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/public/images/games/zone-nova/characters/"

# ✅ 로컬 이미지 베이스(정적 서빙): /images/... -> public/images/...
LOCAL_BASE = "/images/games/zone-nova/characters/"

ELEMENT_ADV = {"Fire": "Wind", "Wind": "Ice", "Ice": "Holy", "Holy": "Chaos", "Chaos": "Fire"}
RARITY_SCORE = {"SSR": 30, "SR": 18, "R": 10, "-": 0}

CACHE = {
    "chars": [],
    "last_refresh": None,
    "source": None,
    "public_dir": None,
    "image_dir": None,
    "error": None,
}

META_PATH = os.path.join("public", "data", "zone-nova", "characters_meta.json")

def load_meta_map() -> dict:
    try:
        if os.path.isfile(META_PATH):
            with open(META_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}

def apply_meta(chars: list[dict], meta_map: dict) -> list[dict]:
    if not meta_map:
        return chars
    for c in chars:
        cid = (c.get("id") or "").strip().lower()
        m = meta_map.get(cid)
        if isinstance(m, dict):
            if m.get("rarity"):  c["rarity"]  = m["rarity"]
            if m.get("element"): c["element"] = m["element"]
            if m.get("role"):    c["role"]    = m["role"]
    return chars

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s

def find_existing_dir(candidates: list[str]) -> str | None:
    for p in candidates:
        if p and os.path.isdir(p):
            return p
    return None

def detect_public_dir() -> str | None:
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # 사용자가 강제 지정하면 최우선
    env = os.getenv("PUBLIC_DIR")

    candidates = [
        env,
        os.path.join(base_dir, "public"),
        os.path.join(base_dir, "..", "public"),
        os.path.join(os.getcwd(), "public"),
        "/opt/render/project/src/public",
        "/opt/render/project/src/app/public",
    ]
    return find_existing_dir(candidates)

def detect_image_dir(public_dir: str | None) -> str | None:
    if not public_dir:
        return None
    return find_existing_dir([
        os.getenv("IMAGE_DIR"),
        os.path.join(public_dir, "images", "games", "zone-nova", "characters"),
    ])

def normalize_chars(chars: list[dict]) -> list[dict]:
    """
    Jeanne D Arc / Joanof Arc 표기 정규화 + aliases + 중복 제거
    (img_file 같은 필드는 유지)
    """
    out, seen = [], set()

    for c in chars:
        c = dict(c)
        name = (c.get("name") or "").replace("’", "'").strip()
        name = " ".join(name.split())
        cid = (c.get("id") or "").strip()
        aliases = set(c.get("aliases") or [])

        nkey = slug_id(name)
        ikey = slug_id(cid)

        if nkey in {"jeannedarc", "joanofarc"} or ikey in {"jeannedarc", "joanofarc"}:
            if name: aliases.add(name)
            if cid: aliases.add(cid)
            aliases.update(["Jeanne D Arc", "Jeanne D'Arc", "Joanof Arc", "Joan of Arc", "JoanofArc", "JeanneDArc"])
            c["name"] = "Jeanne D Arc"
            c["id"] = "jeannedarc"
        else:
            c["name"] = name or cid
            c["id"] = cid or slug_id(c["name"])

        c["aliases"] = sorted({a.strip() for a in aliases if isinstance(a, str) and a.strip()})

        key = (c["name"] or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)

    return out

def scan_chars_from_local_images(image_dir: str) -> list[dict]:
    """
    ✅ 실제 파일명(확장자 포함)을 기록해서 UI가 .jpg/.png 헷갈리지 않도록 함
    """
    chars = []
    exts = (".png", ".jpg", ".jpeg", ".webp")
    for fn in os.listdir(image_dir):
        if not fn.lower().endswith(exts):
            continue
        stem, ext = os.path.splitext(fn)
        name = stem
        cid = slug_id(stem)

        chars.append({
            "id": cid,
            "name": name,
            "rarity": "-",
            "element": "-",
            "role": "-",
            "img_file": fn,              # ✅ 예: "Nina.jpg"
            "aliases": [name, cid],
        })
    return chars

def ensure_cache(force: bool = False) -> None:
    if CACHE["chars"] and not force:
        return

    CACHE["error"] = None
    public_dir = detect_public_dir()
    image_dir = detect_image_dir(public_dir)

    CACHE["public_dir"] = public_dir
    CACHE["image_dir"] = image_dir

    try:
        if not public_dir:
            raise RuntimeError("public 폴더를 찾지 못했습니다. (레포에 public이 포함되어 배포됐는지 확인)")
        if not image_dir:
            raise RuntimeError("public/images/games/zone-nova/characters 폴더를 찾지 못했습니다.")

        chars = scan_chars_from_local_images(image_dir)
        chars = normalize_chars(chars)
        meta_map = load_meta_map()
        chars = apply_meta(chars, meta_map)
    
        CACHE["chars"] = chars
        CACHE["last_refresh"] = now_iso()
        CACHE["source"] = "local(public) first + github fallback"

    except Exception as e:
        CACHE["chars"] = []
        CACHE["last_refresh"] = now_iso()
        CACHE["source"] = "error"
        CACHE["error"] = str(e)

def resolve_ids(input_list: list[str], chars: list[dict]) -> list[str]:
    if not input_list:
        return []
    by_id = {c["id"].lower(): c["id"] for c in chars if c.get("id")}
    by_name = {(c.get("name") or "").lower(): c["id"] for c in chars if c.get("id")}
    for c in chars:
        cid = c.get("id")
        for a in c.get("aliases") or []:
            if isinstance(a, str) and a.strip():
                by_name[a.strip().lower()] = cid

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

def element_bonus(char_element: str, enemy_element: str | None, boss_weakness: str | None) -> int:
    bonus = 0
    ce = char_element or "-"
    if boss_weakness and ce == boss_weakness:
        bonus += 25
    if enemy_element:
        advantagers = [k for k, v in ELEMENT_ADV.items() if v == enemy_element]
        if ce in advantagers:
            bonus += 20
        if ELEMENT_ADV.get(enemy_element) == ce:
            bonus -= 10
    return bonus

def recommend_party(payload: dict, chars: list[dict]) -> dict:
    mode = payload.get("mode") or "pve"
    owned = resolve_ids(payload.get("owned") or [], chars)
    required = resolve_ids(payload.get("required") or [], chars)
    focus = set(resolve_ids(payload.get("focus") or [], chars))
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
        s = RARITY_SCORE.get(c.get("rarity") or "-", 0)
        s += element_bonus(c.get("element") or "-", enemy_element, boss_weakness)
        role = (c.get("role") or "-").lower()
        if mode == "pvp" and role in ("tank", "healer"):
            s += 6
        if mode == "boss" and role in ("debuffer", "buffer"):
            s += 6
        if c["id"] in focus:
            s += 18
        return s

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
        members.append({
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "rarity": c.get("rarity") or "-",
            "element": c.get("element") or "-",
            "role": c.get("role") or "-",
            "score": score(c),
        })

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
        },
        "best_party": {
            "party_size": 4,
            "members": members,
            "analysis": issues if issues else ["조건 충족(4인 구성)"],
        }
    }

# ✅ Flask app 생성: public 디렉터리를 절대경로로 지정
_PUBLIC_DIR = detect_public_dir()
app = Flask(__name__, static_folder=_PUBLIC_DIR if _PUBLIC_DIR else "public", static_url_path="")

@app.get("/")
def home():
    return redirect("/ui/select")

@app.get("/refresh")
def refresh():
    ensure_cache(force=True)
    return redirect("/ui/select")

@app.get("/zones/zone-nova/characters")
def api_chars():
    ensure_cache()
    return jsonify({
        "count": len(CACHE["chars"]),
        "last_refresh": CACHE["last_refresh"],
        "source": CACHE["source"],
        "public_dir": CACHE["public_dir"],
        "image_dir": CACHE["image_dir"],
        "error": CACHE["error"],
        "characters": CACHE["chars"],
    })

@app.post("/recommend/v3")
def api_recommend():
    ensure_cache()
    payload = request.get_json(force=True) or {}
    return Response(
        json.dumps(recommend_party(payload, CACHE["chars"]), ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8"
    )

@app.get("/ui/select")
def ui_select() -> Response:
    ensure_cache()

    chars_json = json.dumps(CACHE["chars"], ensure_ascii=False)
    bases_json = json.dumps([LOCAL_BASE, JSDELIVR_BASE, RAW_BASE], ensure_ascii=False)

    template = r"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__TITLE__</title>
<style>
body{margin:0;font-family:system-ui,"Noto Sans KR","Malgun Gothic",sans-serif;background:#0b1020;color:#eaf0ff;}
a{color:#86b6ff;text-decoration:none} a:hover{text-decoration:underline}
.top{position:sticky;top:0;background:rgba(11,16,32,.92);backdrop-filter:blur(10px);border-bottom:1px solid rgba(255,255,255,.12);}
.topIn{max-width:1280px;margin:0 auto;padding:12px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.badge{font-size:12px;color:rgba(255,255,255,.75);border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);padding:6px 10px;border-radius:999px;}
.wrap{max-width:1280px;margin:0 auto;padding:14px 16px 24px;}
.grid{display:grid;grid-template-columns:380px 1fr;gap:12px;align-items:start;}
@media(max-width:980px){.grid{grid-template-columns:1fr;}}
.card{border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);border-radius:14px;overflow:hidden;}
.hd{padding:12px 12px;border-bottom:1px solid rgba(255,255,255,.10);font-weight:800;font-size:13px;display:flex;justify-content:space-between;gap:10px;}
.bd{padding:12px;}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:end;}
label{font-size:12px;color:rgba(255,255,255,.72);display:block;margin-bottom:6px;}
select,input{width:100%;padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.25);color:#eaf0ff;outline:none;}
.btn{padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.08);color:#eaf0ff;font-weight:800;cursor:pointer;}
.btn:hover{background:rgba(255,255,255,.12);}
.btnP{border-color:rgba(134,182,255,.45);background:rgba(134,182,255,.18);}
.btnD{border-color:rgba(255,93,108,.55);background:rgba(255,93,108,.12);}
.small{font-size:12px;color:rgba(255,255,255,.70);line-height:1.5;}
.gridWrap{margin-top:10px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.18);border-radius:14px;padding:10px;min-height:420px;max-height:calc(100vh - 260px);overflow:auto;}
.charGrid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;}
@media(max-width:1100px){.charGrid{grid-template-columns:repeat(5,1fr);}}
@media(max-width:980px){.charGrid{grid-template-columns:repeat(4,1fr);} .gridWrap{max-height:none;}}
@media(max-width:680px){.charGrid{grid-template-columns:repeat(3,1fr);}}
@media(max-width:520px){.charGrid{grid-template-columns:repeat(2,1fr);}}

.item{border:1px solid rgba(255,255,255,.12);border-radius:14px;overflow:hidden;background:rgba(0,0,0,.16);position:relative;cursor:pointer;}
.item.sel{border-color:rgba(134,182,255,.6);box-shadow:0 0 0 3px rgba(134,182,255,.12);}
.thumb{width:100%;aspect-ratio:1/1;background:rgba(255,255,255,.06);display:flex;align-items:center;justify-content:center;color:rgba(255,255,255,.35);font-weight:900;}
.thumb img{width:100%;height:100%;object-fit:cover;display:block;}
.ck{position:absolute;top:8px;left:8px;width:22px;height:22px;border-radius:7px;border:1px solid rgba(255,255,255,.18);background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;}
.ck input{width:16px;height:16px;margin:0;accent-color:#86b6ff;}
.badges{position:absolute;bottom:8px;left:8px;right:8px;display:flex;gap:6px;flex-wrap:wrap;}
.tag{font-size:11px;padding:3px 7px;border-radius:999px;border:1px solid rgba(255,255,255,.16);background:rgba(0,0,0,.40);color:rgba(255,255,255,.86);}
.name{padding:10px 10px;font-weight:900;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-top:1px solid rgba(255,255,255,.06);}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Consolas,monospace;font-size:12px;}
</style></head>
<body>
<div class="top"><div class="topIn">
  <div style="font-weight:900;">__TITLE__</div>
  <span class="badge">캐시 __COUNT__ · 갱신 __REFRESH__</span>
  <span class="badge">로컬: /images/... (public)</span>
  <a class="badge" href="/refresh">새로고침</a>
  <a class="badge" href="/zones/zone-nova/characters">JSON</a>
</div></div>

<div class="wrap">
  <div class="grid">
    <div class="card">
      <div class="hd"><span>추천 옵션</span><span class="small">캐릭터 이름만 영어</span></div>
      <div class="bd">
        <div class="row">
          <div style="flex:1;min-width:140px;">
            <label>모드</label>
            <select id="mode">
              <option value="pve">일반(PvE)</option>
              <option value="boss">보스</option>
              <option value="pvp">PvP</option>
            </select>
          </div>
          <div style="flex:1;min-width:160px;">
            <label>보스 약점 속성</label>
            <select id="boss_weakness">
              <option value="">(없음)</option><option>Fire</option><option>Ice</option><option>Wind</option><option>Holy</option><option>Chaos</option>
            </select>
          </div>
          <div style="flex:1;min-width:160px;">
            <label>상대(적) 속성</label>
            <select id="enemy_element">
              <option value="">(없음)</option><option>Fire</option><option>Ice</option><option>Wind</option><option>Holy</option><option>Chaos</option>
            </select>
          </div>
        </div>

        <div style="height:12px;"></div>
        <div class="row">
          <button class="btn" id="btnReq">선택 → 필수</button>
          <button class="btn" id="btnFix">선택 → 고정</button>
          <button class="btn" id="btnBan">선택 → 제외</button>
        </div>

        <div style="height:12px;"></div>
        <div><label>필수 포함 (쉼표)</label><input id="required" placeholder="예) nina, freya"/></div>
        <div style="height:10px;"></div>
        <div><label>고정 포함 (쉼표)</label><input id="fixed" placeholder="예) lavinia"/></div>
        <div style="height:10px;"></div>
        <div><label>제외 (쉼표)</label><input id="banned" placeholder="예) apep"/></div>

        <div style="height:12px;"></div>
        <div class="row">
          <button class="btn btnP" id="btnRun">추천 실행</button>
          <button class="btn btnD" id="btnClear">초기화</button>
        </div>

        <div style="height:12px;"></div>
        <div class="card" style="background:rgba(0,0,0,.18);">
          <div class="hd" style="border-bottom:1px solid rgba(255,255,255,.08);">
            <span>결과(JSON)</span><span id="selCnt" class="small">선택 0</span>
          </div>
          <div class="bd"><div id="out" class="small">(아직 없음)</div></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="hd"><span>보유 캐릭터 선택(이미지 체크)</span><span class="small" id="stat">선택 0</span></div>
      <div class="bd">
        <div class="row">
          <button class="btn" id="btnAllOn">전체 선택</button>
          <button class="btn" id="btnAllOff">전체 해제</button>
          <button class="btn" id="btnVisOn">보이는 것만 선택</button>
          <button class="btn" id="btnVisOff">보이는 것만 해제</button>
        </div>

        <div style="height:10px;"></div>
        <div class="row">
          <div style="flex:1;min-width:150px;">
            <label>이름 필터(영어)</label>
            <input id="q" placeholder="예) nina"/>
          </div>
        </div>

        <div class="gridWrap">
          <div class="charGrid" id="grid"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script type="application/json" id="chars">__CHARS__</script>
<script type="application/json" id="bases">__BASES__</script>
<script>
const CHARS = JSON.parse(document.getElementById('chars').textContent || "[]");
const BASES = JSON.parse(document.getElementById('bases').textContent || "[]");

// (b) 아이콘 표시 (현재 데이터 element/role/rarity가 "-"라면 ❔만 보입니다)
const E_ICON = { Fire:"🔥", Ice:"❄️", Wind:"🌪️", Holy:"✨", Chaos:"☯️", "-":"❔" };
const R_ICON = { tank:"🛡️", healer:"💚", dps:"⚔️", buffer:"📣", debuffer:"🧪", "-":"❔" };

function stat(){
  const n = document.querySelectorAll('.owned:checked').length;
  document.getElementById('stat').textContent = '선택 ' + n;
  document.getElementById('selCnt').textContent = '선택 ' + n;
  document.querySelectorAll('.item').forEach(el=>{
    const cb=el.querySelector('input.owned');
    if(cb && cb.checked) el.classList.add('sel'); else el.classList.remove('sel');
  });
}

function csv(v){ v=(v||'').trim(); if(!v) return []; return v.split(',').map(x=>x.trim()).filter(Boolean); }
function uniq(arr){ const s=new Set(); const o=[]; for(const x of arr){ if(x && !s.has(x)){ s.add(x); o.push(x);} } return o; }
function checked(){ return Array.from(document.querySelectorAll('.owned:checked')).map(x=>x.value); }
function addCheckedTo(id){
  const ids = checked();
  if(!ids.length) return;
  const cur = csv(document.getElementById(id).value);
  document.getElementById(id).value = uniq(cur.concat(ids)).join(', ');
}

// ✅ 핵심: 로컬에서 "실제 파일명(img_file)"로 먼저 요청 (확장자 .jpg 문제 해결)
function imageCandidates(c){
  const out = [];
  const file = c.img_file ? String(c.img_file).trim() : "";

  // 1) 로컬 우선: /images/.../<실제파일명>
  if(file){
    out.push(LOCAL_BASE + encodeURIComponent(file));
  }

  // 2) jsDelivr/raw fallback: base + <실제파일명>
  if(file){
    for(const base of BASES){
      if(base === LOCAL_BASE) continue;
      out.push(base + encodeURIComponent(file));
    }
  }

  // 3) 최후: name/id 기반 확장자 시도
  const names = [];
  if(c.name) names.push(String(c.name).trim());
  if(c.id) names.push(String(c.id).trim());
  if(Array.isArray(c.aliases)) for(const a of c.aliases){ if(a) names.push(String(a).trim()); }
  const exts=['.jpg','.png','.jpeg'];
  for(const base of BASES){
    for(const n of names){
      const enc = encodeURIComponent(n);
      for(const ext of exts) out.push(base + enc + ext);
    }
  }

  const seen = new Set();
  return out.filter(u => (!seen.has(u) && seen.add(u)));
}

function loadWithFallback(img, cand, placeholder){
  let i=0;
  const next=()=>{
    if(i>=cand.length){
      placeholder.textContent='NO IMAGE';
      img.remove();
      return;
    }
    img.src=cand[i++];
  };
  img.onerror=next;
  next();
}

function makeCard(c){
  const el=document.createElement('div');
  el.className='item';
  el.dataset.name=(c.name||'').toLowerCase();

  const thumb=document.createElement('div');
  thumb.className='thumb';

  const img=document.createElement('img');
  loadWithFallback(img, imageCandidates(c), thumb);
  thumb.appendChild(img);

  const ck=document.createElement('div');
  ck.className='ck';
  const cb=document.createElement('input');
  cb.type='checkbox';
  cb.className='owned';
  cb.value=c.id;
  ck.appendChild(cb);

  const badges=document.createElement('div');
  badges.className='badges';
  const elem = (c.element || "-");
  const role = String(c.role || "-").toLowerCase();
  const rar = (c.rarity || "-");
  badges.innerHTML =
    '<span class="tag">' + (E_ICON[elem] || "❔") + ' ' + elem + '</span>' +
    '<span class="tag">' + (R_ICON[role] || "❔") + ' ' + role + '</span>' +
    '<span class="tag">🏷️ ' + rar + '</span>';

  const nm=document.createElement('div');
  nm.className='name';
  nm.textContent=c.name || c.id; // 캐릭터명은 영어 유지

  el.appendChild(thumb);
  el.appendChild(ck);
  el.appendChild(badges);
  el.appendChild(nm);

  el.addEventListener('click', (ev)=>{
    if(ev.target && ev.target.tagName==='INPUT') return;
    cb.checked = !cb.checked;
    stat();
  });
  cb.addEventListener('change', stat);

  return el;
}

function render(list){
  const grid=document.getElementById('grid');
  grid.innerHTML='';
  list.forEach(c=>grid.appendChild(makeCard(c)));
  stat();
}

function applyFilter(){
  const q=(document.getElementById('q').value||'').trim().toLowerCase();
  document.querySelectorAll('.item').forEach(el=>{
    if(!q) { el.style.display=''; return; }
    el.style.display = el.dataset.name.includes(q) ? '' : 'none';
  });
}

async function run(){
  const payload={
    mode: document.getElementById('mode').value,
    owned: checked(),
    required: csv(document.getElementById('required').value),
    focus: csv(document.getElementById('fixed').value),
    banned: csv(document.getElementById('banned').value),
    boss_weakness: document.getElementById('boss_weakness').value || null,
    enemy_element: document.getElementById('enemy_element').value || null
  };
  if(payload.owned.length < 4){
    document.getElementById('out').innerHTML='<div class="small">보유 캐릭터는 최소 4명 체크해야 합니다.</div>';
    return;
  }
  document.getElementById('out').innerHTML='<div class="small">계산 중...</div>';
  const res = await fetch('/recommend/v3',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const json = await res.json();
  document.getElementById('out').innerHTML='<pre>'+JSON.stringify(json,null,2)+'</pre>';
}

function clearAll(){
  document.querySelectorAll('.owned').forEach(x=>x.checked=false);
  ['required','fixed','banned','boss_weakness','enemy_element','q'].forEach(id=>{
    const el=document.getElementById(id);
    if(!el) return;
    if(el.tagName==='SELECT') el.value=''; else el.value='';
  });
  document.getElementById('out').textContent='(아직 없음)';
  stat(); applyFilter();
}

document.addEventListener('DOMContentLoaded', ()=>{
  render(CHARS);
  document.getElementById('q').addEventListener('input', applyFilter);

  document.getElementById('btnAllOn').onclick=()=>{ document.querySelectorAll('.owned').forEach(x=>x.checked=true); stat(); };
  document.getElementById('btnAllOff').onclick=()=>{ document.querySelectorAll('.owned').forEach(x=>x.checked=false); stat(); };

  const visibleItems=()=>Array.from(document.querySelectorAll('.item')).filter(el=>el.style.display!=='none');
  document.getElementById('btnVisOn').onclick=()=>{ visibleItems().forEach(el=>el.querySelector('.owned').checked=true); stat(); };
  document.getElementById('btnVisOff').onclick=()=>{ visibleItems().forEach(el=>el.querySelector('.owned').checked=false); stat(); };

  document.getElementById('btnReq').onclick=()=>addCheckedTo('required');
  document.getElementById('btnFix').onclick=()=>addCheckedTo('fixed');
  document.getElementById('btnBan').onclick=()=>addCheckedTo('banned');

  document.getElementById('btnRun').onclick=run;
  document.getElementById('btnClear').onclick=clearAll;
});
</script>
<script>
  // LOCAL_BASE를 JS에서 사용
  const LOCAL_BASE = "__LOCAL_BASE__";
</script>
</body></html>
"""

    html = (template
        .replace("__TITLE__", APP_TITLE)
        .replace("__COUNT__", str(len(CACHE["chars"])))
        .replace("__REFRESH__", CACHE["last_refresh"] or "N/A")
        .replace("__CHARS__", chars_json)
        .replace("__BASES__", bases_json)
        .replace("__LOCAL_BASE__", LOCAL_BASE)
    )

    return Response(html, mimetype="text/html; charset=utf-8")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
