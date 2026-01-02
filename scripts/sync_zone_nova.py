import argparse
import json
import os
import re
import subprocess
import hashlib
import time
import urllib.request

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

def load_js_default_export_as_json(fp: Path) -> dict:
    """
    gacha-wiki의 .js 파일(대부분 export default {...})을 Node로 import해서
    JSON 문자열로 출력한 뒤 파이썬 dict로 변환한다.
    """
    import subprocess
    from pathlib import Path

    # Windows 경로/특수문자 대비: file URL로 변환해서 dynamic import
    js = r"""
import { pathToFileURL } from 'url';
const p = process.argv[1];
const m = await import(pathToFileURL(p).href);
const data = m.default ?? m;
process.stdout.write(JSON.stringify(data));
""".strip()

    proc = subprocess.run(
        ["node", "--input-type=module", "-e", js, str(fp)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node import failed: {fp.name}\n{proc.stderr}")

    return json.loads(proc.stdout)

def _load_json_if_exists(p: Path, default):
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def _save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def _openai_translate_ko(text: str, api_key: str, model: str) -> str:
    """
    Chat Completions API 사용. (공식 엔드포인트)
    POST https://api.openai.com/v1/chat/completions :contentReference[oaicite:1]{index=1}
    """
    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a translation engine. Translate the user's text into Korean. "
                    "Do not add explanations. Preserve formatting such as newlines and bullet points. "
                    "Keep proper nouns as-is when they look like character names."
                ),
            },
            {"role": "user", "content": text},
        ],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    j = json.loads(raw)

    # 안전 처리
    out = (
        j.get("choices", [{}])[0]
         .get("message", {})
         .get("content", "")
    )
    return (out or "").strip()

def _should_translate_string(s: str) -> bool:
    """
    이미 한국어가 섞여있거나, 너무 짧거나, 코드/ID 같은 건 번역 안 함.
    """
    if not s:
        return False
    t = s.strip()
    if len(t) <= 1:
        return False

    # 한글이 이미 포함되어 있으면 스킵(원본에 KO가 있는 경우)
    if any("\uac00" <= ch <= "\ud7a3" for ch in t):
        return False

    # id/슬러그/파일명 같은 느낌이면 스킵
    if len(t) < 40 and all(ch.isalnum() or ch in "-_./ " for ch in t):
        return False

    return True

def translate_detail_object_to_ko(detail: dict, character_name: str, cache_path: Path) -> dict:
    """
    detail 내부의 '문장/설명' 스트링을 한국어로 번역해 저장.
    단, 캐릭터명(character_name) 및 최상위 name은 유지.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    # 키 없으면 번역 스킵(그대로 저장)
    if not api_key:
        return detail

    cache = _load_json_if_exists(cache_path, default={})

    def tr(s: str) -> str:
        if not _should_translate_string(s):
            return s
        key = _sha1(s)
        if key in cache:
            return cache[key]
        # 과금/레이트리밋 완화용 소량 sleep
        time.sleep(0.2)
        ko = _openai_translate_ko(s, api_key=api_key, model=model)
        cache[key] = ko if ko else s
        return cache[key]

    def walk(obj, path=()):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                # 캐릭터명은 유지:
                # - 최상위의 name
                # - character.name (혹시 존재하면)
                if (
                    isinstance(v, str)
                    and k == "name"
                    and (
                        len(path) == 0 or (len(path) == 1 and path[0] == "character")
                    )
                ):
                    out[k] = v
                    continue

                # 캐릭터명이 값으로 등장해도 그대로 유지(동일 문자열이면)
                if isinstance(v, str) and character_name and v.strip() == character_name.strip():
                    out[k] = v
                    continue

                out[k] = walk(v, path + (k,))
            return out

        if isinstance(obj, list):
            return [walk(x, path + ("[]",)) for x in obj]

        if isinstance(obj, str):
            return tr(obj)

        return obj

    translated = walk(detail)

    # 캐시 저장(커밋 대상이 되게 public/data 아래에 두는 걸 권장)
    _save_json(cache_path, cache)
    return translated


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

    for fp in sorted(upstream_char_dir.glob("*.js")):
        try:
            raw = load_js_default_export_as_json(fp)
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

        cache_path = DATA_DIR / "_translate_cache_ko.json"
        detail_obj = translate_detail_object_to_ko(detail_obj, character_name=canon_name, cache_path=cache_path)
        
        out_path = out_dir / f"{chosen}.json"
        _save_json(out_path, raw)

    # 리포트 저장
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
