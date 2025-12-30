# main.py
import os
import re
import json
import time
import itertools
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

import requests
from flask import Flask, request, Response, redirect, url_for, send_from_directory
from flask import render_template_string

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except Exception:
    BS4_AVAILABLE = False


###############################################################################
# 기본 설정
###############################################################################
KST = timezone(timedelta(hours=9))

APP_TITLE = "Nova"

# Render 기본 작업 디렉토리: /opt/render/project/src
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# 정적 리소스(이미지) 폴더
# 현재 Render에서 확인한 경로와 동일하게 맞춤:
# /opt/render/project/src/public/images/games/zone-nova/characters
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
ZN_IMAGE_DIR = os.environ.get(
    "ZN_IMAGE_DIR",
    os.path.join(PUBLIC_DIR, "images", "games", "zone-nova", "characters")
)

# gachawiki base
ZN_GUIDE_BASE = "https://gachawiki.info/guides/zone-nova/"
ZN_CHAR_LIST_URL = ZN_GUIDE_BASE + "characters/"

# 네트워크 환경(회사망 등)에서 SSL 체인 문제 발생할 수 있어 보정 옵션 제공
#  - 기본: True (정상 검증)
#  - 실패 시: 자동으로 verify=False 재시도
FORCE_INSECURE_SSL = os.environ.get("FORCE_INSECURE_SSL", "0") == "1"

# “원격 스크랩 강제 금지” 옵션(회사망/차단 환경에서 local-only로 운영 가능)
FORCE_LOCAL_ONLY = os.environ.get("FORCE_LOCAL_ONLY", "0") == "1"

# JSON 한글 깨짐(유니코드 이스케이프) 방지
def json_response(payload: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        status=status,
        mimetype="application/json; charset=utf-8"
    )

def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


###############################################################################
# 속성 상성표 / 약점 가중치
###############################################################################
# NOTE:
# Zone Nova의 “정확한” 속성 상성이 다를 수 있습니다.
# 일단 기본 상성표를 넣어두고(표시/가중치 반영),
# 실제 규칙 확인되면 여기만 수정하면 됩니다.
#
# 예: attacker가 defender에게 강함
ELEMENT_ADVANTAGE = {
    "Fire":  ["Wind"],
    "Wind":  ["Ice"],
    "Ice":   ["Holy"],
    "Holy":  ["Chaos"],
    "Chaos": ["Fire"],
}

ALL_ELEMENTS = ["Fire", "Ice", "Wind", "Holy", "Chaos"]

# 약점/상성 가중치 (UI에서 선택한 값 기반으로 점수에 반영)
WEIGHT_MATCH_WEAKNESS = 1.50     # Boss Weakness Element와 동일한 속성 캐릭 보너스
WEIGHT_ADVANTAGE_OVER_ENEMY = 1.00  # Enemy Element 기준 “상성 우위” 보너스


###############################################################################
# 역할/클래스 매핑 (gachawiki에서 Class/Role이 다양한 형태로 들어올 수 있어 보정)
###############################################################################
def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s

def infer_role(char: Dict[str, Any]) -> str:
    """
    role/cclass 등을 종합해 내부 역할을 단순화:
    - Tank / Healer / Support / Debuffer / DPS
    """
    role = normalize_text(char.get("role", "")).lower()
    cclass = normalize_text(char.get("class", "")).lower()

    # role 우선
    if "tank" in role:
        return "Tank"
    if "healer" in role:
        return "Healer"
    if "support" in role or "buffer" in role:
        return "Support"
    if "debuff" in role or "interference" in role:
        return "Debuffer"
    if "dps" in role:
        return "DPS"

    # class 보정
    if "guardian" in cclass:
        return "Tank"
    if "healer" in cclass:
        return "Healer"
    if "buffer" in cclass or "support" in cclass:
        return "Support"
    if "debuff" in cclass or "interference" in cclass:
        return "Debuffer"

    # 나머지는 DPS로 처리
    return "DPS"


###############################################################################
# 캐릭 데이터 캐시
###############################################################################
CACHE: Dict[str, Any] = {
    "last_refresh": None,
    "characters": [],         # List[Dict]
    "source": None,           # images_only / images+remote / remote_only
    "remote_scrape": False,
    "remote_count": 0,
    "remote_error": None,
    "cache_error": None,
}


