import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "public" / "data" / "zone-nova"
CHAR_JSON = OUT_DIR / "characters.json"
META_JSON = OUT_DIR / "characters_meta.json"

NODE_EXTRACTOR = REPO_ROOT / "scripts" / "extract_zone_nova_characters.mjs"

# ===== 이름 매핑 (gacha-wiki -> 실제 게임 표시명) =====
# "후열(-> 오른쪽)"이 진짜 이름이므로, 이 값으로 name을 표시하고 이미지 매칭도 이 이름 기준으로 진행
NAME_OVERRIDES = {
    "Greed Mammon": "Mammon",
    "GreedMammon": "Mammon",
    "Mammon": "Mammon",

    "Kela": "Clara",
    "Clara": "Clara",

    "Morgan": "Morgan Le Fay",
    "MorganLeFay": "Morgan Le Fay",
    "Morgan Le Fay": "Morgan Le Fay",

    "Leviathan": "Behemoth",
    "Behemoth": "Behemoth",

    "Snow Girl": "Yuki-onna",
    "SnowGirl": "Yuki-onna",
    "Yuki-onna": "Yuki-onna",

    "Shanna": "Saya",
    "Saya": "Saya",
}

# Apep 표기(표시명만 Title Case로)
DISPLAY_NAME_CASE_FIX = {
    "apep": "Apep",
}

# ===== 표기 규칙: element / class / role =====
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

# Apep Tank 고정은 취소(요청 반영): 별도 override 없음


def iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def title_first(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s[:1].upper() + s[1:].lower()


def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize_class(cls: str) -> str:
    cls = (cls or "").strip()
    if not cls:
        return ""
    key = cls.lower()

    alias = {
        "guard": "guardian",
        "support": "buffer",
        "debuff": "debuffer",
    }
    key = alias.get(key, key)
    return CLASS_CANON.get(key, title_first(cls))


def derive_role(class_title: str) -> str:
    return CLASS_TO_ROLE.get(class_title, "")


def has_js_files(p: Path) -> bool:
    if not p.exists() or not p.is_dir():
        return False
    for child in p.iterdir():
        if child.is_file() and child.suffix.lower() == ".js" and child.name.lower() != "index.js":
            return True
    return False


def find_zone_nova_characters_dir(upstream_root: Path) -> Path:
    candidates = [
        upstream_root / "src" / "data" / "zone-nova" / "characters",
        upstream_root / "src" / "data" / "zone_nova" / "characters",
        upstream_root / "src" / "data" / "zone-nova" / "character",
        upstream_root / "src" / "data" / "zone_nova" / "character",
    ]
    for c in candidates:
        if has_js_files(c):
            return c

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


def apply_name_override(name: str, char_id: str) -> str:
    name = normalize_spaces(name)

    # id 기반 케이스 보정(Apep)
    if char_id and char_id.lower() in DISPLAY_NAME_CASE_FIX:
        return DISPLAY_NAME_CASE_FIX[char_id.lower()]

    # 여러 형태로 매핑 시도
    if name in NAME_OVERRIDES:
        return NAME_OVERRIDES[name]

    compact = name.replace(" ", "")
    if compact in NAME_OVERRIDES:
        return NAME_OVERRIDES[compact]

    # 기본: 그대로
    return name


def find_image_filename(char_img_dir: Path, candidates: list[str]) -> str:
    """
    public/images/games/zone-nova/characters 안에서
    후보 이름들에 매칭되는 실제 파일명을 찾는다.
    - 확장자: jpg/png/jpeg/webp
    - 공백/하이픈/언더스코어 변형도 체크
    """
    if not char_img_dir.exists():
        return ""

    exts = [".jpg", ".png", ".jpeg", ".webp", ".JPG", ".PNG", ".JPEG", ".WEBP"]

    def variants(base: str) -> list[str]:
        base = normalize_spaces(base)
        v = set()
        v.add(base)
        v.add(base.replace(" ", ""))
        v.add(base.replace(" ", "-"))
        v.add(base.replace(" ", "_"))
        v.add(base.lower())
        v.add(base.lower().replace(" ", ""))
        v.add(base.lower().replace(" ", "-"))
        v.add(base.lower().replace(" ", "_"))
        return list(v)

    for cand in candidates:
        if not cand:
            continue
        for b in variants(cand):
            for ext in exts:
                fp = char_img_dir / f"{b}{ext}"
                if fp.exists():
                    return fp.name

    return ""


def build_meta(char_list: list[dict]) -> dict:
    char_img_dir = REPO_ROOT / "public" / "images" / "games" / "zone-nova" / "characters"
    out_chars = []

    for c in char_list:
        cid = (c.get("id") or "").strip()
        raw_name = (c.get("name") or "").strip()

        # 1) 표시명 보정(실제 게임명)
        display_name = apply_name_override(raw_name, cid)

        # 2) element: 첫 글자 대문자
        element_raw = (c.get("element") or "").strip()
        element = title_first(element_raw)

        # 3) class: 7개 표준화(첫 글자 대문자 포함)
        class_raw = (c.get("class") or c.get("Class") or "").strip()
        class_title = normalize_class(class_raw)

        # 4) role: class 기반 파생(요청 표기: Healer/DPS/Buffer/Debuffer/Tank)
        role = derive_role(class_title)
        if role == "Dps":
            role = "DPS"
        if role and role not in ROLE_CANON:
            # 강제 보정(예외 방지)
            if role.lower() == "dps":
                role = "DPS"
            else:
                role = title_first(role)

        # 5) rarity: SSR/SR 등급 그대로 (대문자)
        rarity = (c.get("rarity") or "").strip().upper()

        # 6) image: 변경된 이름 기준으로 실제 파일명 탐색
        #    우선순위: display_name -> raw_name -> id
        img_filename = find_image_filename(char_img_dir, [display_name, raw_name, cid])
        img_url = f"/images/games/zone-nova/characters/{img_filename}" if img_filename else ""

        out_chars.append({
            "id": cid,
            "name": display_name,     # 화면에 보일 이름(실제 게임명)
            "name_src": raw_name,     # (선택) 원본명(디버그용) - 원하면 추후 삭제 가능
            "rarity": rarity,
            "element": element,
            "class": class_title,     # Buffer/Debuffer/Guardian/Healer/Mage/Rogue/Warrior
            "role": role,             # Healer/DPS/Buffer/Debuffer/Tank
            "image": img_url,         # 항상 URL 형태로 통일
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
    if not upstream_root.exists():
        raise RuntimeError(f"업스트림 루트가 존재하지 않습니다: {upstream_root}")

    char_dir = find_zone_nova_characters_dir(upstream_root)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) upstream -> characters.json
    run_node_extract(char_dir, CHAR_JSON)

    # 2) characters.json -> characters_meta.json
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
