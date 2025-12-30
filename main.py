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
# Remote parse (bs4)
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
# Scoring / Party search
# =========================
def rarity_bonus(r: Optional[str]) -> float:
    r = (r or "").upper().strip()
    return {"SSR": 20.0, "SR": 10.0, "R": 0.0}.get(r, 0.0)


def role_bonus(role: Optional[str], mode: str) -> float:
    r = (role or "").strip().lower()

    if mode == "boss":
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

    # pve
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


def count_by_key(party: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in party:
        v = (c.get(key) or "Unknown")
        out[v] = out.get(v, 0) + 1
    return out


def party_synergy_bonus(party: List[Dict[str, Any]], mode: str, prefer: str) -> Tuple[float, List[str]]:
    """
    prefer: mono | diverse | balanced
    """
    reasons: List[str] = []
    bonus = 0.0

    roles = count_by_key(party, "role")
    elems = count_by_key(party, "element")

    has_tank = roles.get("Tank", 0) >= 1
    has_heal = roles.get("Healer", 0) >= 1

    # 역할 구성 보너스
    if has_tank:
        bonus += 12.0; reasons.append("탱커 포함")
    if has_heal:
        bonus += 12.0; reasons.append("힐러 포함")

    if mode == "boss":
        if roles.get("DPS", 0) >= 2:
            bonus += 10.0; reasons.append("보스전: 딜러 2명+")
        if roles.get("Debuffer", 0) >= 1:
            bonus += 8.0; reasons.append("보스전: 디버퍼 포함")
    elif mode == "pvp":
        if has_tank and has_heal:
            bonus += 10.0; reasons.append("PVP: 탱+힐 안정")
        if roles.get("Debuffer", 0) >= 1:
            bonus += 6.0; reasons.append("PVP: 디버퍼 포함")
    else:  # pve
        if roles.get("DPS", 0) >= 2:
            bonus += 8.0; reasons.append("PVE: 딜러 2명+")
        if roles.get("Buffer", 0) >= 1 or roles.get("Debuffer", 0) >= 1:
            bonus += 6.0; reasons.append("PVE: 버프/디버프 포함")

    # 속성(엘리먼트) 시너지(과도하게 강제하지 않도록 작은 가중치)
    unique_elems = len([k for k in elems.keys() if k and k != "Unknown"])
    if prefer == "mono":
        if unique_elems == 1:
            bonus += 8.0; reasons.append("속성 통일(모노)")
        elif unique_elems == 2:
            bonus += 3.0; reasons.append("속성 2종")
    elif prefer == "diverse":
        if unique_elems >= 3:
            bonus += 8.0; reasons.append("속성 다양(3종+)")
        elif unique_elems == 2:
            bonus += 4.0; reasons.append("속성 2종")
    else:  # balanced
        if unique_elems == 2:
            bonus += 6.0; reasons.append("속성 밸런스(2종)")
        elif unique_elems == 3:
            bonus += 5.0; reasons.append("속성 3종")

    return bonus, reasons


def normalize_id_or_name_list(xs: Any) -> List[str]:
    if not isinstance(xs, list):
        return []
    out: List[str] = []
    for x in xs:
        k = str(x).strip().lower()
        if k:
            out.append(k)
    return out


def choose_candidate_pool(owned_chars: List[Dict[str, Any]], mode: str, pool_size: int = 22) -> List[Dict[str, Any]]:
    """
    조합 탐색 폭발 방지:
    - 개인 점수 상위 pool_size명을 기본 후보로 쓰되,
    - 탱/힐이 pool에 없으면 각각 1명은 강제로 포함
    """
    scored = sorted(owned_chars, key=lambda c: score_character(c, mode), reverse=True)
    base = scored[:max(8, min(pool_size, len(scored)))]

    # 탱/힐 보정 포함
    def best_of_role(role: str) -> Optional[Dict[str, Any]]:
        candidates = [c for c in owned_chars if (c.get("role") or "").lower() == role.lower()]
        if not candidates:
            return None
        candidates.sort(key=lambda c: score_character(c, mode), reverse=True)
        return candidates[0]

    bt = best_of_role("tank")
    bh = best_of_role("healer")

    if bt and all(c["id"] != bt["id"] for c in base):
        base.append(bt)
    if bh and all(c["id"] != bh["id"] for c in base):
        base.append(bh)

    # 중복 제거
    uniq = {c["id"]: c for c in base}
    return list(uniq.values())


def build_top_parties(
    owned_chars: List[Dict[str, Any]],
    mode: str,
    top_k: int,
    required_ids_or_names: List[str],
    banned_ids_or_names: List[str],
    enforce_roles: bool,
    prefer: str
) -> Dict[str, Any]:
    """
    Top K 파티 탐색(4인).
    - 후보풀을 제한 후(기본 22명), 4인 조합을 전수 검색
    - 점수: 개인 점수 합 + 시너지 보너스
    """
    if top_k < 1:
        top_k = 1
    if top_k > 10:
        top_k = 10

    # 캐릭터 lookup
    by_id = {c["id"].lower(): c for c in owned_chars}
    by_name = {c["name"].lower(): c for c in owned_chars}

    def resolve_one(k: str) -> Optional[Dict[str, Any]]:
        if k in by_id:
            return by_id[k]
        if k in by_name:
            return by_name[k]
        return None

    required_objs: List[Dict[str, Any]] = []
    for k in required_ids_or_names:
        c = resolve_one(k)
        if c:
            required_objs.append(c)
    required_objs = list({c["id"]: c for c in required_objs}.values())

    banned_set: set[str] = set()
    for k in banned_ids_or_names:
        c = resolve_one(k)
        if c:
            banned_set.add(c["id"])

    # banned 제거
    filtered = [c for c in owned_chars if c["id"] not in banned_set]

    # required가 banned 되었거나 owned에 없을 때 문제 표시
    issues: List[str] = []
    for r in required_ids_or_names:
        if not resolve_one(r):
            issues.append(f"필수 캐릭터 미보유/미인식: {r}")

    # 후보풀 축소
    candidate_pool = choose_candidate_pool(filtered, mode, pool_size=22)

    # required는 후보풀에 반드시 포함
    for rc in required_objs:
        if all(c["id"] != rc["id"] for c in candidate_pool):
            candidate_pool.append(rc)

    # 그래도 4명 미만이면 종료
    if len(candidate_pool) < PARTY_SIZE:
        return {
            "ok": False,
            "error": f"후보 풀이 {len(candidate_pool)}명이라 파티 구성 불가",
            "issues": issues,
            "parties": [],
        }

    # 조합 탐색(전수)
    best: List[Tuple[float, Dict[str, Any]]] = []

    # 고정 required 처리: required가 4명 초과면 불가
    if len(required_objs) > PARTY_SIZE:
        return {
            "ok": False,
            "error": f"필수 캐릭터가 {len(required_objs)}명이라 4인 파티 불가",
            "issues": issues,
            "parties": [],
        }

    required_ids = {c["id"] for c in required_objs}
    pool = [c for c in candidate_pool if c["id"] not in required_ids]

    # 4인 조합 생성(필수 포함)
    need = PARTY_SIZE - len(required_objs)

    # 작은 최적화: pool 정렬
    pool.sort(key=lambda c: score_character(c, mode), reverse=True)

    # 조합 생성 (need가 0/1/2/3/4)
    def iter_combos(lst: List[Dict[str, Any]], k: int):
        # 간단 조합 생성기(내장 itertools 없이 구현)
        n = len(lst)
        if k == 0:
            yield []
            return
        if k == 1:
            for i in range(n):
                yield [lst[i]]
            return
        if k == 2:
            for i in range(n):
                for j in range(i + 1, n):
                    yield [lst[i], lst[j]]
            return
        if k == 3:
            for i in range(n):
                for j in range(i + 1, n):
                    for m in range(j + 1, n):
                        yield [lst[i], lst[j], lst[m]]
            return
        if k == 4:
            for i in range(n):
                for j in range(i + 1, n):
                    for m in range(j + 1, n):
                        for t in range(m + 1, n):
                            yield [lst[i], lst[j], lst[m], lst[t]]
            return
        # 그 외는 미지원
        return

    for picked in iter_combos(pool, need):
        party = required_objs + picked
        if len(party) != PARTY_SIZE:
            continue

        # 역할 강제 조건
        if enforce_roles:
            roles = [(c.get("role") or "").lower() for c in party]
            if "tank" not in roles:
                continue
            if "healer" not in roles:
                continue

        # 개인 점수 합
        indiv = sum(score_character(c, mode) for c in party)

        # 시너지 보너스
        syn_bonus, reasons = party_synergy_bonus(party, mode, prefer)
        total = indiv + syn_bonus

        # 상위 top_k 유지
        entry = {
            "score_total": round(total, 2),
            "score_individual_sum": round(indiv, 2),
            "score_synergy_bonus": round(syn_bonus, 2),
            "reasons": reasons,
            "roles": count_by_key(party, "role"),
            "elements": count_by_key(party, "element"),
            "party": [{
                "id": c["id"],
                "name": c["name"],
                "rarity": c.get("rarity"),
                "element": c.get("element"),
                "role": c.get("role"),
                "class": c.get("class"),
                "faction": c.get("faction"),
                "image": c.get("image"),
                "score": round(score_character(c, mode), 2),
            } for c in sorted(party, key=lambda x: score_character(x, mode), reverse=True)]
        }

        best.append((total, entry))

    if not best:
        # enforce_roles 때문에 한 건도 안 나오는 경우
        if enforce_roles:
            issues.append("탱커/힐러 강제 조건으로 조합 0건(보유 풀 부족 가능)")
        return {
            "ok": True,
            "issues": issues,
            "parties": [],
        }

    best.sort(key=lambda x: x[0], reverse=True)
    top = [e for _, e in best[:top_k]]

    # 대체 후보(파티에 포함되지 않은 상위 3명)
    # (첫 번째 파티 기준)
    used_ids = set()
    if top:
        used_ids = {c["id"] for c in top[0]["party"]}

    remain = [c for c in filtered if c["id"] not in used_ids]
    remain.sort(key=lambda c: score_character(c, mode), reverse=True)

    alternatives = [{
        "id": c["id"],
        "name": c["name"],
        "rarity": c.get("rarity"),
        "element": c.get("element"),
        "role": c.get("role"),
        "score": round(score_character(c, mode), 2),
        "image": c.get("image"),
    } for c in remain[:3]]

    return {
        "ok": True,
        "issues": issues,
        "alternatives": alternatives,
        "parties": top,
    }


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
      <a href="/recommend/v2" style="margin-left:10px;">/recommend/v2</a>
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
  <p>기본 추천(Top1). 고급 옵션은 <code>/recommend/v2</code>를 사용하세요.</p>
  <pre style="background:#f5f5f5;padding:12px;border-radius:8px;">
POST /recommend
Content-Type: application/json

{{
  "mode": "pve",
  "owned": ["nina", "freya", "lavinia", "apep"]
}}
  </pre>
</body></html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.post("/recommend")
def recommend() -> Response:
    """
    기존 호환:
    - 내부적으로 v2를 호출하여 parties[0]을 best_party로 내려줌
    """
    ensure_cache_loaded()
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "pve").strip().lower()
    owned = data.get("owned") or []

    if mode not in {"pve", "boss", "pvp"}:
        mode = "pve"
    if not isinstance(owned, list):
        return Response(json.dumps({"error": "owned는 배열이어야 합니다."}, ensure_ascii=False),
                        mimetype="application/json; charset=utf-8", status=400)

    owned_keys = normalize_id_or_name_list(owned)

    chars = CACHE["zone_nova"]["characters"]
    by_id = {c["id"].lower(): c for c in chars}
    by_name = {c["name"].lower(): c for c in chars}

    owned_chars: List[Dict[str, Any]] = []
    for k in owned_keys:
        if k in by_id:
            owned_chars.append(by_id[k])
        elif k in by_name:
            owned_chars.append(by_name[k])
    owned_chars = list({c["id"]: c for c in owned_chars}.values())

    if len(owned_chars) < PARTY_SIZE:
        return Response(json.dumps({"error": f"최소 {PARTY_SIZE}명 필요", "count_owned": len(owned_chars)}, ensure_ascii=False),
                        mimetype="application/json; charset=utf-8", status=400)

    out = build_top_parties(
        owned_chars=owned_chars,
        mode=mode,
        top_k=1,
        required_ids_or_names=[],
        banned_ids_or_names=[],
        enforce_roles=True,
        prefer="balanced",
    )

    parties = out.get("parties") or []
    best_party = parties[0]["party"] if parties else []

    result = {
        "mode": mode,
        "best_party": best_party,
        "issues": out.get("issues") or [],
        "alternatives": out.get("alternatives") or [],
        "data_source": CACHE["zone_nova"]["source"],
        "remote_ok": CACHE["zone_nova"]["remote_ok"],
        "remote_count": CACHE["zone_nova"]["remote_count"],
        "remote_error": CACHE["zone_nova"]["remote_error"],
    }
    return Response(json.dumps(result, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.get("/recommend/v2")
def recommend_v2_help() -> Response:
    html = f"""
<!doctype html>
<html lang="ko">
<head><meta charset="utf-8" /><title>{APP_TITLE}</title></head>
<body style="font-family: Arial, sans-serif; margin: 24px;">
  <h2>{APP_TITLE} /recommend/v2</h2>
  <p>Top N 파티 + 필수/제외/선호 시너지 지원</p>
  <pre style="background:#f5f5f5;padding:12px;border-radius:8px;">
POST /recommend/v2
Content-Type: application/json

{{
  "mode": "pve",              // pve | boss | pvp
  "owned": ["nina","freya","lavinia","apep"],
  "top_k": 3,                 // 1~10
  "required": ["nina"],        // (선택) 꼭 포함할 캐릭(id 또는 name)
  "banned": ["xxx"],           // (선택) 제외할 캐릭(id 또는 name)
  "enforce_roles": true,       // 탱+힐 강제(기본 true)
  "prefer": "balanced"         // mono | diverse | balanced
}}
  </pre>
</body></html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.post("/recommend/v2")
def recommend_v2() -> Response:
    ensure_cache_loaded()
    data = request.get_json(silent=True) or {}

    mode = (data.get("mode") or "pve").strip().lower()
    if mode not in {"pve", "boss", "pvp"}:
        mode = "pve"

    owned = data.get("owned") or []
    if not isinstance(owned, list):
        return Response(json.dumps({"error": "owned는 배열이어야 합니다."}, ensure_ascii=False),
                        mimetype="application/json; charset=utf-8", status=400)

    top_k = data.get("top_k", 3)
    try:
        top_k = int(top_k)
    except Exception:
        top_k = 3

    required = normalize_id_or_name_list(data.get("required"))
    banned = normalize_id_or_name_list(data.get("banned"))

    enforce_roles = data.get("enforce_roles", True)
    enforce_roles = bool(enforce_roles)

    prefer = (data.get("prefer") or "balanced").strip().lower()
    if prefer not in {"mono", "diverse", "balanced"}:
        prefer = "balanced"

    owned_keys = normalize_id_or_name_list(owned)

    chars = CACHE["zone_nova"]["characters"]
    by_id = {c["id"].lower(): c for c in chars}
    by_name = {c["name"].lower(): c for c in chars}

    owned_chars: List[Dict[str, Any]] = []
    for k in owned_keys:
        if k in by_id:
            owned_chars.append(by_id[k])
        elif k in by_name:
            owned_chars.append(by_name[k])
    owned_chars = list({c["id"]: c for c in owned_chars}.values())

    if len(owned_chars) < PARTY_SIZE:
        return Response(json.dumps({"error": f"최소 {PARTY_SIZE}명 필요", "count_owned": len(owned_chars)}, ensure_ascii=False),
                        mimetype="application/json; charset=utf-8", status=400)

    out = build_top_parties(
        owned_chars=owned_chars,
        mode=mode,
        top_k=top_k,
        required_ids_or_names=required,
        banned_ids_or_names=banned,
        enforce_roles=enforce_roles,
        prefer=prefer,
    )

    result = {
        "mode": mode,
        "top_k": top_k,
        "prefer": prefer,
        "enforce_roles": enforce_roles,
        "issues": out.get("issues") or [],
        "alternatives": out.get("alternatives") or [],
        "parties": out.get("parties") or [],
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
    <div style="margin-bottom:12px; display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
      <div>
        Mode:
        <select id="mode">
          <option value="pve">pve</option>
          <option value="boss">boss</option>
          <option value="pvp">pvp</option>
        </select>
      </div>

      <div>
        Top:
        <select id="top_k">
          <option value="1">1</option>
          <option value="3" selected>3</option>
          <option value="5">5</option>
        </select>
      </div>

      <div>
        Prefer:
        <select id="prefer">
          <option value="balanced" selected>balanced</option>
          <option value="mono">mono</option>
          <option value="diverse">diverse</option>
        </select>
      </div>

      <div>
        <label><input type="checkbox" id="enforce_roles" checked /> 탱+힐 강제</label>
      </div>

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
  const top_k = parseInt(document.getElementById("top_k").value, 10);
  const prefer = document.getElementById("prefer").value;
  const enforce_roles = document.getElementById("enforce_roles").checked;
  const owned = Array.from(document.querySelectorAll('input[name="owned"]:checked')).map(x => x.value);

  const res = await fetch("/recommend/v2", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ mode, owned, top_k, prefer, enforce_roles }})
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
