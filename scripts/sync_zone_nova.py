import os
import re
import json
import time
import argparse
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests
from bs4 import BeautifulSoup


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE_DIR, "public", "data", "zone-nova", "characters_meta.json")

# 1차(일괄 파싱) / 2차(목록+개별페이지)
URL_COMPARISON_V2 = "https://gachawiki.info/guides/zone-nova/character-comparison-v2/"
URL_CHAR_INDEX = "https://gachawiki.info/guides/zone-nova/characters/"


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


def http_get(url: str, timeout: int = 35) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NovaSync/1.0; +https://github.com/)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def pick_value(lines: List[str], key: str) -> Optional[str]:
    for i, l in enumerate(lines):
        if l == key and i + 1 < len(lines):
            return lines[i + 1]
    return None


def parse_from_web_comparison_v2(html: str) -> Dict[str, Dict[str, str]]:
    """
    character-comparison-v2 페이지는 대개 텍스트에
    ### Name / Rarity / Element / Role 구조가 들어있음
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    chunks = text.split("\n### ")
    if len(chunks) < 2:
        raise RuntimeError("comparison-v2 구조에서 '###' 패턴을 찾지 못했습니다(페이지 구조 변경/차단 가능).")

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
        # Jeanne D Arc 통일
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
        raise RuntimeError(f"comparison-v2 파싱 결과가 너무 적습니다({len(result)}).")
    return result


def extract_dt_dd(soup: BeautifulSoup, key: str) -> Optional[str]:
    """
    DT/DD 구조에서 key에 해당하는 값을 찾음
    """
    dts = soup.find_all(["dt", "th"])
    for dt in dts:
        if not dt.get_text(strip=True):
            continue
        if dt.get_text(strip=True).lower() == key.lower():
            # dt 다음 dd 혹은 같은 row의 td
            dd = dt.find_next_sibling(["dd", "td"])
            if dd:
                v = dd.get_text(" ", strip=True)
                return v if v else None
    return None


def extract_from_table_rows(soup: BeautifulSoup, key: str) -> Optional[str]:
    """
    표(tr) 기반에서 th=key인 td를 찾음
    """
    for tr in soup.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if not th or not td:
            continue
        if th.get_text(strip=True).lower() == key.lower():
            v = td.get_text(" ", strip=True)
            return v if v else None
    return None


def parse_character_detail(url: str) -> Optional[Tuple[str, str, str, str]]:
    """
    개별 캐릭터 페이지에서 (id, name, rarity, element, role)을 추출.
    실패하면 None.
    """
    html = http_get(url)
    soup = BeautifulSoup(html, "lxml")

    # 이름: h1 우선
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else ""
    if not name:
        # title fallback
        title = soup.find("title")
        name = title.get_text(" ", strip=True) if title else ""
        name = name.replace(" - GachaWiki", "").strip()

    name = name.replace("’", "'").strip()
    if not name:
        return None

    rarity = (
        extract_dt_dd(soup, "Rarity")
        or extract_from_table_rows(soup, "Rarity")
    )
    element = (
        extract_dt_dd(soup, "Element")
        or extract_from_table_rows(soup, "Element")
    )
    role = (
        extract_dt_dd(soup, "Role")
        or extract_from_table_rows(soup, "Role")
        or extract_dt_dd(soup, "Class")
        or extract_from_table_rows(soup, "Class")
    )

    if not (rarity and element):
        # 텍스트 라인 기반 마지막 보정
        txt = soup.get_text("\n")
        lines = [x.strip() for x in txt.split("\n") if x.strip()]
        rarity = rarity or pick_value(lines, "Rarity")
        element = element or pick_value(lines, "Element")
        role = role or pick_value(lines, "Role") or pick_value(lines, "Class")

    if not (rarity and element and role):
        return None

    sid = slug_id(name)
    if sid in {"joanofarc", "jeannedarc"} or "jeanne" in sid:
        sid = "jeannedarc"
        name = "Jeanne D Arc"

    return sid, name, rarity.strip().upper(), element.strip(), normalize_role(role)


def parse_from_char_index_and_details(index_html: str, max_sleep: float = 0.12) -> Dict[str, Dict[str, str]]:
    """
    characters/ 목록 페이지에서 캐릭터 링크를 수집하고,
    각 캐릭터 페이지를 순회하며 rarity/element/role을 수집.
    """
    soup = BeautifulSoup(index_html, "lxml")

    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # 절대/상대 모두 처리
        if href.startswith("/"):
            href = "https://gachawiki.info" + href
        if not href.startswith("https://gachawiki.info/guides/zone-nova/characters/"):
            continue
        # 인덱스 자체 제외
        if href.rstrip("/") == URL_CHAR_INDEX.rstrip("/"):
            continue
        links.add(href.rstrip("/") + "/")

    links = sorted(links)
    if len(links) < 10:
        raise RuntimeError(f"characters 인덱스에서 링크를 충분히 수집하지 못했습니다({len(links)}).")

    result: Dict[str, Dict[str, str]] = {}
    fails: List[str] = []

    for i, url in enumerate(links, start=1):
        try:
            parsed = parse_character_detail(url)
            if not parsed:
                fails.append(url)
                continue
            sid, name, rarity, element, role = parsed
            result[sid] = {"name": name, "rarity": rarity, "element": element, "role": role}
        except Exception:
            fails.append(url)

        # 과도한 요청 방지(가볍게)
        time.sleep(max_sleep)

    if len(result) < 20:
        raise RuntimeError(f"개별 페이지 파싱 결과가 너무 적습니다({len(result)}). 실패 예시: {fails[:5]}")

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write characters_meta.json to repo")
    args = ap.parse_args()

    # 예시 데이터 생성은 절대 하지 않음.
    # 항상 원문(웹)에서 파싱해 저장하는 방식만 사용.
    meta: Dict[str, Dict[str, str]] = {}
    source = ""

    # 1차: comparison-v2
    try:
        html = http_get(URL_COMPARISON_V2)
        meta = parse_from_web_comparison_v2(html)
        source = f"web:{URL_COMPARISON_V2}"
        print(f"[OK] comparison-v2 parsed count={len(meta)}")
    except Exception as e1:
        print(f"[WARN] comparison-v2 failed: {e1}")

        # 2차: characters index + details
        html = http_get(URL_CHAR_INDEX)
        meta = parse_from_char_index_and_details(html)
        source = f"web:{URL_CHAR_INDEX} (details)"
        print(f"[OK] index+details parsed count={len(meta)}")

    payload = {
        "_meta": {
            "game": "zone-nova",
            "source": source,
            "generated_at": now_iso(),
            "count": len(meta),
        },
        "characters": meta
    }

    if args.write:
        ensure_parent_dir(OUT_PATH)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"[OK] wrote: {OUT_PATH}")
        print(f"[OK] count={len(meta)} source={source}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    main()
