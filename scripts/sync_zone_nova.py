import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "public" / "data" / "zone-nova"
CHAR_JSON = OUT_DIR / "characters.json"
META_JSON = OUT_DIR / "characters_meta.json"

NODE_EXTRACTOR = REPO_ROOT / "scripts" / "extract_zone_nova_characters.mjs"


# ===== 표준화 규칙 =====
CLASS_CANON = {
    "buffer": "Buffer",
    "debuffer": "Debuffer",
    "guardian": "Guardian",
    "healer": "Healer",
    "mage": "Mage",
    "rogue": "Rogue",
    "warrior": "Warrior",
}

ROLE_CANON = {"Healer", "DPS", "Buffer", "Debuffer", "Tank"}

# class(7) -> role(5)
CLASS_TO_ROLE = {
    "Buffer": "Buffer",
    "Debuffer": "Debuffer",
    "Healer": "Healer",
    "Guardian": "Tank",
    "Mage": "DPS",
    "Rogue": "DPS",
    "Warrior": "DPS",
}

# 예외: Apep은 Warrior지만 Tank 가능 -> 여기서는 role 자체를 Tank로 고정(요청사항)
SPECIAL_ROLE_OVERRIDE = {
    "apep": "Tank"
}


def iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def title_first(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s[:1].upper() + s[1:].lower()


def normalize_class(cls: str) -> str:
    cls = (cls or "").strip()
    if not cls:
        return ""
    key = cls.lower()
    # 자주 섞이는 별칭 보정
    alias = {
        "guard": "guardian",
        "support": "buffer",
        "debuff": "debuffer",
    }
    key = alias.get(key, key)
    return CLASS_CANON.get(key, title_first(cls))


def derive_role(char_id: str, class_title: str) -> str:
    cid = (char_id or "").strip().lower()
    if cid in SPECIAL_ROLE_OVERRIDE:
        return SPECIAL_ROLE_OVERRIDE[cid]
    return CLASS_TO_ROLE.get(class_title, "")


def has_js_files(p: Path) -> bool:
    if not p.exists() or not p.is_dir():
        return False
    for child in p.iterdir():
        if child.is_file() and child.suffix.lower() == ".js" and child.name.lower() != "index.js":
            return True
    return False


def find_zone_nova_characters_dir(upstream_root: Path) -> Path:
    # 1) 대표 경로 우선
    candidates = [
        upstream_root / "src" / "data" / "zone-nova" / "characters",
        upstream_root / "src" / "data" / "zone_nova" / "characters",
        upstream_root / "src" / "data" / "zone-nova" / "character",
        upstream_root / "src" / "data" / "zone_nova" / "character",
    ]
    for c in candidates:
        if has_js_files(c):
            return c

    # 2) 스캔(업스트림 구조 변경 대비)
    src_root = upstream_root / "src"
    if not src_root.exists():
        raise RuntimeError(f"업스트림 src 디렉터리를 찾지 못했습니다: {src_root}")

    for root, dirs, files in os.walk(src_root):
        rp = Path(root)
        parts = [x.lower() for x in rp.parts]
        if ("zone-nova" in parts or "zone_nova" in parts) and rp.name.lower() in ("characters", "character"):
            if any(f.lower().endswith(".js") and f.lower() != "index.js" for f in files):
                return rp

    raise RuntimeError("업스트림에서 zone-nova 캐릭터 디렉터리를 찾지 못했습니다.")


def run_node_extract(input_dir: Path, out_file: Path):
    if not NODE_EXTRACTOR.exists():
        raise RuntimeError(f"노드 추출기 파일이 없습니다: {NODE_EXTRACTOR}")

    cmd = ["node", str(NODE_EXTRACTOR), "--input-dir", str(input_dir), "--out", str(out_file)]
    proc = subprocess.run(cmd, capture_output=True, text=True)

    if proc.returncode != 0:
        raise RuntimeError(
            "Node 변환 실패:\n"
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n"
        )


def build_meta(char_list: list[dict]) -> dict:
    out_chars = []

    for c in char_list:
        cid = (c.get("id") or "").strip()
        name = (c.get("name") or "").strip()

        # 표기 규칙: element/classes 첫 글자 대문자
        element_raw = (c.get("element") or "").strip()
        element = title_first(element_raw)

        # class는 업스트림 키가 class라고 하셨으므로 class 우선
        class_raw = (c.get("class") or c.get("Class") or "").strip()
        class_title = normalize_class(class_raw)

        rarity = (c.get("rarity") or "").strip().upper()

        # role은 class에서 파생(원본 role 무시)
        role = derive_role(cid, class_title)

        # role 표기 규칙(Healer/DPS/Buffer/Debuffer/Tank)
        if role and role not in ROLE_CANON:
            # 예외 없이 ROLE_CANON으로 강제
            role = role[:1].upper() + role[1:].lower()
            if role == "Dps":
                role = "DPS"

        img = (c.get("image") or "").strip()

        out_chars.append({
            "id": cid,
            "name": name,
            "rarity": rarity,
            "element": element,
            "class": class_title,  # 7개
            "role": role,          # 5개
            "image": img,
        })

    return {
        "game": "zone-nova",
        "last_refresh": iso_now(),
        "count": len(out_chars),
        "characters": out_chars,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True, help="Upstream repo root path (cloned)")
    ap.add_argument("--write", action="store_true", help="Write outputs to public/data/zone-nova")
    args = ap.parse_args()

    upstream_root = Path(args.upstream).resolve()

    # 여기서 죽는 이유는 대부분 워크플로 clone 경로가 다르기 때문
    if not upstream_root.exists():
        raise RuntimeError(f"업스트림 루트가 존재하지 않습니다: {upstream_root}")

    char_dir = find_zone_nova_characters_dir(upstream_root)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 업스트림 캐릭터 .js들 -> characters.json
    run_node_extract(char_dir, CHAR_JSON)

    # 2) characters.json -> characters_meta.json (class 기반 role 생성 + 표기 규칙 적용)
    char_list = json.loads(CHAR_JSON.read_text(encoding="utf-8"))
    if not isinstance(char_list, list):
        raise RuntimeError("characters.json 포맷 오류: list 여야 합니다.")

    meta = build_meta(char_list)

    if args.write:
        META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("OK")
    print(f"- detected char dir: {char_dir}")
    print(f"- wrote: {CHAR_JSON} ({len(char_list)})")
    print(f"- wrote: {META_JSON} ({meta['count']})")


if __name__ == "__main__":
    main()
