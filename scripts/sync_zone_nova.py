import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]  # /nova
PUBLIC_DATA_DIR = REPO_ROOT / "public" / "data" / "zone-nova"
SCRIPTS_DIR = REPO_ROOT / "scripts"

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "public" / "data" / "zone-nova"

NAMES_OVR_PATH = DATA_DIR / "overrides_names.json"
FACTIONS_OVR_PATH = DATA_DIR / "overrides_factions.json"
META_PATH = DATA_DIR / "characters_meta.json"

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

def title_case(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s[0].upper() + s[1:].lower()

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
    # 이미 규정된 형태로만
    role_up = role.upper()
    if role_up == "DPS": return "DPS"
    return title_case(role)

def apply_faction_map(faction: str) -> str:
    f = (faction or "").strip()
    if not f:
        return ""
    return FACTION_NAME_MAP.get(f, f)

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

        name = (c.get("name") or cid).strip()
        rarity = normalize_rarity(c.get("rarity") or "")
        element = normalize_element(c.get("element") or "")
        cls = normalize_class(c.get("class") or "")
        faction = apply_faction_map(c.get("faction") or "")

        role = class_to_role(cls)
        role = normalize_role(role)

        chars.append({
            "id": cid,
            "name": name,
            "rarity": rarity,
            "element": element,
            "class": cls,       # ✅ class(7)
            "role": role,       # ✅ role(5)
            "faction": faction, # ✅ faction(8)
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

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def apply_overrides(characters: list[dict]) -> list[dict]:
    name_ovr_raw = _load_json(NAMES_OVR_PATH)
    faction_ovr_raw = _load_json(FACTIONS_OVR_PATH)

    # case-insensitive 매칭을 위해 정규화 맵 생성
    name_ovr = {_norm(k): v for k, v in name_ovr_raw.items()}
    faction_ovr = {_norm(k): v for k, v in faction_ovr_raw.items()}

    for ch in characters:
        # 원본 이름(업스트림 값) 기준으로 오버라이드 적용
        src_name = ch.get("name") or ch.get("name_en") or ch.get("title") or ""
        src_faction = ch.get("faction") or ""

        # 표시용 필드에만 반영 (ID/slug 등 내부키는 유지)
        display_name = name_ovr.get(_norm(src_name))
        if display_name:
            ch["display_name"] = display_name
        else:
            # 오버라이드 없으면 display_name을 원본으로 채워 UI에서 일관되게 사용 가능
            ch.setdefault("display_name", src_name)

        display_faction = faction_ovr.get(_norm(src_faction))
        if display_faction:
            ch["faction_display"] = display_faction
        else:
            ch.setdefault("faction_display", src_faction)

    return characters


def main():
    # 1) 여기에서 업스트림 데이터 받아와서 characters 리스트 만든 뒤 (기존 로직)
    # characters = fetch_and_build_characters()

    # 2) 마지막에 무조건 오버라이드 적용
    characters = apply_overrides(characters)

    # 3) 저장
    with META_PATH.open("w", encoding="utf-8") as f:
        json.dump(characters, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