def scan_local_images(image_dir: str) -> List[Dict[str, Any]]:
    """
    로컬 이미지 파일명 기반으로 캐릭 목록 생성.
    예: Nina.jpg -> id=nina, name=Nina
    """
    if not os.path.isdir(image_dir):
        return []

    chars = []
    for fn in sorted(os.listdir(image_dir)):
        if not fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        base = os.path.splitext(fn)[0]
        cid = slugify(base)
        # name은 원본 베이스를 살리되, 너무 이상하면 id를 사용
        name = base.strip() if base.strip() else cid
        chars.append({
            "id": cid,
            "name": name,
            "image": f"/public/images/games/zone-nova/characters/{fn}",
            "element": None,
            "class": None,
            "role": None,
            "rarity": None,
            "source": "images",
        })
    return chars


def _requests_get(url: str, timeout: int = 20) -> requests.Response:
    """
    SSL verify 실패(회사망 SSL 프록시 등) 케이스를 고려해:
    1) 기본 verify=True
    2) 실패 시 verify=False 재시도 (로그/표시)
    """
    if FORCE_INSECURE_SSL:
        return requests.get(url, timeout=timeout, verify=False)

    try:
        return requests.get(url, timeout=timeout)
    except requests.exceptions.SSLError:
        # 재시도
        return requests.get(url, timeout=timeout, verify=False)


def scrape_remote_characters() -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    """
    gachawiki Zone Nova 캐릭 페이지를 순회하면서
    id/name/element/class/role/rarity 등을 수집.
    """
    if not BS4_AVAILABLE:
        return [], 0, "bs4_not_available"

    try:
        r = _requests_get(ZN_CHAR_LIST_URL, timeout=25)
        if r.status_code != 200:
            return [], 0, f"status_{r.status_code}"
        soup = BeautifulSoup(r.text, "html.parser")

        # 캐릭 링크 수집: /guides/zone-nova/characters/<slug>/
        links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if "/guides/zone-nova/characters/" in href:
                # list 페이지 내 중복/상위 링크가 섞일 수 있어 필터링
                # 예: .../characters/athena/ 형태만
                m = re.search(r"/guides/zone-nova/characters/([^/]+)/", href)
                if m:
                    links.append((m.group(1), href))

        # 중복 제거
        uniq = {}
        for slug, href in links:
            if slug not in uniq:
                # 절대 URL로 정리
                if href.startswith("http"):
                    uniq[slug] = href
                else:
                    uniq[slug] = "https://gachawiki.info" + href

        # 각 캐릭 페이지 파싱
        chars = []
        for slug, url in sorted(uniq.items()):
            try:
                cr = _requests_get(url, timeout=25)
                if cr.status_code != 200:
                    continue
                cs = BeautifulSoup(cr.text, "html.parser")

                # 페이지 내 “Character Overview” 영역에서 Element/Class/Role/Rarity 텍스트를 찾는 방식
                text = cs.get_text("\n", strip=True)

                # name 추출: h1 또는 #  Name 구조가 섞일 수 있어 우선순위로 시도
                name = None
                h1 = cs.find(["h1", "h2"])
                if h1:
                    name = normalize_text(h1.get_text())
                if not name:
                    name = slug.replace("-", " ").title()

                # Element / Class / Role / Rarity 찾기
                # gachawiki 페이지는 “Element / Class / Role / Rarity” 항목이 보통 포함 :contentReference[oaicite:2]{index=2}
                def find_value(label: str) -> Optional[str]:
                    # label 다음 줄에 값이 나오는 케이스가 많아서 텍스트 기반으로 보정
                    # 예: "Element\nWind"
                    pattern = rf"{label}\s*\n([A-Za-z]+)"
                    mm = re.search(pattern, text, re.IGNORECASE)
                    if mm:
                        return mm.group(1).strip()
                    return None

                element = find_value("Element")
                cclass = find_value("Class")
                role = find_value("Role")
                rarity = find_value("Rarity")

                chars.append({
                    "id": slugify(slug),
                    "name": name,
                    "element": element,
                    "class": cclass,
                    "role": role,
                    "rarity": rarity,
                    "page": url,
                    "source": "remote",
                })
            except Exception:
                continue

        return chars, len(chars), None

    except Exception as e:
        return [], 0, str(e)


