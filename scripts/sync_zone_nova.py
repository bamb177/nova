# scripts/sync_zone_nova.py
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def title_case(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s[0].upper() + s[1:].lower()


def normalize_element(s: str) -> str:
    return title_case(s)


def normalize_class(s: str) -> str:
    return title_case(s)


def normalize_rarity(s: str) -> str:
    t = (s or "").strip().upper()
    return t if t in {"SSR", "SR", "R"} else (t or "")


def derive_role_from_class(cls: str) -> str:
    c = normalize_class(cls)
    if c == "Guardian":
        return "Tank"
    if c == "Healer":
        return "Healer"
    if c == "Buffer":
        return "Buffer"
    if c == "Debuffer":
        return "Debuffer"
    if c in {"Warrior", "Mage", "Rogue"}:
        return "DPS"
    return ""


def normalize_role(role: str, cls: str) -> str:
    r = (role or "").strip()
    if r:
        t = title_case(r)
        if t.upper() == "DPS":
            return "DPS"
        return t
    return derive_role_from_class(cls)


def slug_id(name: str) -> str:
    # jeanne d arc -> jeannedarc
    import re
    v = (name or "").strip().lower()
    v = re.sub(r"[^a-z0-9]", "", v)
    return v


def build_image_index(img_dir: Path):
    # public/images/games/zone-nova/characters
    idx = {}
    if not img_dir.exists():
        return idx

    for p in img_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        stem = p.stem  # 파일명(확장자 제외)
        idx[stem.lower()] = p.name
    return idx


def pick_image_url(name: str, img_index: dict):
    # name 기반으로 가장 단순 매칭 (현재 repo는 이름 기반 파일들이 이미 존재)
    key = (name or "").strip().lower()
    if key in img_index:
        return f"/images/games/zone-nova/characters/{img_index[key]}"

    # 공백/특수문자 제거 매칭도 시도
    sid = slug_id(name)
    if sid and sid in img_index:
        return f"/images/games/zone-nova/characters/{img_index[sid]}"

    # 마지막: 없음
    return None


def run_node_extract(repo_root: Path, upstream_root: Path, out_json: Path):
    script = repo_root / "scripts" / "extract_zone_nova_characters.mjs"
    if not script.exists():
        raise RuntimeError(f"추출 스크립트가 없습니다: {script}")

    cmd = ["node", str(script), "--upstream", str(upstream_root), "--out", str(out_json)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Node 변환 실패\n"
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write output files")
    ap.add_argument("--upstream", required=True, help="upstream repo dir (e.g., _upstream_gacha_wiki)")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    upstream_root = (repo_root / args.upstream).resolve()

    if not upstream_root.exists():
        raise RuntimeError(f"업스트림 루트가 존재하지 않습니다: {upstream_root}")

    data_dir = repo_root / "public" / "data" / "zone-nova"
    out_characters_json = data_dir / "characters.json"
    out_meta_json = data_dir / "characters_meta.json"

    # 1) 업스트림에서 characters JSON 추출
    run_node_extract(repo_root, upstream_root, out_characters_json)

    # 2) 추출 결과 로드
    chars = load_json(out_characters_json)
    if not isinstance(chars, list):
        raise RuntimeError("characters.json은 list 형태여야 합니다.")

    # 3) 이미지 인덱스 만들기 (repo에 있는 이미지 기준)
    img_dir = repo_root / "public" / "images" / "games" / "zone-nova" / "characters"
    img_index = build_image_index(img_dir)

    # 4) characters_meta.json 생성 (UI/추천 엔진이 쓰는 최종)
    meta_list = []
    for c in chars:
        name = (c.get("name") or "").strip()
        if not name:
            # name이 비어있으면 파일명 기반이 들어가도록 node에서 처리되어야 함
            continue

        element = normalize_element(c.get("element") or "")
        cls = normalize_class(c.get("class") or "")
        rarity = normalize_rarity(c.get("rarity") or "")
        role = normalize_role(c.get("role") or "", cls)

        # Apep/Gaia 누락 케이스 방지: class/element가 비어있으면 source_file이라도 남겨 디버깅 가능
        image = pick_image_url(name, img_index)

        meta_list.append({
            "id": c.get("id") or slug_id(name),
            "name": name,
            "element": element,
            "class": cls,
            "role": role,
            "rarity": rarity,
            "image": image,  # None 가능(없으면 UI에서 NO IMAGE)
            "source_file": c.get("source_file"),
        })

    # 5) 안전장치: id 기준 중복 제거(마지막 값 유지)
    dedup = {}
    for m in meta_list:
        dedup[m["id"]] = m
    meta_list = list(dedup.values())
    meta_list.sort(key=lambda x: x["name"].lower())

    payload = {
        "last_refresh": now_iso(),
        "count": len(meta_list),
        "characters": meta_list
    }

    if args.write:
        write_json(out_meta_json, payload)

    print(f"OK: characters={len(chars)}, meta={len(meta_list)}")
    print(f"- {out_characters_json}")
    print(f"- {out_meta_json}")


if __name__ == "__main__":
    main()
