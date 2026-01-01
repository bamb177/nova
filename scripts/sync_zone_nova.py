import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]  # /nova
PUBLIC_DATA_DIR = REPO_ROOT / "public" / "data" / "zone-nova"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ===== 사용자 고정 변환 =====
FACTION_NAME_MAP = {
    "A.S.A": "Asa",
    "Bicta Tower": "Bikta",
    "Chemic": "Kemich",
    "Monochrome Nation": "Monochrome Realm",
    "Oduis": "Otis",
    "Pingjing City": "Heikyo Castle",
    "Sapphire": "Safir",
}

# 속성명 변경: Ice -> Frost, Wind -> Storm, Fire -> Blaze
ELEMENT_RENAME = {"Ice": "Frost", "Wind": "Storm", "Fire": "Blaze"}

# class(7) -> role(표시/운영용)
# 요청: Disruptor(=Debuffer class)는 역할에서 DPS로 보이게
CLASS_TO_ROLE = {
    "Warrior": "DPS",
    "Mage": "DPS",
    "Rogue": "DPS",
    "Guardian": "Tank",
    "Healer": "Healer",
    "Buffer": "Buffer",
    "Debuffer": "DPS",  # ✅ 변경
}

def _load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def _save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def title_case(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s[0].upper() + s[1:].lower()

def normalize_rarity(r: str) -> str:
    return (r or "").strip().upper()

def normalize_element(e: str) -> str:
    e = title_case(e)
    return ELEMENT_RENAME.get(e, e)

def normalize_class(c: str) -> str:
    return title_case(c)

def normalize_role(role: str) -> str:
    role = (role or "").strip()
    if not role:
        return ""
    if role.upper() == "DPS":
        return "DPS"
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

        role = normalize_role(class_to_role(cls))

        chars.append({
            "id": cid,
            "name": name,
            "rarity": rarity,
            "element": element,
            "class": cls,
            "role": role,
            "faction": faction,
        })

    # 중복 id 제거
    dedup = {}
    for c in chars:
        dedup[c["id"]] = c
    chars = list(dedup.values())
    chars.sort(key=lambda x: x["id"])

    factions = sorted({c["faction"] for c in chars if c.get("faction")})
    elements = sorted({c["element"] for c in chars if c.get("element")})
    classes = sorted({c["class"] for c in chars if c.get("class")})

    return {
        "last_refresh": _now_iso(),
        "count": len(chars),
        "factions_count": len(factions),
        "factions": factions,
        "elements": elements,
        "classes": classes,
        "characters": chars,
    }

def _norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"[\"`]", "", s)
    s = re.sub(r"[\s\-_]+", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s

def _load_our_char_base():
    """
    우리 레포 기준 캐릭터 리스트를 구성(메타 + characters.json 병합)
    """
    meta_path = PUBLIC_DATA_DIR / "characters_meta.json"
    chars_path = PUBLIC_DATA_DIR / "characters.json"

    our = []

    meta = _load_json(meta_path)
    if isinstance(meta, dict) and isinstance(meta.get("characters"), list):
        our.extend(meta["characters"])

    cj = _load_json(chars_path)
    if isinstance(cj, list):
        our.extend(cj)
    elif isinstance(cj, dict):
        # {characters:[...]} 또는 map 형태
        if isinstance(cj.get("characters"), list):
            our.extend(cj["characters"])
        elif isinstance(cj.get("characters"), dict):
            for k, v in cj["characters"].items():
                if isinstance(v, dict):
                    vv = dict(v)
                    vv.setdefault("id", k)
                    our.append(vv)
        else:
            # map-like
            dict_like = [v for v in cj.values() if isinstance(v, dict)]
            if len(dict_like) >= 3:
                for k, v in cj.items():
                    if isinstance(v, dict):
                        vv = dict(v)
                        vv.setdefault("id", k)
                        our.append(vv)

    # id 없는 경우 방어
    out = []
    seen = set()
    for c in our:
        if not isinstance(c, dict):
            continue
        cid = (c.get("id") or "").strip()
        name = (c.get("name") or "").strip()
        if not cid:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "name": name})
    return out

def _load_overrides_names():
    p = PUBLIC_DATA_DIR / "overrides_names.json"
    d = _load_json(p)
    return d if isinstance(d, dict) else {}