def merge_characters(local_chars: List[Dict[str, Any]], remote_chars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    local(image) 기반 캐릭에 remote(속성/클래스/역할) 정보를 덮어씌움.
    id 기준으로 병합. remote에만 존재하면 추가.
    """
    by_id = {c["id"]: dict(c) for c in local_chars}

    for rc in remote_chars:
        cid = rc["id"]
        if cid in by_id:
            by_id[cid]["element"] = rc.get("element") or by_id[cid].get("element")
            by_id[cid]["class"] = rc.get("class") or by_id[cid].get("class")
            by_id[cid]["role"] = rc.get("role") or by_id[cid].get("role")
            by_id[cid]["rarity"] = rc.get("rarity") or by_id[cid].get("rarity")
            by_id[cid]["page"] = rc.get("page")
            by_id[cid]["source"] = "images+remote"
        else:
            # remote only
            by_id[cid] = dict(rc)
            # image는 없을 수 있음
            by_id[cid].setdefault("image", None)

    # 내부 역할(internal_role) 채움
    out = []
    for c in by_id.values():
        c["internal_role"] = infer_role(c)
        out.append(c)

    # 이름 기준 정렬
    out.sort(key=lambda x: (x.get("name") or x.get("id") or "").lower())
    return out


def refresh_cache() -> Dict[str, Any]:
    try:
        local_chars = scan_local_images(ZN_IMAGE_DIR)

        remote_chars = []
        remote_count = 0
        remote_error = None
        remote_scrape = False

        if not FORCE_LOCAL_ONLY:
            remote_scrape = True
            remote_chars, remote_count, remote_error = scrape_remote_characters()

        merged = merge_characters(local_chars, remote_chars)

        if remote_scrape and remote_count > 0 and len(local_chars) > 0:
            source = "images+remote"
        elif remote_scrape and remote_count > 0 and len(local_chars) == 0:
            source = "remote_only"
        else:
            source = "images_only"

        CACHE.update({
            "last_refresh": now_kst_iso(),
            "characters": merged,
            "source": source,
            "remote_scrape": remote_scrape,
            "remote_count": remote_count,
            "remote_error": remote_error,
            "cache_error": None,
        })
        return CACHE

    except Exception as e:
        CACHE["cache_error"] = str(e)
        return CACHE


###############################################################################
# 추천 로직(4인 파티)
###############################################################################
MODE_ROLE_WEIGHTS = {
    "pve":  {"DPS": 2.0, "Tank": 1.5, "Healer": 1.5, "Support": 1.2, "Debuffer": 1.2},
    "boss": {"DPS": 2.2, "Tank": 1.6, "Healer": 1.5, "Support": 1.3, "Debuffer": 1.6},
    "pvp":  {"DPS": 1.8, "Tank": 1.8, "Healer": 1.7, "Support": 1.4, "Debuffer": 1.4},
}

RARITY_BASE = {
    "SSR": 3.0,
    "SR":  2.0,
    "R":   1.0,
}

def rarity_score(r: Optional[str]) -> float:
    r = (r or "").upper().strip()
    return RARITY_BASE.get(r, 2.0)  # 모르면 중간값

def element_advantage(attacker: Optional[str], defender: Optional[str]) -> bool:
    if not attacker or not defender:
        return False
    return defender in ELEMENT_ADVANTAGE.get(attacker, [])

def score_team(
    team: List[Dict[str, Any]],
    mode: str,
    weakness_element: Optional[str],
    enemy_element: Optional[str],
    focus_ids: List[str],
) -> Tuple[float, List[str]]:
    """
    팀 점수 + 분석(사유) 반환
    """
    mode = (mode or "pve").lower()
    weights = MODE_ROLE_WEIGHTS.get(mode, MODE_ROLE_WEIGHTS["pve"])

    reasons = []
    score = 0.0

    # 개별 점수
    role_counts = {"Tank": 0, "Healer": 0, "Support": 0, "Debuffer": 0, "DPS": 0}
    weakness_hits = 0
    advantage_hits = 0
    focus_hits = 0

    for c in team:
        internal_role = c.get("internal_role") or infer_role(c)
        role_counts[internal_role] = role_counts.get(internal_role, 0) + 1

        base = rarity_score(c.get("rarity"))
        score += base * weights.get(internal_role, 1.0)

        # 약점 속성 매칭
        if weakness_element and c.get("element") == weakness_element:
            score += WEIGHT_MATCH_WEAKNESS
            weakness_hits += 1

        # 상성 우위
        if enemy_element and element_advantage(c.get("element"), enemy_element):
            score += WEIGHT_ADVANTAGE_OVER_ENEMY
            advantage_hits += 1

        # focus(중심 캐릭) 포함 가산
        if c.get("id") in focus_ids:
            score += 2.0
            focus_hits += 1

    # 필수 구조 보너스/패널티
    if role_counts["Tank"] >= 1:
        score += 2.0
    else:
        score -= 6.0
        reasons.append("탱커(Guardian/Tank) 부재")

    if role_counts["Healer"] >= 1:
        score += 2.0
    else:
        score -= 6.0
        reasons.append("힐러(Healer) 부재")

    # 모드별 선호
    if mode == "boss":
        if role_counts["Debuffer"] >= 1:
            score += 1.5
        else:
            score -= 1.0
            reasons.append("보스전인데 디버퍼가 없어 효율이 떨어질 수 있음")
    if mode == "pvp":
        if role_counts["Tank"] >= 1 and role_counts["Healer"] >= 1:
            score += 1.0

    # 중복 페널티(같은 역할 과다)
    # (게임 구조상 4인이라 역할이 한쪽으로 쏠리면 안정성이 떨어짐)
    if role_counts["Healer"] >= 2:
        score -= 1.0
        reasons.append("힐러 과다(2+)로 딜/유틸 부족 가능")
    if role_counts["Tank"] >= 2:
        score -= 0.5
        reasons.append("탱커 과다(2+)로 화력 부족 가능")

    # 약점/상성 요약
    if weakness_element:
        reasons.append(f"보스 약점 속성({weakness_element}) 매칭: {weakness_hits}/4")
    if enemy_element:
        reasons.append(f"상성 우위(Enemy={enemy_element}) 적용: {advantage_hits}/4")
    if focus_ids:
        reasons.append(f"선택 중심 캐릭 포함: {focus_hits}/{len(focus_ids)}")

    # 너무 길면 핵심만
    reasons = reasons[:6]
    return score, reasons


def recommend_parties(
    characters: List[Dict[str, Any]],
    mode: str,
    owned_ids: List[str],
    required_ids: List[str],
    banned_ids: List[str],
    party_size: int,
    weakness_element: Optional[str],
    enemy_element: Optional[str],
    focus_ids: List[str],
    top_k: int = 5,
) -> Dict[str, Any]:
    """
    조합 폭발을 피하기 위해 Beam Search 방식으로 4인 추천.
    """
    mode = (mode or "pve").lower()
    party_size = int(party_size or 4)
    if party_size != 4:
        party_size = 4  # Zone Nova는 4인 고정 전제로 운영 :contentReference[oaicite:3]{index=3}

    by_id = {c["id"]: c for c in characters}
    # name도 매칭되도록 보조 인덱스
    by_name = {slugify(c.get("name", "")): c["id"] for c in characters if c.get("name")}

    def to_id(x: str) -> Optional[str]:
        x = (x or "").strip()
        if not x:
            return None
        sx = slugify(x)
        if sx in by_id:
            return sx
        if sx in by_name:
            return by_name[sx]
        return None

    owned_set = set(filter(None, [to_id(x) for x in owned_ids]))
    req_set = set(filter(None, [to_id(x) for x in required_ids]))
    ban_set = set(filter(None, [to_id(x) for x in banned_ids]))
    focus_set = set(filter(None, [to_id(x) for x in focus_ids]))

    # owned 미지정이면 전체 사용 가능
    if not owned_set:
        owned_set = set(by_id.keys())

    # banned 제거
    owned_set -= ban_set

    # required가 owned에 없으면 불가능 처리
    missing_req = sorted([rid for rid in req_set if rid not in owned_set])
    if missing_req:
        return {
            "error": "required_not_owned",
            "missing_required": missing_req,
        }

    pool_ids = sorted(list(owned_set))
    pool = [by_id[i] for i in pool_ids]

    # required 팀 초기화
    required_team = [by_id[rid] for rid in sorted(req_set)]
    if len(required_team) > party_size:
        return {"error": "too_many_required", "required_count": len(required_team), "party_size": party_size}

    # Beam Search
    beam_width = 250
    beams: List[Tuple[List[Dict[str, Any]], float]] = []

    base_score, _ = score_team(required_team, mode, weakness_element, enemy_element, list(focus_set))
    beams.append((required_team, base_score))

    remaining_steps = party_size - len(required_team)

    for _ in range(remaining_steps):
        candidates = []
        for team, _team_score in beams:
            team_ids = {c["id"] for c in team}
            for c in pool:
                if c["id"] in team_ids:
                    continue
                # 확장
                new_team = team + [c]
                s, _ = score_team(new_team, mode, weakness_element, enemy_element, list(focus_set))
                candidates.append((new_team, s))

        # 상위 beam_width 유지
        candidates.sort(key=lambda x: x[1], reverse=True)
        beams = candidates[:beam_width]

        if not beams:
            break

    # 최종 팀 중 top_k
    final = []
    seen = set()
    for team, s in sorted(beams, key=lambda x: x[1], reverse=True):
        ids = tuple(sorted([c["id"] for c in team]))
        if ids in seen:
            continue
        seen.add(ids)
        _, reasons = score_team(team, mode, weakness_element, enemy_element, list(focus_set))
        final.append({
            "score": round(s, 3),
            "members": [
                {
                    "id": c["id"],
                    "name": c.get("name"),
                    "element": c.get("element"),
                    "class": c.get("class"),
                    "role": c.get("role"),
                    "internal_role": c.get("internal_role"),
                    "rarity": c.get("rarity"),
                    "image": c.get("image"),
                    "page": c.get("page"),
                } for c in team
            ],
            "analysis": reasons,
        })
        if len(final) >= top_k:
            break

    return {
        "mode": mode,
        "party_size": party_size,
        "inputs": {
            "owned": sorted(list(owned_set)),
            "required": sorted(list(req_set)),
            "banned": sorted(list(ban_set)),
            "focus": sorted(list(focus_set)),
            "weakness_element": weakness_element,
            "enemy_element": enemy_element,
        },
        "top_parties": final
    }


###############################################################################
# Flask App
###############################################################################
app = Flask(__name__, static_folder=None)

# 초기 1회 로딩(배포 직후 바로 UI 사용 가능)
refresh_cache()


###############################################################################
# Routes
###############################################################################
@app.get("/public/<path:filename>")
def public_files(filename):
    return send_from_directory(PUBLIC_DIR, filename)

@app.get("/")
def index():
    return redirect(url_for("ui_select"))

@app.get("/meta")
def meta():
    image_files = 0
    if os.path.isdir(ZN_IMAGE_DIR):
        image_files = len([f for f in os.listdir(ZN_IMAGE_DIR) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))])

    payload = {
        "title": APP_TITLE,
        "image_dir": ZN_IMAGE_DIR,
        "image_files": image_files,
        "last_refresh": CACHE.get("last_refresh"),
        "characters_cached": len(CACHE.get("characters", [])),
        "source": CACHE.get("source"),
        "remote_scrape": CACHE.get("remote_scrape"),
        "remote_count": CACHE.get("remote_count"),
        "bs4_available": BS4_AVAILABLE,
        "force_local_only": FORCE_LOCAL_ONLY,
        "remote_error": CACHE.get("remote_error"),
        "cache_error": CACHE.get("cache_error"),
        "endpoints": ["/refresh", "/zones/zone-nova/characters", "/ui/select", "/recommend"],
    }
    return json_response(payload)

@app.get("/refresh")
def refresh():
    refresh_cache()
    return redirect(url_for("meta"))

@app.get("/zones/zone-nova/characters")
def zones_zone_nova_characters():
    chars = CACHE.get("characters", [])
    payload = {
        "game": "zone-nova",
        "count": len(chars),
        "last_refresh": CACHE.get("last_refresh"),
        "source": CACHE.get("source"),
        "remote_scrape": CACHE.get("remote_scrape"),
        "remote_count": CACHE.get("remote_count"),
        "remote_error": CACHE.get("remote_error"),
        "characters": chars,
    }
    return json_response(payload)

@app.route("/recommend", methods=["GET", "POST"])
def recommend():
    if request.method == "GET":
        return redirect(url_for("ui_select"))

    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "pve").lower()
    owned = data.get("owned") or []
    required = data.get("required") or []
    banned = data.get("banned") or []
    focus = data.get("focus") or []
    weakness_element = data.get("weakness_element") or None
    enemy_element = data.get("enemy_element") or None

    # 방어: element 값 표준화
    if weakness_element:
        weakness_element = weakness_element.strip()
        if weakness_element not in ALL_ELEMENTS:
            weakness_element = None
    if enemy_element:
        enemy_element = enemy_element.strip()
        if enemy_element not in ALL_ELEMENTS:
            enemy_element = None

    result = recommend_parties(
        characters=CACHE.get("characters", []),
        mode=mode,
        owned_ids=owned,
        required_ids=required,
        banned_ids=banned,
        party_size=4,
        weakness_element=weakness_element,
        enemy_element=enemy_element,
        focus_ids=focus,
        top_k=5,
    )
    return json_response(result)


###############################################################################
# UI (표 형태)
###############################################################################
UI_HTML = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{{ title }}</title>
  <style>
    body { font-family: Arial, Helvetica, sans-serif; margin: 0; background:#0b0f14; color:#e8eef6; }
    header { padding: 18px 18px 10px; border-bottom: 1px solid rgba(255,255,255,.08); }
    header h1 { margin:0; font-size: 20px; letter-spacing:.2px; }
    header .sub { margin-top:6px; color:#a9b6c6; font-size: 12px; line-height: 1.4; }

    .wrap { padding: 16px 18px 40px; max-width: 1200px; margin: 0 auto; }
    .grid { display:grid; grid-template-columns: 380px 1fr; gap: 14px; }
    @media (max-width: 980px){ .grid { grid-template-columns: 1fr; } }

    .card { background:#121a24; border: 1px solid rgba(255,255,255,.08); border-radius: 12px; padding: 14px; }
    .card h2 { margin:0 0 10px; font-size: 14px; color:#dbe7f7; }
    .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    label { font-size: 12px; color:#c7d3e3; }
    select, input[type="text"] { background:#0b0f14; color:#e8eef6; border:1px solid rgba(255,255,255,.14); border-radius: 10px; padding: 10px; font-size: 13px; }
    button { background:#2b7cff; color:white; border:none; border-radius:10px; padding: 10px 12px; font-weight:700; cursor:pointer; }
    button.secondary { background:#243244; color:#cfe0f2; border:1px solid rgba(255,255,255,.14); }

    .list { display:grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    @media (max-width: 980px){ .list { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 520px){ .list { grid-template-columns: 1fr; } }

    .char { display:flex; gap: 10px; align-items:center; padding: 10px; border-radius: 10px; background:#0b0f14; border:1px solid rgba(255,255,255,.08); }
    .char img { width: 44px; height: 44px; border-radius: 10px; object-fit: cover; background:#0b0f14; }
    .char .nm { font-weight:700; font-size: 13px; }
    .char .meta { font-size: 11px; color:#a9b6c6; margin-top:2px; }
    .char input { transform: scale(1.1); }

    table { width:100%; border-collapse: collapse; overflow:hidden; border-radius: 12px; }
    th, td { padding: 10px 10px; border-bottom: 1px solid rgba(255,255,255,.08); vertical-align: top; }
    th { text-align:left; font-size: 12px; color:#cfe0f2; background:#0b0f14; position: sticky; top: 0; }
    td { font-size: 13px; color:#e8eef6; }
    .pill { display:inline-block; padding: 4px 8px; border-radius: 999px; border:1px solid rgba(255,255,255,.12); color:#cfe0f2; font-size: 11px; margin-right:6px; }
    .members { display:grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; }
    @media (max-width: 980px){ .members { grid-template-columns: repeat(2, minmax(120px, 1fr)); } }
    .mcard { display:flex; gap:8px; padding: 8px; background:#0b0f14; border:1px solid rgba(255,255,255,.08); border-radius: 10px; }
    .mcard img { width: 40px; height: 40px; border-radius: 10px; object-fit: cover; background:#0b0f14; }
    .mcard .t { font-weight:700; font-size: 12px; }
    .mcard .s { font-size: 11px; color:#a9b6c6; margin-top:2px; }

    .hint { font-size: 12px; color:#a9b6c6; line-height:1.5; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .small { font-size: 12px; color:#a9b6c6; }

    .adv { display:grid; grid-template-columns: repeat(5, 1fr); gap: 6px; margin-top: 10px; }
    .adv .box { background:#0b0f14; border:1px solid rgba(255,255,255,.08); border-radius: 10px; padding: 8px; font-size: 12px; }
    .adv .box .k { font-weight:700; }
  </style>
</head>
<body>
<header>
  <h1>{{ title }}</h1>
  <div class="sub">
    Characters cached: <b>{{ count }}</b> &nbsp;|&nbsp; Last refresh: <span class="mono">{{ last_refresh }}</span>
    &nbsp;|&nbsp; Source: <b>{{ source }}</b>
    &nbsp;|&nbsp; <a href="/refresh" style="color:#8fb7ff">Refresh</a>
    &nbsp;|&nbsp; <a href="/meta" style="color:#8fb7ff">Meta(JSON)</a>
  </div>
</header>

<div class="wrap">
  <div class="grid">
    <div class="card">
      <h2>추천 옵션</h2>

      <div class="row" style="margin-bottom:10px;">
        <label>Mode</label>
        <select id="mode">
          <option value="pve">pve</option>
          <option value="boss">boss</option>
          <option value="pvp">pvp</option>
        </select>
      </div>

      <div class="row" style="margin-bottom:10px;">
        <label>Boss Weakness Element</label>
        <select id="weakness">
          <option value="">(선택 안함)</option>
          {% for e in elements %}
          <option value="{{e}}">{{e}}</option>
          {% endfor %}
        </select>
      </div>

      <div class="row" style="margin-bottom:10px;">
        <label>Enemy Element (상성)</label>
        <select id="enemy">
          <option value="">(선택 안함)</option>
          {% for e in elements %}
          <option value="{{e}}">{{e}}</option>
          {% endfor %}
        </select>
      </div>

      <div class="hint">
        - <b>Required</b>: 무조건 포함(고정)<br/>
        - <b>Focus</b>: 꼭 고정은 아니지만 “중심 캐릭 기반으로 점수 가산”<br/>
        - <b>Banned</b>: 절대 제외<br/>
        - 파티는 <b>4인 고정</b>, 기본적으로 <b>탱+힐</b> 우선
      </div>

      <hr style="border:0;border-top:1px solid rgba(255,255,255,.08); margin:12px 0;"/>

      <div class="row" style="margin-bottom:10px;">
        <button onclick="applyQuick('required')" class="secondary">선택 캐릭 → Required</button>
        <button onclick="applyQuick('focus')" class="secondary">선택 캐릭 → Focus</button>
        <button onclick="applyQuick('banned')" class="secondary">선택 캐릭 → Banned</button>
      </div>

      <div style="margin-bottom:10px;">
        <label>Required (comma)</label>
        <input id="required" type="text" placeholder="ex) nina, freya" style="width:100%; margin-top:6px;"/>
      </div>

      <div style="margin-bottom:10px;">
        <label>Focus (comma)</label>
        <input id="focus" type="text" placeholder="ex) nina" style="width:100%; margin-top:6px;"/>
      </div>

      <div style="margin-bottom:10px;">
        <label>Banned (comma)</label>
        <input id="banned" type="text" placeholder="ex) apollo" style="width:100%; margin-top:6px;"/>
      </div>

      <div class="row" style="margin-top: 12px;">
        <button onclick="runRecommend()">Recommend</button>
        <button onclick="clearAll()" class="secondary">Clear</button>
      </div>

      <div style="margin-top: 12px;" class="small">
        <div><b>속성 상성표(기본)</b> (정확 규칙이 다르면 main.py의 ELEMENT_ADVANTAGE만 수정)</div>
        <div class="adv">
          {% for k,v in adv.items() %}
            <div class="box"><span class="k">{{k}}</span> ▶ {{ ", ".join(v) }}</div>
          {% endfor %}
        </div>
      </div>
    </div>

    <div class="card">
      <h2>보유 캐릭 선택 (Owned)</h2>
      <div class="list" id="charList">
        {% for c in chars %}
        <div class="char">
          <input type="checkbox" class="owned" value="{{ c['id'] }}" />
          {% if c.get("image") %}
            <img src="{{ c['image'] }}" onerror="this.style.display='none'" />
          {% else %}
            <img src="" style="display:none"/>
          {% endif %}
          <div>
            <div class="nm">{{ c.get("name") or c.get("id") }}</div>
            <div class="meta">
              <span class="pill">{{ c.get("internal_role") }}</span>
              <span class="pill">{{ c.get("element") or "?" }}</span>
              <span class="pill">{{ c.get("class") or "?" }}</span>
            </div>
          </div>
        </div>
        {% endfor %}
      </div>
      <div class="hint" style="margin-top:10px;">
        팁: 먼저 Owned 체크 → “선택 캐릭 → Required/Focus/Banned” 버튼으로 자동 입력 후 Recommend를 누르세요.
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:14px;">
    <h2>추천 결과 (표)</h2>
    <div id="result" class="hint">Recommend를 실행하면 결과가 표로 표시됩니다.</div>
  </div>
</div>

<script>
function getCheckedOwned(){
  const boxes = document.querySelectorAll(".owned:checked");
  return Array.from(boxes).map(b => b.value);
}

function csvToList(v){
  v = (v || "").trim();
  if(!v) return [];
  return v.split(",").map(x => x.trim()).filter(Boolean);
}

function listToCsv(arr){
  return (arr || []).join(", ");
}

function applyQuick(target){
  const owned = getCheckedOwned();
  if(owned.length === 0){
    alert("먼저 Owned 체크를 해주세요.");
    return;
  }
  const el = document.getElementById(target);
  const current = new Set(csvToList(el.value));
  owned.forEach(x => current.add(x));
  el.value = listToCsv(Array.from(current));
}

function clearAll(){
  document.querySelectorAll(".owned").forEach(b => b.checked = false);
  ["required","focus","banned"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("weakness").value = "";
  document.getElementById("enemy").value = "";
  document.getElementById("result").innerHTML = "Recommend를 실행하면 결과가 표로 표시됩니다.";
}

async function runRecommend(){
  const mode = document.getElementById("mode").value;
  const owned = getCheckedOwned();
  const required = csvToList(document.getElementById("required").value);
  const focus = csvToList(document.getElementById("focus").value);
  const banned = csvToList(document.getElementById("banned").value);
  const weakness_element = document.getElementById("weakness").value || null;
  const enemy_element = document.getElementById("enemy").value || null;

  const payload = { mode, owned, required, focus, banned, weakness_element, enemy_element };

  document.getElementById("result").innerHTML = "계산 중...";

  const r = await fetch("/recommend", {
    method:"POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(payload)
  });

  const data = await r.json();
  if(data.error){
    document.getElementById("result").innerHTML =
      "<div class='mono'>Error: " + JSON.stringify(data, null, 2) + "</div>";
    return;
  }

  const rows = (data.top_parties || []);
  if(rows.length === 0){
    document.getElementById("result").innerHTML = "추천 결과가 없습니다. (Owned/Required/Banned 조건 확인)";
    return;
  }

  let html = "";
  html += "<div class='small'>Inputs: <span class='mono'>" + JSON.stringify(data.inputs) + "</span></div>";
  html += "<div style='margin-top:10px; overflow:auto; max-height: 640px; border-radius:12px; border:1px solid rgba(255,255,255,.08)'>";
  html += "<table>";
  html += "<thead><tr>";
  html += "<th style='width:70px'>Rank</th>";
  html += "<th style='width:90px'>Score</th>";
  html += "<th>Party (4)</th>";
  html += "<th style='width:320px'>Analysis</th>";
  html += "</tr></thead>";
  html += "<tbody>";

  rows.forEach((p, idx) => {
    html += "<tr>";
    html += "<td><b>#"+(idx+1)+"</b></td>";
    html += "<td><b>"+p.score+"</b></td>";

    html += "<td>";
    html += "<div class='members'>";
    (p.members||[]).forEach(m => {
      const nm = m.name || m.id;
      const img = m.image ? m.image : "";
      html += "<div class='mcard'>";
      if(img){
        html += "<img src='"+img+"' onerror=\"this.style.display='none'\" />";
      }else{
        html += "<img src='' style='display:none'/>";
      }
      html += "<div>";
      html += "<div class='t'>"+nm+"</div>";
      html += "<div class='s'>"+(m.internal_role||'?')+" | "+(m.element||'?')+" | "+(m.class||'?')+"</div>";
      html += "</div></div>";
    });
    html += "</div>";
    html += "</td>";

    html += "<td>";
    html += "<ul style='margin:0; padding-left:18px'>";
    (p.analysis||[]).forEach(a => html += "<li>"+a+"</li>");
    html += "</ul>";
    html += "</td>";

    html += "</tr>";
  });

  html += "</tbody></table></div>";
  document.getElementById("result").innerHTML = html;
}
</script>

</body>
</html>
"""

@app.get("/ui/select")
def ui_select():
    chars = CACHE.get("characters", [])
    return render_template_string(
        UI_HTML,
        title=APP_TITLE,
        count=len(chars),
        last_refresh=CACHE.get("last_refresh"),
        source=CACHE.get("source"),
        chars=chars,
        elements=ALL_ELEMENTS,
        adv=ELEMENT_ADVANTAGE
    )


###############################################################################
# Local Run
###############################################################################
if __name__ == "__main__":
    # Render에서는 PORT 환경변수 사용. 로컬은 기본 40000.
    port = int(os.environ.get("PORT", "40000"))
    app.run(host="0.0.0.0", port=port, debug=True)
