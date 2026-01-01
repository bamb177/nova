import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]  # /nova
PUBLIC_DATA_DIR = REPO_ROOT / "public" / "data" / "zone-nova"
SCRIPTS_DIR = REPO_ROOT / "scripts"

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def title_case(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s[0].upper() + s[1:].lower()

# ✅ 캐릭터 이름 오버라이드(동기화해도 원복 방지)
# "전열(업스트림 표기) -> 후열(진짜 표시명)"
NAME_OVERRIDE_MAP = {
    "Greed Mammon": "Mammon",
    "Kela": "Clara",
    "Morgan": "Morgan Le Fay",
    "Leviathan": "Behemoth",
    "Snow Girl": "Yuki-onna",
    "Shanna": "Saya",
    "Naiya": "Naya",
    "Afrodite": "Aphrodite",
    "apep": "Apep",
    "Belphegar": "Belphegor",
    "Chiya": "Cynia",
    "Freye": "Frigga",
    "gaia": "Gaia",
    "Jeanne D Arc": "Joan of Arc",
    "Penny": "Pennie",
    "Yuis": "Zeus",
}

# ✅ 파벌명 고정 변환 (동기화해도 원복 방지)
FACTION_NAME_MAP = {
    "A.S.A": "Asa",
    "Bicta Tower": "Bikta",
    "Chemic": "Kemich",
    "Monochrome Nation": "Monochrome Realm",
    "Oduis": "Otis",
    "Pingjing City": "Heikyo Castle",
    "Sapphire": "Safir",
    # 사용자가 말한 "총 8개" 중 여기 없는 1개는 원문 유지(아래 apply_faction_map에서 그대로 통과)
}

# ✅ class(7) -> role(5) 규칙
# DPS = Warrior/Mage/Rogue
# Guardian -> Tank
# Buffer/Debuffer/Healer -> 동일명 역할
CLASS_TO_ROLE = {
    "Warrior": "DPS",
    "Mage": "DPS",
    "Rogue": "DPS",
    "Guardian": "Tank",
    "Healer": "Healer",
    "Buffer": "Buffer",
    "Debuffer": "Debuffer",
}

def normalize_rarity(r: str) -> str:
    r = (r or "").strip().upper()
    return r

def normalize_element(e: str) -> str:
    # Fire/Wind/Ice/Holy/Chaos 첫글자 대문자
    return title_case(e)

def normalize_class(c: str) -> str:
    # Buffer/Debuffer/Guardian/Healer/Mage/Rogue/Warrior 첫글자 대문자
    return title_case(c)

def normalize_role(role: str) -> str:
    # Healer/DPS/Buffer/Debuffer/Tank
    role = (role or "").strip()
    if not role:
        return ""
    role_up = role.upper()
    if role_up == "DPS":
        return "DPS"
    return title_case(role)

def apply_faction_map(faction: str) -> str:
    f = (faction or "").strip()
    if not f:
        return ""
    return FACTION_NAME_MAP.get(f, f)

def apply_name_override(name: str) -> str:
    """
    업스트림 name을 입력받아, 오버라이드 표시명을 반환.
    - 대소문자/공백 차이를 흡수하기 위해 case-insensitive 매칭 사용
    """
    n = (name or "").strip()
    if not n:
        return ""
    # case-insensitive 매칭
    key = _norm(n)
    # NAME_OVERRIDE_MAP도 동일 정규화로 탐색
    for k, v in NAME_OVERRIDE_MAP.items():
        if _norm(k) == key:
            return v
    return n

def class_to_role(cls: str) -> str:
    c = normalize_class(cls)
    return CLASS_TO_ROLE.get(c, "")

def run_node_extract(upstream_char_dir: Path, out_json: Path):
    extractor = SCRIPTS_DIR / "extract_zone_nova_characters.mjs"
    if not extractor.exists():
        raise RuntimeError(f"extractor 파일이 없습니다: {extractor}")

    cmd = [
        "node",
        str(extractor),
        "--dir", str(upstream_char_dir),
        "--out", str(out_json),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Node 변환 실패:\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}\n"
        )

