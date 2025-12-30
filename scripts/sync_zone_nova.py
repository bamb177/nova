import os
import re
import json
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# (fallback) 웹 파싱은 남겨두되, Actions에서는 기본적으로 GitHub 레포(업스트림)에서 추출하도록 구성
import requests
from bs4 import BeautifulSoup


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE_DIR, "public", "data", "zone-nova", "characters_meta.json")

FALLBACK_WEB_URL = "https://gachawiki.info/guides/zone-nova/character-comparison-v2/"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def normalize_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in {"dps", "tank", "healer", "buffer", "debuffer"}:
        return r
    if "heal" in r:
        return "healer"
    if "tank" in r or "guard" in r or "defend" in r:
        return "tank"
    if "debuff" in r:
        return "debuffer"
    if "buff" in r or "support" in r:
        return "buffer"
    if "dps" in r or "damage" in r or "attacker" in r:
        return "dps"
    return r or "-"


def ensure_parent_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def safe_read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def extract_candidates(upstream_root: str) -> List[str]:
    """
    업스트림 레포에서 zone-nova 관련 JSON을 전부 찾아 후보로 수집.
    우선순위:
      1) public/data/zone-nova 아래 JSON
      2) public/data/**/zone-nova 관련 JSON
      3) 전체에서 zone-nova + characters 포함 JSON
    """
    cand: List[str] = []

    def walk_collect(root: str):
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(".json"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, upstream_root).replace("\\", "/")
                cand.append(rel)

    # 1) 가장 기대되는 위치
    p1 = os.path.join(upstream_root, "public", "data", "zone-nova")
    if os.path.isdir(p1):
        walk_collect(p1)

    # 2) public/data 전체
    p2 = os.path.join(upstream_root, "public", "data")
    if os.path.isdir(p2):
        walk_collect(p2)

    # 3) src(혹시 JSON이 src에 있을 수 있음)
    p3 = os.path.join(upstream_root, "src")
    if os.path.isdir(p3):
        walk_collect(p3)

    # 중복 제거
    cand = sorted(list(dict.fromkeys(cand)))

    # 필터링/정렬 우선순위 적용
    def score(rel: str) -> Tuple[int, int, int]:
        r = rel.lower()
        s = 0
        if r.startswith("public/data/zone-nova/"):
            s += 300
        if "zone-nova" in r:
            s += 200
        if "character" in r:
            s += 150
        if "characters" in r:
            s += 150
        if "meta" in r:
            s += 50
        # 짧을수록 우선
        return (-s, len(r), 0)

    cand.sort(key=score)
    return cand


def try_build_meta_from_obj(obj: Any) -> Optional[Dict[str, Dict[str, str]]]:
    """
    다양한 JSON 형태를 받아서:
      {id: {...}} or {characters:[...]} or [...] 형태를 캐릭터 meta map으로 변환
    결과:
      { "nina": {"name":"Nina","rarity":"SSR","element":"Wind","role":"dps"}, ... }
    """
    items: List[Dict[str, Any]] = []

    if isinstance(obj, list):
        # list[character]
        if obj and isinstance(obj[0], dict):
            items = obj
        else:
            return None

    elif isinstance(obj, dict):
        # {characters:[...]} or {characters:{...}} or {id:{...}}
        if "characters" in obj:
            c = obj["characters"]
            if isinstance(c, list) and c and isinstance(c[0], dict):
                items = c
            elif isinstance(c, dict):
                # dict map 형태
                items = []
                for k, v in c.items():
                    if isinstance(v, dict):
                        vv = dict(v)
                        vv["_id"] = k
                        items.append(vv)
            else:
                return None
        else:
            # dict map 형태로 간주
            items = []
            for k, v in obj.items():
                if isinstance(v, dict):
                    vv = dict(v)
                    vv["_id"] = k
                    items.append(vv)

    else:
        return None

    meta: Dict[str, Dict[str, str]] = {}

    for it in items:
        if not isinstance(it, dict):
            continue

        # 가능한 필드들 폭넓게 대응
        name = (it.get("name") or it.get("title") or it.get("character") or it.get("displayName") or "").strip()
        if not name:
            # id 기반 이름이라도 없으면 패스
            continue

        rarity = (it.get("rarity") or it.get("grade") or it.get("rank") or "").strip()
        element = (it.get("element") or it.get("attr") or it.get("attribute") or "").strip()
        role = (it.get("role") or it.get("class") or it.get("type") or "").strip()

        # 최소 요건: rarity/element는 있어야 “게임 데이터”로 쓸만함
        if not rarity or not element:
            continue

        sid = slug_id(it.get("_id") or name)

        # Jeanne D Arc 통일
        sid_low = sid.lower()
        if sid_low in {"joanofarc", "jeannedarc"} or "jeanne" in sid_low:
            sid = "jeannedarc"
            name = "Jeanne D Arc"

        meta[sid] = {
            "name": name,                       # UI는 영어 유지
            "rarity": rarity.upper(),
            "element": element,
            "role": normalize_role(role),
        }

    # 정상이라면 40+ 기대. 너무 적으면 후보 파일이 아닌 것으로 간주
    if len(meta) < 20:
        return None

    return meta


