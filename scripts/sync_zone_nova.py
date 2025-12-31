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


def iso_now():
    # KST 고정이 꼭 필요하면 여기서 +09:00로 바꿔도 됨
    return datetime.now(timezone.utc).astimezone().isoformat()


def has_js_files(p: Path) -> bool:
    if not p.exists() or not p.is_dir():
        return False
    for child in p.iterdir():
        if child.is_file() and child.suffix.lower() == ".js" and child.name.lower() != "index.js":
            return True
    return False


def find_zone_nova_characters_dir(upstream_root: Path) -> Path:
    """
    Upstream structure may change. We search robustly.
    Priority:
      1) known canonical paths
      2) heuristic walk under upstream_root/src looking for *zone-nova*/*zone_nova* and *characters* directory
    """
    candidates = [
        upstream_root / "src" / "data" / "zone-nova" / "characters",
        upstream_root / "src" / "data" / "zone_nova" / "characters",
        upstream_root / "src" / "data" / "zone-nova" / "character",
        upstream_root / "src" / "data" / "zone_nova" / "character",
    ]
    for c in candidates:
        if has_js_files(c):
            return c

    # heuristic scan
    src_root = upstream_root / "src"
    if not src_root.exists():
        raise RuntimeError(f"업스트림 src 디렉터리를 찾지 못했습니다: {src_root}")

    best = None
    for root, dirs, files in os.walk(src_root):
        rp = Path(root)
        lower_parts = [x.lower() for x in rp.parts]
        # zone-nova/zone_nova 포함 & characters/character 디렉터리 후보
        if ("zone-nova" in lower_parts or "zone_nova" in lower_parts) and (
            rp.name.lower() in ("characters", "character")
        ):
            if any(f.lower().endswith(".js") and f.lower() != "index.js" for f in files):
                best = rp
                break

    if best:
        return best

    raise RuntimeError(
        "업스트림에서 zone-nova 캐릭터 디렉터리를 찾지 못했습니다. "
        "업스트림 경로가 변경된 것으로 보입니다."
    )


def run_node_extract(input_dir: Path, out_file: Path):
    if not NODE_EXTRACTOR.exists():
        raise RuntimeError(f"노드 추출기 파일이 없습니다: {NODE_EXTRACTOR}")

    cmd = [
        "node",
        str(NODE_EXTRACTOR),
        "--input-dir",
        str(input_dir),
        "--out",
        str(out_file),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Node 변환 실패:\n"
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n"
        )


def build_meta(char_list: list[dict]) -> dict:
    """
    characters_meta.json: UI/추천 엔진이 바로 쓰기 쉬운 형태로 정규화.
    - classes는 'class' 필드로 저장 (사용자 요구)
    - role은 파생값(버튼/필터용). 필요없으면 추후 제거 가능.
    """
    out_chars = []
    for c in char_list:
        name = (c.get("name") or "").strip()
        cid = (c.get("id") or "").strip()
        cls = (c.get("class") or "").strip()
        rarity = (c.get("rarity") or "").strip()
        element = (c.get("element") or "").strip()
        role = (c.get("role") or "").strip()
        tank_capable = bool(c.get("tank_capable", False))

        # image 처리:
        # - 업스트림에서 image를 줬으면 그대로 사용
        # - 없으면 (name 기반 jpg)로 기본값. 실제 파일명 예외(Snow/Morgan 등)는 후속 단계에서 별도 맵으로 보정 가능
        img = (c.get("image") or "").strip()
        if not img:
            # 기본 규칙: 공백 제거한 형태를 쓰지 않고, 기존 시스템과 충돌이 많아 name을 그대로 쓰지 않음
            # 현재는 id 기반으로 우선 추정
            img = f"{cid}.jpg" if cid else ""

        out_chars.append(
            {
                "id": cid,
                "name": name,
                "rarity": rarity,
                "element": element,
                "class": cls,          # 핵심: class 기반
                "role": role,          # 파생(필터/추천용)
                "tank_capable": tank_capable,
                "image": img,          # UI 이미지 매칭용
            }
        )

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

    # 1) find actual characters dir
    char_dir = find_zone_nova_characters_dir(upstream_root)

    # 2) node extract -> characters.json
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_node_extract(char_dir, CHAR_JSON)

    # 3) build characters_meta.json in required format
    char_list = json.loads(CHAR_JSON.read_text(encoding="utf-8"))
    if not isinstance(char_list, list):
        raise RuntimeError("characters.json 포맷 오류: list 여야 합니다.")

    meta = build_meta(char_list)

    if args.write:
        META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("OK")
    print(f"- upstream: {upstream_root}")
    print(f"- detected dir: {char_dir}")
    print(f"- characters.json: {CHAR_JSON} ({len(char_list)})")
    print(f"- characters_meta.json: {META_JSON} ({meta['count']})")


if __name__ == "__main__":
    main()
