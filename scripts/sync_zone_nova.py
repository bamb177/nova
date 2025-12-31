# scripts/sync_zone_nova.py
import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "public" / "data" / "zone-nova"
CHAR_JSON = DATA_DIR / "characters.json"
CHAR_META_JSON = DATA_DIR / "characters_meta.json"

UPSTREAM_CHAR_JS = Path("src/data/zone-nova/characters.js")

CLASS_SET = {"buffer", "debuffer", "guardian", "healer", "mage", "rogue", "warrior"}
ROLE_SET = {"buffer", "dps", "debuffer", "healer", "tank"}

CLASS_TO_ROLE = {
    "buffer": "buffer",
    "debuffer": "debuffer",
    "healer": "healer",
    "guardian": "tank",
    "mage": "dps",
    "rogue": "dps",
    "warrior": "dps",
}

# Apep 예외: warrior 클래스여도 tank 역할 가능(요청사항 반영)
SPECIAL_ROLE_OVERRIDES = {
    "apep": "tank",
}

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def run_node_extract(upstream_root: Path, out_file: Path):
    """
    업스트림 characters.js -> characters.json 변환
    """
    script = ROOT / "scripts" / "extract_zone_nova_characters.mjs"
    if not script.exists():
        raise RuntimeError(f"extract 스크립트를 찾지 못했습니다: {script}")

    js_path = upstream_root / UPSTREAM_CHAR_JS
    if not js_path.exists():
        raise RuntimeError(f"업스트림 파일을 찾지 못했습니다: {js_path}")

    out_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "node",
        str(script),
        "--upstream",
        str(upstream_root),
        "--out",
        str(out_file),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Node 변환 실패:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

    print(proc.stdout.strip())


def normalize_class(v) -> str:
    """
    원본 characters.js 안의 class/classes/job 등 어느 키로 오든 class(7)로 정규화
    """
    if v is None:
        return "-"
    s = str(v).strip()
    if not s:
        return "-"

    low = s.lower()

    alias = {
        "guard": "guardian",
        "guardian": "guardian",
        "healer": "healer",
        "buffer": "buffer",
        "support": "buffer",
        "debuffer": "debuffer",
        "mage": "mage",
        "rogue": "rogue",
        "warrior": "warrior",
        # 잘못 들어온 경우 보정(원본에서 role을 class로 잘못 쓰는 상황 대비)
        "tank": "guardian",
        "dps": "warrior",
    }

    if low in CLASS_SET:
        return low
    if low in alias:
        return alias[low]
    return "-"


def role_from_class(cls: str, cid: str) -> str:
    if not cls or cls == "-":
        return "-"
    cid = (cid or "").strip().lower()
    if cid in SPECIAL_ROLE_OVERRIDES:
        return SPECIAL_ROLE_OVERRIDES[cid]
    return CLASS_TO_ROLE.get(cls, "-")


def load_characters_json(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("characters"), list):
        return raw["characters"]
    raise RuntimeError("characters.json 포맷 오류: list 또는 {characters:[...]} 이어야 합니다.")


def build_char_meta(chars: list[dict]) -> dict:
    """
    출력: { characters: [ ... ] }
    - class(7) 기준으로 role(5) 계산
    """
    out = []
    seen = set()

    for c in chars:
        if not isinstance(c, dict):
            continue

        name = (c.get("name") or "").strip()
        cid = c.get("id") or c.get("_id") or ""
        cid = slug_id(cid) if cid else slug_id(name)

        if not cid or cid in seen:
            continue
        seen.add(cid)

        # Jeanne D Arc 통일(기존 혼선 방지)
        if slug_id(name) in {"jeannedarc", "joanofarc"} or cid in {"joanofarc"} or "jeanne" in cid:
            cid = "jeannedarc"
            name = "Jeanne D Arc"

        rarity = (c.get("rarity") or c.get("rank") or "-").strip().upper()
        element = (c.get("element") or "-").strip()

        # 중요한 부분: role이 아니라 class를 가져온다
        cls_raw = (
            c.get("class") or c.get("Class") or
            c.get("classes") or c.get("Classes") or
            c.get("job") or c.get("Job") or
            c.get("type") or c.get("Type")
        )
        cls = normalize_class(cls_raw)

        role = role_from_class(cls, cid)

        out.append({
            "id": cid,
            "name": name or cid,
            "rarity": rarity,
            "element": element,
            "class": cls,   # 7개
            "role": role,   # 5개(계산값)
        })

    return {
        "generated_at": now_iso(),
        "count": len(out),
        "characters": out,
        "notes": {
            "class_set": sorted(list(CLASS_SET)),
            "role_set": sorted(list(ROLE_SET)),
            "class_to_role": CLASS_TO_ROLE,
            "special_role_overrides": SPECIAL_ROLE_OVERRIDES,
        }
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True, help="업스트림 레포 루트 (예: _upstream_gacha_wiki)")
    ap.add_argument("--write", action="store_true", help="characters.json + characters_meta.json 쓰기")
    args = ap.parse_args()

    upstream_root = Path(args.upstream).resolve()

    # 1) 업스트림 characters.js -> characters.json
    run_node_extract(upstream_root, CHAR_JSON)

    # 2) characters.json -> characters_meta.json (classes 기반)
    chars = load_characters_json(CHAR_JSON)
    meta = build_char_meta(chars)

    if args.write:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CHAR_META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OK: wrote {CHAR_META_JSON} (count={meta['count']})")
    else:
        print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