def fetch_web(url: str, timeout: int = 30) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaSync/1.0)"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def pick_value(lines: List[str], key: str) -> Optional[str]:
    for i, l in enumerate(lines):
        if l == key and i + 1 < len(lines):
            return lines[i + 1]
    return None


def parse_from_web_comparison_v2(html: str) -> Dict[str, Dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    chunks = text.split("\n### ")
    if len(chunks) < 2:
        raise RuntimeError("웹 파싱 실패: '### 캐릭터명' 패턴을 찾지 못했습니다. (페이지 구조 변경/차단 가능)")

    result: Dict[str, Dict[str, str]] = {}
    for chunk in chunks[1:]:
        name = chunk.split("\n", 1)[0].strip()
        if not name:
            continue
        lines = [x.strip() for x in chunk.split("\n") if x.strip()]
        rarity = pick_value(lines, "Rarity")
        element = pick_value(lines, "Element")
        role = pick_value(lines, "Role")
        if not (rarity and element and role):
            continue

        sid = slug_id(name)
        if sid in {"joanofarc", "jeannedarc"} or "jeanne" in sid:
            sid = "jeannedarc"
            name = "Jeanne D Arc"

        result[sid] = {
            "name": name,
            "rarity": rarity.strip().upper(),
            "element": element.strip(),
            "role": normalize_role(role),
        }

    if len(result) < 20:
        raise RuntimeError(f"웹 파싱 결과가 너무 적습니다({len(result)}). 페이지 구조 변경/차단 가능성이 큽니다.")
    return result


def build_from_upstream_repo(upstream_root: str) -> Tuple[Dict[str, Dict[str, str]], str]:
    candidates = extract_candidates(upstream_root)

    tried = 0
    for rel in candidates:
        if "zone-nova" not in rel.lower():
            continue
        # characters 관련 JSON 우선
        if ("character" not in rel.lower()) and ("characters" not in rel.lower()):
            continue

        full = os.path.join(upstream_root, rel.replace("/", os.sep))
        obj = safe_read_json(full)
        if obj is None:
            continue

        tried += 1
        meta = try_build_meta_from_obj(obj)
        if meta:
            return meta, rel

        # 너무 많은 시도를 할 필요는 없음
        if tried >= 25:
            break

    # 2차: zone-nova가 포함된 JSON 전반에서 다시 탐색(확장)
    tried = 0
    for rel in candidates:
        if "zone-nova" not in rel.lower():
            continue
        full = os.path.join(upstream_root, rel.replace("/", os.sep))
        obj = safe_read_json(full)
        if obj is None:
            continue
        tried += 1
        meta = try_build_meta_from_obj(obj)
        if meta:
            return meta, rel
        if tried >= 80:
            break

    # 실패 시 디버그 메시지
    sample = "\n- " + "\n- ".join(candidates[:25])
    raise RuntimeError(
        "업스트림 레포에서 zone-nova 캐릭터 JSON을 자동 추출하지 못했습니다.\n"
        "후보 JSON(일부):" + sample
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write characters_meta.json to repo")
    ap.add_argument("--upstream", default="", help="path to cloned boring877/gacha-wiki repo")
    ap.add_argument("--allow-web-fallback", action="store_true", help="if upstream fails, try web scrape fallback")
    args = ap.parse_args()

    try:
        meta: Dict[str, Dict[str, str]] = {}
        source = "unknown"

        if args.upstream:
            upstream_root = os.path.abspath(args.upstream)
            if not os.path.isdir(upstream_root):
                raise RuntimeError(f"--upstream 경로가 존재하지 않습니다: {upstream_root}")
            meta, picked = build_from_upstream_repo(upstream_root)
            source = f"upstream_repo:{picked}"
        else:
            raise RuntimeError("--upstream 경로가 필요합니다. (Actions에서 업스트림 레포를 clone하도록 워크플로우를 구성하세요.)")

        payload = {
            "_meta": {
                "game": "zone-nova",
                "source": source,
                "generated_at": now_iso(),
                "count": len(meta),
            },
            "characters": meta,
        }

        if args.write:
            ensure_parent_dir(OUT_PATH)
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            print(f"[OK] wrote: {OUT_PATH}")
            print(f"[OK] count={len(meta)} source={source}")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])

    except Exception as e:
        # 웹 fallback 옵션이 켜져있고, upstream이 실패했을 때만 시도
        if args.allow_web_fallback:
            try:
                print(f"[WARN] upstream failed: {e}")
                html = fetch_web(FALLBACK_WEB_URL)
                meta = parse_from_web_comparison_v2(html)
                payload = {
                    "_meta": {
                        "game": "zone-nova",
                        "source": f"web:{FALLBACK_WEB_URL}",
                        "generated_at": now_iso(),
                        "count": len(meta),
                    },
                    "characters": meta,
                }
                ensure_parent_dir(OUT_PATH)
                with open(OUT_PATH, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
                print(f"[OK] wrote via web fallback: {OUT_PATH} (count={len(meta)})")
                return
            except Exception as e2:
                print("[ERROR] upstream + web fallback 모두 실패")
                print(f"upstream error: {e}")
                print(f"web error: {e2}")
                raise

        print("[ERROR] sync failed:", str(e))
        raise


if __name__ == "__main__":
    main()