def _guess_name_from_detail(obj: dict, filename: str) -> str:
    """
    gacha-wiki 캐릭터 상세 파일의 name 키가 어떤 형태든 최대한 찾아냄.
    """
    if not isinstance(obj, dict):
        return os.path.splitext(filename)[0]

    for k in ("name", "Name", "character", "Character", "title", "Title"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # 흔한 중첩 구조 방어
    for k in ("details", "info", "profile", "meta"):
        v = obj.get(k)
        if isinstance(v, dict):
            for kk in ("name", "Name", "title", "Title"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()

    return os.path.splitext(filename)[0]

def sync_details_from_gacha_wiki(upstream_char_dir: Path):
    """
    upstream_char_dir = gacha-wiki/src/data/zone-nova/characters
    결과: public/data/zone-nova/characters/<our_id>.json
    """
    overrides = _load_overrides_names()
    our_base = _load_our_char_base()

    # 우리 캐릭터 이름 -> id 인덱스
    name_to_id = {}
    for c in our_base:
        n = _norm_name(c.get("name"))
        if n:
            name_to_id.setdefault(n, []).append(c["id"])

    out_dir = PUBLIC_DATA_DIR / "characters"
    unmatched = []

    for fp in sorted(upstream_char_dir.glob("*.json")):
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            unmatched.append({"file": fp.name, "reason": f"json parse fail: {e}"})
            continue

        upstream_name = _guess_name_from_detail(raw, fp.name)
        canonical_name = overrides.get(upstream_name, upstream_name)  # ✅ 후열(우측) 이름을 canon으로

        # 1) canon name으로 매칭
        key = _norm_name(canonical_name)
        cand = name_to_id.get(key) or []

        # 2) upstream name으로 매칭(보조)
        if not cand:
            key2 = _norm_name(upstream_name)
            cand = name_to_id.get(key2) or []

        # 3) 파일명 base로 매칭(최후 수단)
        if not cand:
            base = os.path.splitext(fp.name)[0]
            key3 = _norm_name(base)
            cand = name_to_id.get(key3) or []

        if not cand:
            unmatched.append({"file": fp.name, "name": upstream_name, "canonical": canonical_name, "reason": "no match"})
            continue

        if len(cand) > 1:
            # 대부분은 단일이어야 정상. 다수면 일단 첫번째 채택하고 리포트
            chosen = cand[0]
            unmatched.append({
                "file": fp.name,
                "name": upstream_name,
                "canonical": canonical_name,
                "reason": f"ambiguous match -> {cand} (picked {chosen})"
            })
        else:
            chosen = cand[0]

        # 저장: 파일명 = 우리 id
        if isinstance(raw, dict):
            raw["id"] = chosen
            raw["name"] = canonical_name
            raw["_synced_at"] = _now_iso()
            raw["_source_repo"] = "boring877/gacha-wiki"
            raw["_source_path"] = "src/data/zone-nova/characters"
            raw["_source_file"] = fp.name
            raw["_source_name"] = upstream_name

        out_path = out_dir / f"{chosen}.json"
        _save_json(out_path, raw)

    # 리포트 저장
    if unmatched:
        _save_json(PUBLIC_DATA_DIR / "_unmatched_gacha_wiki.json", {
            "generated_at": _now_iso(),
            "count": len(unmatched),
            "items": unmatched
        })

    print(f"[ok] details synced -> {out_dir} (unmatched: {len(unmatched)})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write characters_meta.json into public/data/zone-nova")
    ap.add_argument("--upstream", type=str, required=True, help="upstream repo folder name (cloned path)")
    ap.add_argument("--sync-details", action="store_true", help="sync character detail json into public/data/zone-nova/characters/")
    args = ap.parse_args()

    upstream_root = REPO_ROOT / args.upstream
    if not upstream_root.exists():
        raise RuntimeError(f"업스트림 루트가 존재하지 않습니다: {upstream_root}")

    upstream_char_dir = upstream_root / "src" / "data" / "zone-nova" / "characters"
    if not upstream_char_dir.exists():
        raise RuntimeError(f"업스트림 캐릭터 디렉터리가 없습니다: {upstream_char_dir}")

    # 1) meta 생성(기존 기능)
    tmp_out = REPO_ROOT / ".tmp_zone_nova_characters.json"
    run_node_extract(upstream_char_dir, tmp_out)

    raw = json.loads(tmp_out.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("추출 결과 포맷 오류: list여야 합니다.")

    meta = build_characters_meta(raw)

    if args.write:
        PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_meta = PUBLIC_DATA_DIR / "characters_meta.json"
        _save_json(out_meta, meta)

    try:
        tmp_out.unlink()
    except Exception:
        pass

    print(f"[ok] characters_meta.json generated: count={meta['count']} factions={meta.get('factions_count')}")

    # 2) 상세 동기화(신규)
    if args.sync_details:
        sync_details_from_gacha_wiki(upstream_char_dir)

if __name__ == "__main__":
    main()
