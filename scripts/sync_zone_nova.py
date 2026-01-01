import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]  # /nova
PUBLIC_DATA_DIR = REPO_ROOT / "public" / "data" / "zone-nova"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ===== 사용자 고정 오버라이드 =====

# ✅ 이름 고정 변환(동기화해도 원복 방지)
# - 키는 normalize_name(원본 name) 기준
NAME_DISPLAY_MAP = {
    "greed mammon": "Mammon",
    "kela": "Clara",
    "morgan": "Morgan Le Fay",
    "leviathan": "Behemoth",
    "snow girl": "Yuki-onna",
    "shanna": "Saya",
    "naiya": "Naya",
    "afrodite": "Aphrodite",
    "apep": "Apep",
    "belphegar": "Belphegor",
    "chiya": "Cynia",
    "freye": "Frigga",
    "gaia": "Gaia",
    "jeanne d arc": "Joan of Arc",
    "penny": "Pennie",
    "yuis": "Zeus",
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
    # 총 8개 중 여기 없는 1개는 원문 유지
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

def normalize_name(name: str) -> str:
    name = (name or "").replace("’", "'").strip()
    name = " ".join(name.split())
    return name

def normalize_rarity(r: str) -> str:
    return (r or "").strip().upper()

def normalize_element(e: str) -> str:
    # Fire/Wind/Ice/Holy/Chaos 첫글자 대문자
    return title_case(e)

def normalize_class(c: str) -> str:
    # Buffer/Debuffer/Guardian/Healer/Mage/Rogue/Warrior 첫글자 대문자
    s = title_case(c)
    # 오타 보정
    if s.lower() == "debeffer":
        return "Debuffer"
    return s

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

def apply_name_map(name: str) -> str:
    nm = normalize_name(name)
    key = nm.lower()
    return NAME_DISPLAY_MAP.get(key, nm)

def class_to_role(cls: str) -> str:
    c = normalize_class(cls)
    return CLASS_TO_ROLE.get(c, "")

def run_node_extract(upstream_char_dir: Path, out_json: Path):
    extractor = SCRIPTS_DIR / "extract_zone_nova_characters.mjs"
    if not extractor.exists():
        raise RuntimeError(f"extractor 파일이 없습니다: {extractor}")

    cmd = ["node", str(extractor), "--dir", str(upstream_char_dir), "--out", str(out_json)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Node 변환 실패:\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}\n"
        )

def _load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def _as_list(raw):
    """
    characters.json 또는 characters_meta.json 등 다양한 포맷을 list[dict]로 정규화
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        if isinstance(raw.get("characters"), list):
            return [x for x in raw["characters"] if isinstance(x, dict)]
        # {id: {...}} 형태
        out = []
        for k, v in raw.items():
            if isinstance(v, dict):
                item = dict(v)
                if "id" not in item:
                    item["id"] = k
                out.append(item)
        if len(out) >= 1:
            return out
    return []

def build_characters_meta(raw_list: list, local_overrides: list) -> dict:
    """
    raw_list: upstream 추출 결과 list
    local_overrides: public/data/zone-nova/characters.json (수동 보강/누락 보완)
      - upstream에 없는 캐릭터(Apep/Gaia 등)는 local에서 추가됨
      - 동일 id가 있으면 local 값을 우선(필드별 덮어쓰기)
    """
    # 1) upstream 정규화
    chars = []
    for c in raw_list:
        cid = (c.get("id") or "").strip()
        if not cid:
            continue

        name = apply_name_map(c.get("name") or cid)
        rarity = normalize_rarity(c.get("rarity") or "")
        element = normalize_element(c.get("element") or "")
        cls = normalize_class(c.get("class") or "")
        faction = apply_faction_map(c.get("faction") or "")

        role = normalize_role(class_to_role(cls))

        chars.append({
            "id": cid,
            "name": name,
            "rarity": rarity,
            "element": element,
            "class": cls,       # class(7)
            "role": role,       # role(5)
            "faction": faction, # faction(8)
        })

    by_id = {c["id"]: c for c in chars}

    # 2) local overrides 병합
    for ov in local_overrides:
        if not isinstance(ov, dict):
            continue
        cid = (ov.get("id") or "").strip()
        if not cid:
            continue

        # local 데이터도 동일 정규화 적용
        name = apply_name_map(ov.get("name") or cid)
        rarity = normalize_rarity(ov.get("rarity") or "")
        element = normalize_element(ov.get("element") or "")
        cls = normalize_class(ov.get("class") or "")
        faction = apply_faction_map(ov.get("faction") or "")

        role = normalize_role(ov.get("role") or class_to_role(cls) or "")

        item = {
            "id": cid,
            "name": name,
            "rarity": rarity,
            "element": element,
            "class": cls,
            "role": role,
            "faction": faction,
        }

        if cid in by_id:
            # 필드 단위로 local이 비어있지 않으면 덮어쓰기
            for k, v in item.items():
                if v not in (None, "", "-"):
                    by_id[cid][k] = v
        else:
            by_id[cid] = item

    merged = list(by_id.values())
    merged.sort(key=lambda x: (x.get("id") or ""))

    last_refresh = datetime.now(timezone.utc).isoformat()
    factions = sorted({c["faction"] for c in merged if c.get("faction")})
    elements = sorted({c["element"] for c in merged if c.get("element")})
    classes = sorted({c["class"] for c in merged if c.get("class")})

    return {
        "last_refresh": last_refresh,
        "count": len(merged),
        "factions_count": len(factions),
        "factions": factions,
        "elements": elements,
        "classes": classes,
        "characters": merged,
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

    raw = _load_json(tmp_out)
    if not isinstance(raw, list):
        raise RuntimeError("추출 결과 포맷 오류: list여야 합니다.")

    # local overrides (Apep/Gaia 등 보강)
    local_path = PUBLIC_DATA_DIR / "characters.json"
    local_raw = _load_json(local_path)
    local_list = _as_list(local_raw)

    meta = build_characters_meta(raw, local_list)

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