def build_characters_meta(raw_list: list) -> dict:
    # raw_list: [{id,name,rarity,element,class,faction}, ...]
    chars = []
    for c in raw_list:
        cid = (c.get("id") or "").strip()
        if not cid:
            continue

        # ✅ 업스트림 원본 보존
        name_raw = (c.get("name") or cid).strip()
        # ✅ 표시명 오버라이드(요청 매핑 적용)
        name = apply_name_override(name_raw)

        rarity = normalize_rarity(c.get("rarity") or "")
        element = normalize_element(c.get("element") or "")
        cls = normalize_class(c.get("class") or "")

        # ✅ 파벌도 원본 보존 + 표시용은 맵 적용
        faction_raw = (c.get("faction") or "").strip()
        faction = apply_faction_map(faction_raw)

        role = class_to_role(cls)
        role = normalize_role(role)

        # ✅ 매칭 안정성: 원본/표시명 모두 aliases로 제공
        aliases = []
        for v in [name, name_raw, cid]:
            v = (v or "").strip()
            if v and v not in aliases:
                aliases.append(v)

        chars.append({
            "id": cid,
            "name": name,               # ✅ UI/표시용(오버라이드 적용)
            "name_raw": name_raw,       # ✅ 업스트림 원본 보존(동기화 비교/추적용)
            "aliases": aliases,         # ✅ 매칭 안정화(원본/표시/ID)
            "rarity": rarity,
            "element": element,
            "class": cls,               # ✅ class(7)
            "role": role,               # ✅ role(5)
            "faction": faction,         # ✅ 표시용(맵 적용)
            "faction_raw": faction_raw, # ✅ 업스트림 원본 보존
            # image는 main.py에서 id/name 매핑으로 붙이는 방식이면 여기 없어도 됨
        })

    # 중복 id 제거
    dedup = {}
    for c in chars:
        dedup[c["id"]] = c
    chars = list(dedup.values())
    chars.sort(key=lambda x: x["id"])

    last_refresh = datetime.now(timezone.utc).isoformat()
    factions = sorted({c["faction"] for c in chars if c.get("faction")})
    elements = sorted({c["element"] for c in chars if c.get("element")})
    classes = sorted({c["class"] for c in chars if c.get("class")})

    return {
        "last_refresh": last_refresh,
        "count": len(chars),
        "factions_count": len(factions),
        "factions": factions,
        "elements": elements,
        "classes": classes,
        "characters": chars,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write json into public/data/zone-nova")
    ap.add_argument("--upstream", type=str, required=True, help="upstream repo folder name (cloned path)")
    args = ap.parse_args()

    upstream_root = REPO_ROOT / args.upstream
    if not upstream_root.exists():
        raise RuntimeError(f"업스트림 루트가 존재하지 않습니다: {upstream_root}")

    # ✅ 사용자가 확인한 경로: src/data/zone-nova/characters
    upstream_char_dir = upstream_root / "src" / "data" / "zone-nova" / "characters"
    if not upstream_char_dir.exists():
        raise RuntimeError(f"업스트림 캐릭터 디렉터리가 없습니다: {upstream_char_dir}")

    tmp_out = REPO_ROOT / ".tmp_zone_nova_characters.json"
    run_node_extract(upstream_char_dir, tmp_out)

    raw = json.loads(tmp_out.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("추출 결과 포맷 오류: list여야 합니다.")

    meta = build_characters_meta(raw)

    if args.write:
        PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_meta = PUBLIC_DATA_DIR / "characters_meta.json"
        out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # cleanup
    try:
        tmp_out.unlink()
    except Exception:
        pass

    print(f"[ok] characters_meta.json generated: count={meta['count']} factions={meta.get('factions_count')}")

if __name__ == "__main__":
    main()
