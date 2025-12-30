import os
import json
import re
from datetime import datetime, timezone
from flask import Flask, request, Response, redirect, jsonify

APP_TITLE = os.getenv("APP_TITLE", "Nova")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ✅ 레포에 커밋된 데이터만 사용
CHAR_JSON = os.path.join(BASE_DIR, "public", "data", "zone-nova", "characters.json")
ELEM_JSON = os.path.join(BASE_DIR, "public", "data", "zone-nova", "element_chart.json")
BOSS_JSON = os.path.join(BASE_DIR, "public", "data", "zone-nova", "bosses.json")

# ✅ 정적 파일은 public 폴더에서만 서빙 (이미지도 여기서만)
app = Flask(__name__, static_folder="public", static_url_path="")

RARITY_SCORE = {"SSR": 30, "SR": 18, "R": 10, "-": 0}

CACHE = {
    "chars": [],
    "bosses": [],
    "element_adv": {"Fire": "Wind", "Wind": "Ice", "Ice": "Holy", "Holy": "Chaos", "Chaos": "Fire"},
    "last_refresh": None,
    "source": {
        "characters": "public/data/zone-nova/characters.json",
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

def normalize_chars(chars: list[dict]) -> list[dict]:
    """
    - id/name 정리
    - Jeanne D Arc / Joanof Arc 정규화(jeannedarc)
    - 중복 제거
    - image는 /images/... 경로만 인정 (없으면 None)
    """
    out = []
    seen = set()

    for c in chars:
        c = dict(c)
        name = (c.get("name") or "").replace("’", "'").strip()
        name = " ".join(name.split())
        cid = (c.get("id") or "").strip()

        if not cid:
            cid = slug_id(name)

        if slug_id(name) in {"jeannedarc", "joanofarc"} or slug_id(cid) in {"jeannedarc", "joanofarc"}:
            c["id"] = "jeannedarc"
            c["name"] = "Jeanne D Arc"
        else:
            c["id"] = slug_id(cid)
            c["name"] = name or cid

        c["rarity"] = c.get("rarity") or "-"
        c["element"] = c.get("element") or "-"
        c["role"] = c.get("role") or "-"

        img = c.get("image")
        img_file = c.get("img_file")

        if not img and img_file:
            c["image"] = f"/images/games/zone-nova/characters/{img_file}"
        elif isinstance(img, str) and img.startswith("/images/"):
            c["image"] = img
        else:
            c["image"] = img if (isinstance(img, str) and img.startswith("/")) else None

        key = c["id"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)

    return out

def normalize_bosses(bosses: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for b in bosses or []:
        if not isinstance(b, dict):
            continue
        bid = slug_id(b.get("id") or b.get("name") or "")
        name = (b.get("name") or bid or "").strip()
        if not bid:
            continue
        if bid in seen:
            continue
        seen.add(bid)
        out.append({
            "id": bid,
            "name": name,
            "weakness": b.get("weakness") or None,
            "enemy_element": b.get("enemy_element") or None,
        })
    return out

def read_json_file(path: str):
    if not os.path.isfile(path):
        raise RuntimeError(f"필수 파일이 없습니다: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_all(force: bool = False) -> None:
    if CACHE["chars"] and CACHE["bosses"] and not force:
        return

    CACHE["error"] = None
    try:
        # characters
        cdata = read_json_file(CHAR_JSON)
        if isinstance(cdata, dict) and isinstance(cdata.get("characters"), list):
            chars = cdata["characters"]
        elif isinstance(cdata, list):
            chars = cdata
        else:
            raise RuntimeError("characters.json 포맷 오류: characters 배열이 필요합니다.")
        CACHE["chars"] = normalize_chars(chars)

        # element_chart
        edata = read_json_file(ELEM_JSON)
        adv = None
        if isinstance(edata, dict):
            adv = edata.get("adv")
        if not (isinstance(adv, dict) and adv):
            raise RuntimeError("element_chart.json 포맷 오류: { adv: {...} } 형태가 필요합니다.")
        # 최소 키 검증
        for k in ["Fire", "Wind", "Ice", "Holy", "Chaos"]:
            if k not in adv:
                raise RuntimeError(f"element_chart.json adv 누락: {k}")
        CACHE["element_adv"] = {str(k): str(v) for k, v in adv.items()}

        # bosses
        bdata = read_json_file(BOSS_JSON)
        bosses = None
        if isinstance(bdata, dict):
            bosses = bdata.get("bosses")
        if not isinstance(bosses, list):
            raise RuntimeError("bosses.json 포맷 오류: { bosses: [...] } 형태가 필요합니다.")
        CACHE["bosses"] = normalize_bosses(bosses)

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

def element_bonus(char_element: str, enemy_element: str | None, boss_weakness: str | None, adv_map: dict) -> int:
    bonus = 0
    ce = char_element or "-"

    if boss_weakness and ce == boss_weakness:
        bonus += 25

    if enemy_element:
        # enemy를 이기는 속성(adv_map[x] == enemy)
        advantagers = [k for k, v in adv_map.items() if v == enemy_element]
        if ce in advantagers:
            bonus += 20

        # enemy가 나를 이기면 감점(adv_map[enemy] == ce)
        if adv_map.get(enemy_element) == ce:
            bonus -= 10

    return bonus

def recommend_party(payload: dict, chars: list[dict], adv_map: dict) -> dict:
    mode = payload.get("mode") or "pve"
    owned = resolve_ids(payload.get("owned") or [], chars)
    required = resolve_ids(payload.get("required") or [], chars)
    focus = set(resolve_ids(payload.get("focus") or [], chars))
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
        s = RARITY_SCORE.get(c.get("rarity") or "-", 0)
        s += element_bonus(c.get("element") or "-", enemy_element, boss_weakness, adv_map)

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
            "name": c.get("name") or c["id"],  # 캐릭터명 영어 유지
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
        "source": CACHE["source"],
        "characters_cached": len(CACHE["chars"]),
        "bosses_cached": len(CACHE["bosses"]),
        "last_refresh": CACHE["last_refresh"],
        "error": CACHE["error"],
        "char_json": "public/data/zone-nova/characters.json",
        "boss_json": "public/data/zone-nova/bosses.json",
        "element_chart_json": "public/data/zone-nova/element_chart.json",
        "image_base": "/images/games/zone-nova/characters/",
        "element_adv": CACHE["element_adv"],
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

    chars_json = json.dumps(CACHE["chars"], ensure_ascii=False)
    bosses_json = json.dumps(CACHE["bosses"], ensure_ascii=False)
    adv_json = json.dumps(CACHE["element_adv"], ensure_ascii=False)

    html = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{APP_TITLE}</title>
<style>
body{{margin:0;font-family:system-ui,"Noto Sans KR","Malgun Gothic",sans-serif;background:#0b1020;color:#eaf0ff;}}
a{{color:#86b6ff;text-decoration:none}} a:hover{{text-decoration:underline}}
.top{{position:sticky;top:0;background:rgba(11,16,32,.92);backdrop-filter:blur(10px);border-bottom:1px solid rgba(255,255,255,.12);}}
.topIn{{max-width:1280px;margin:0 auto;padding:12px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;}}
.badge{{font-size:12px;color:rgba(255,255,255,.75);border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);padding:6px 10px;border-radius:999px;}}
.wrap{{max-width:1280px;margin:0 auto;padding:14px 16px 24px;}}
.grid{{display:grid;grid-template-columns:380px 1fr;gap:12px;align-items:start;}}
@media(max-width:980px){{.grid{{grid-template-columns:1fr;}}}}
.card{{border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);border-radius:14px;overflow:hidden;}}
.hd{{padding:12px 12px;border-bottom:1px solid rgba(255,255,255,.10);font-weight:800;font-size:13px;display:flex;justify-content:space-between;gap:10px;}}
.bd{{padding:12px;}}
.row{{display:flex;flex-wrap:wrap;gap:10px;align-items:end;}}
label{{font-size:12px;color:rgba(255,255,255,.72);display:block;margin-bottom:6px;}}
select,input{{width:100%;padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.25);color:#eaf0ff;outline:none;}}
.btn{{padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.08);color:#eaf0ff;font-weight:800;cursor:pointer;}}
.btn:hover{{background:rgba(255,255,255,.12);}}
.btnP{{border-color:rgba(134,182,255,.45);background:rgba(134,182,255,.18);}}
.btnD{{border-color:rgba(255,93,108,.55);background:rgba(255,93,108,.12);}}
.small{{font-size:12px;color:rgba(255,255,255,.70);line-height:1.5;}}
.gridWrap{{margin-top:10px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.18);border-radius:14px;padding:10px;min-height:420px;max-height:calc(100vh - 260px);overflow:auto;}}
.charGrid{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;}}
@media(max-width:1100px){{.charGrid{{grid-template-columns:repeat(5,1fr);}}}}
@media(max-width:980px){{.charGrid{{grid-template-columns:repeat(4,1fr);}} .gridWrap{{max-height:none;}}}}
@media(max-width:680px){{.charGrid{{grid-template-columns:repeat(3,1fr);}}}}
@media(max-width:520px){{.charGrid{{grid-template-columns:repeat(2,1fr);}}}}

.item{{border:1px solid rgba(255,255,255,.12);border-radius:14px;overflow:hidden;background:rgba(0,0,0,.16);position:relative;cursor:pointer;}}
.item.sel{{border-color:rgba(134,182,255,.6);box-shadow:0 0 0 3px rgba(134,182,255,.12);}}
.thumb{{width:100%;aspect-ratio:1/1;background:rgba(255,255,255,.06);display:flex;align-items:center;justify-content:center;color:rgba(255,255,255,.35);font-weight:900;}}
.thumb img{{width:100%;height:100%;object-fit:cover;display:block;}}
.ck{{position:absolute;top:8px;left:8px;width:22px;height:22px;border-radius:7px;border:1px solid rgba(255,255,255,.18);background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;}}
.ck input{{width:16px;height:16px;margin:0;accent-color:#86b6ff;}}
.badges{{position:absolute;bottom:8px;left:8px;right:8px;display:flex;gap:6px;flex-wrap:wrap;}}
.tag{{font-size:11px;padding:3px 7px;border-radius:999px;border:1px solid rgba(255,255,255,.16);background:rgba(0,0,0,.40);color:rgba(255,255,255,.86);}}
.name{{padding:10px 10px;font-weight:900;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-top:1px solid rgba(255,255,255,.06);}}
pre{{margin:0;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Consolas,monospace;font-size:12px;}}
</style></head>
<body>
<div class="top"><div class="topIn">
  <div style="font-weight:900;">{APP_TITLE}</div>
  <span class="badge">캐시 {len(CACHE["chars"])} · 보스 {len(CACHE["bosses"])} · 갱신 {CACHE["last_refresh"] or "N/A"}</span>
  <a class="badge" href="/refresh">새로고침</a>
  <a class="badge" href="/meta">메타</a>
  <a class="badge" href="/zones/zone-nova/characters">캐릭터 JSON</a>
  <a class="badge" href="/zones/zone-nova/bosses">보스 JSON</a>
</div></div>

<div class="wrap">
  <div class="grid">
    <div class="card">
      <div class="hd"><span>추천 옵션</span><span class="small">보스 선택 시 약점/속성 자동 반영</span></div>
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

          <div style="flex:1;min-width:220px;">
            <label>보스 선택</label>
            <select id="boss_pick">
              <option value="">(선택 안 함)</option>
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

        <div style="height:10px;" class="small">
          상성표: Fire→Wind→Ice→Holy→Chaos→Fire (adv 기반)
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

<script type="application/json" id="chars">{chars_json}</script>
<script type="application/json" id="bosses">{bosses_json}</script>
<script type="application/json" id="adv">{adv_json}</script>

<script>
const CHARS = JSON.parse(document.getElementById('chars').textContent || "[]");
const BOSSES = JSON.parse(document.getElementById('bosses').textContent || "[]");
const ADV = JSON.parse(document.getElementById('adv').textContent || "{{}}");

const E_ICON = {{ Fire:"🔥", Ice:"❄️", Wind:"🌪️", Holy:"✨", Chaos:"☯️", "-":"❔" }};
const R_ICON = {{ tank:"🛡️", healer:"💚", dps:"⚔️", buffer:"📣", debuffer:"🧪", "-":"❔" }};

function stat(){{
  const n = document.querySelectorAll('.owned:checked').length;
  document.getElementById('stat').textContent = '선택 ' + n;
  document.getElementById('selCnt').textContent = '선택 ' + n;
  document.querySelectorAll('.item').forEach(el=>{{
    const cb=el.querySelector('input.owned');
    if(cb && cb.checked) el.classList.add('sel'); else el.classList.remove('sel');
  }});
}}

function csv(v){{ v=(v||'').trim(); if(!v) return []; return v.split(',').map(x=>x.trim()).filter(Boolean); }}
function uniq(arr){{ const s=new Set(); const o=[]; for(const x of arr){{ if(x && !s.has(x)){{ s.add(x); o.push(x);}} }} return o; }}
function checked(){{ return Array.from(document.querySelectorAll('.owned:checked')).map(x=>x.value); }}
function addCheckedTo(id){{
  const ids = checked();
  if(!ids.length) return;
  const cur = csv(document.getElementById(id).value);
  document.getElementById(id).value = uniq(cur.concat(ids)).join(', ');
}}

function makeCard(c){{
  const el=document.createElement('div');
  el.className='item';
  el.dataset.name=(c.name||'').toLowerCase();

  const thumb=document.createElement('div');
  thumb.className='thumb';

  if(c.image){{
    const img=document.createElement('img');
    img.src=c.image;
    img.onerror=()=>{{ thumb.textContent='NO IMAGE'; img.remove(); }};
    thumb.appendChild(img);
  }} else {{
    thumb.textContent='NO IMAGE';
  }}

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
  nm.textContent=c.name || c.id;

  el.appendChild(thumb);
  el.appendChild(ck);
  el.appendChild(badges);
  el.appendChild(nm);

  el.addEventListener('click', (ev)=>{{
    if(ev.target && ev.target.tagName==='INPUT') return;
    cb.checked = !cb.checked;
    stat();
  }});
  cb.addEventListener('change', stat);

  return el;
}}

function render(list){{
  const grid=document.getElementById('grid');
  grid.innerHTML='';
  list.forEach(c=>grid.appendChild(makeCard(c)));
  stat();
}}

function applyFilter(){{
  const q=(document.getElementById('q').value||'').trim().toLowerCase();
  document.querySelectorAll('.item').forEach(el=>{{
    if(!q) {{ el.style.display=''; return; }}
    el.style.display = el.dataset.name.includes(q) ? '' : 'none';
  }});
}}

function fillBossSelect(){{
  const sel = document.getElementById('boss_pick');
  // 기존 옵션 유지(첫번째)
  for(const b of BOSSES){{
    const opt = document.createElement('option');
    opt.value = b.id;
    opt.textContent = b.name || b.id;
    sel.appendChild(opt);
  }}
}}

function onBossPick(){{
  const bid = document.getElementById('boss_pick').value;
  const b = BOSSES.find(x => x.id === bid);
  if(!b) return;

  // 보스 선택 -> 약점/적속성 자동 반영 (값이 있으면)
  if(b.weakness) document.getElementById('boss_weakness').value = b.weakness;
  if(b.enemy_element) document.getElementById('enemy_element').value = b.enemy_element;

  // 모드를 보스에 자동 전환(원하면 제거 가능)
  document.getElementById('mode').value = 'boss';
}}

async function run(){{
  const payload={{
    mode: document.getElementById('mode').value,
    owned: checked(),
    required: csv(document.getElementById('required').value),
    focus: csv(document.getElementById('fixed').value),
    banned: csv(document.getElementById('banned').value),
    boss_weakness: document.getElementById('boss_weakness').value || null,
    enemy_element: document.getElementById('enemy_element').value || null
  }};
  if(payload.owned.length < 4){{
    document.getElementById('out').innerHTML='<div class="small">보유 캐릭터는 최소 4명 체크해야 합니다.</div>';
    return;
  }}
  document.getElementById('out').innerHTML='<div class="small">계산 중...</div>';
  const res = await fetch('/recommend/v3',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  const json = await res.json();
  document.getElementById('out').innerHTML='<pre>'+JSON.stringify(json,null,2)+'</pre>';
}}

function clearAll(){{
  document.querySelectorAll('.owned').forEach(x=>x.checked=false);
  ['required','fixed','banned','boss_weakness','enemy_element','q','boss_pick'].forEach(id=>{{
    const el=document.getElementById(id);
    if(!el) return;
    if(el.tagName==='SELECT') el.value=''; else el.value='';
  }});
  document.getElementById('out').textContent='(아직 없음)';
  stat(); applyFilter();
}}

document.addEventListener('DOMContentLoaded', ()=>{{
  render(CHARS);
  fillBossSelect();

  document.getElementById('q').addEventListener('input', applyFilter);
  document.getElementById('boss_pick').addEventListener('change', onBossPick);

  document.getElementById('btnAllOn').onclick=()=>{{ document.querySelectorAll('.owned').forEach(x=>x.checked=true); stat(); }};
  document.getElementById('btnAllOff').onclick=()=>{{ document.querySelectorAll('.owned').forEach(x=>x.checked=false); stat(); }};

  const visibleItems=()=>Array.from(document.querySelectorAll('.item')).filter(el=>el.style.display!=='none');
  document.getElementById('btnVisOn').onclick=()=>{{ visibleItems().forEach(el=>el.querySelector('.owned').checked=true); stat(); }};
  document.getElementById('btnVisOff').onclick=()=>{{ visibleItems().forEach(el=>el.querySelector('.owned').checked=false); stat(); }};

  document.getElementById('btnReq').onclick=()=>addCheckedTo('required');
  document.getElementById('btnFix').onclick=()=>addCheckedTo('fixed');
  document.getElementById('btnBan').onclick=()=>addCheckedTo('banned');

  document.getElementById('btnRun').onclick=run;
  document.getElementById('btnClear').onclick=clearAll;
}});
</script>
</body></html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
