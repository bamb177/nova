import os
import re
import json
import argparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE_DIR, "public", "data", "zone-nova", "characters_meta.json")

# 가장 안정적으로 "전체 캐릭터 + (Role/Element/Rarity)"가 한 페이지에 모이는 곳
SOURCE_URL = "https://gachawiki.info/guides/zone-nova/character-comparison-v2/"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def pick_value(lines, key):
    """lines에서 key 다음 줄 값을 가져옴."""
    for i, l in enumerate(lines):
        if l == key and i + 1 < len(lines):
            return lines[i + 1]
    return None


def normalize_role(role: str) -> str:
    # 비교 페이지는 Role이 DPS/Tank/Healer/Buffer/Debuffer 형태
    r = (role or "").strip().lower()
    # 혹시 class/role 혼재될 때를 대비
    if r in {"dps", "tank", "healer", "buffer", "debuffer"}:
        return r
    # 예외 처리
    if "heal" in r:
        return "healer"
    if "tank" in r or "guard" in r:
        return "tank"
    if "debuff" in r:
        return "debuffer"
    if "buff" in r:
        return "buffer"
    if "dps" in r or "damage" in r:
        return "dps"
    return r or "-"


def parse_characters_from_comparison_v2(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    # 페이지 텍스트가 매우 길기 때문에 "### 캐릭터명" 단위로 쪼개서 파싱
    chunks = text.split("\n### ")
    if len(chunks) < 2:
        raise RuntimeError("파싱 실패: '### 캐릭터명' 패턴을 찾지 못했습니다. 소스 페이지 구조가 변경되었을 수 있습니다.")

    result = {}
    for chunk in chunks[1:]:
        # 첫 줄이 이름
        name = chunk.split("\n", 1)[0].strip()
        if not name:
            continue

        # 공백/빈줄 제거한 라인 목록
        lines = [x.strip() for x in chunk.split("\n") if x.strip()]

        rarity = pick_value(lines, "Rarity")
        element = pick_value(lines, "Element")
        role = pick_value(lines, "Role")

        # 이 3개가 없으면 캐릭터 블록이 아닌 노이즈일 가능성이 큼
        if not (rarity and element and role):
            continue

        # Jeanne D Arc 정규화(이전 오류 방지)
        name_norm = name.replace("’", "'").strip()
        sid = slug_id(name_norm)

        if sid in {"joanofarc", "jeannedarc"} or "jeanne" in sid:
            sid = "jeannedarc"
            name_norm = "Jeanne D Arc"

        result[sid] = {
            "name": name_norm,               # UI에 영어 이름 유지
            "rarity": rarity.strip().upper(),# SSR/SR/R
            "element": element.strip(),      # Fire/Ice/Wind/Holy/Chaos
            "role": normalize_role(role),    # dps/tank/healer/buffer/debuffer
        }

    if len(result) < 20:
        # 정상이라면 40+가 나와야 함. 너무 적으면 구조 변경 가능성 높음.
        raise RuntimeError(f"파싱 결과 캐릭터 수가 너무 적습니다({len(result)}). 소스 페이지 구조 변경 가능성이 큽니다.")

    return result


def fetch(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NovaSync/1.0; +https://github.com/)"
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)


def main(write: bool):
    html = fetch(SOURCE_URL)
    chars = parse_characters_from_comparison_v2(html)

    payload = {
        "_meta": {
            "game": "zone-nova",
            "source": SOURCE_URL,
            "generated_at": now_iso(),
            "count": len(chars),
        },
        "characters": chars
    }

    if write:
        ensure_parent_dir(OUT_PATH)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"[OK] wrote: {OUT_PATH} (count={len(chars)})")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])  # 미리보기


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write characters_meta.json to repo")
    args = ap.parse_args()
    main(write=args.write)
