import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple, List

from argostranslate import package as argos_package
from argostranslate import translate as argos_translate


# -----------------------
# Cache
# -----------------------
def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_cache(cache_path: Path) -> Dict[str, str]:
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    return {}

def save_cache(cache_path: Path, cache: Dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# -----------------------
# Simple checks
# -----------------------
def has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


# -----------------------
# Token protection (placeholders, tags, brackets, variables)
# -----------------------
TOKEN_PATTERNS = [
    r"\{[^}]+\}",        # {x}
    r"\[[^\]]+\]",       # [Skill]
    r"<[^>]+>",          # <tag>
    r"__[^_]+__",         # __PLACEHOLDER__
]

def protect_tokens(text: str) -> Tuple[str, Dict[str, str]]:
    if not text:
        return text, {}

    placeholders: Dict[str, str] = {}
    combined = re.compile("|".join(f"({p})" for p in TOKEN_PATTERNS))

    def repl(m: re.Match) -> str:
        token = m.group(0)
        key = f"__PH{len(placeholders)}__"
        placeholders[key] = token
        return key

    protected = combined.sub(repl, text)
    return protected, placeholders

def restore_tokens(text: str, placeholders: Dict[str, str]) -> str:
    if not placeholders:
        return text
    # 길이 긴 키부터 치환(안전)
    for k in sorted(placeholders.keys(), key=len, reverse=True):
        text = text.replace(k, placeholders[k])
    return text


# -----------------------
# Glossary post-process (free MT 품질 보정)
# -----------------------
def apply_glossary(text: str, glossary: Dict[str, str]) -> str:
    if not glossary:
        return text
    # 간단 치환(필요 시 regex 강화 가능)
    for k, v in glossary.items():
        if k and v:
            text = text.replace(k, v)
    return text


# -----------------------
# Ensure Argos EN->KO model installed
# -----------------------
def ensure_argos_en_ko() -> None:
    # 모델이 이미 설치되어 있으면 그대로 사용
    installed = argos_translate.get_installed_languages()
    if any(l.code == "en" for l in installed) and any(l.code == "ko" for l in installed):
        # 설치된 언어만으로는 부족할 수 있어, 실제 en->ko 번역 가능 여부도 확인
        # 간단 체크: en 언어의 translation list에 ko가 있는지
        en_lang = next((l for l in installed if l.code == "en"), None)
        if en_lang:
            if any(t.to_lang.code == "ko" for t in en_lang.translations):
                return

    # 없으면 다운로드/설치
    argos_package.update_package_index()
    available = argos_package.get_available_packages()
    target = None
    for p in available:
        if p.from_code == "en" and p.to_code == "ko":
            target = p
            break
    if not target:
        raise RuntimeError("Argos EN->KO package not found in index.")

    download_path = target.download()
    argos_package.install_from_path(download_path)


# -----------------------
# JS module import (default + named export)
# -----------------------
def import_js_module(js_path: Path) -> Dict[str, Any]:
    js_path = js_path.resolve()
    with tempfile.TemporaryDirectory() as td:
        tmp_mjs = Path(td) / (js_path.stem + ".mjs")
        shutil.copyfile(js_path, tmp_mjs)

        file_url = tmp_mjs.as_uri()
        code = r"""
import(process.argv[1]).then((m) => {
  process.stdout.write(JSON.stringify(m));
}).catch((e) => {
  console.error(String(e?.stack ?? e));
  process.exit(1);
});
""".strip()

        result = subprocess.run(
            ["node", "--input-type=module", "-e", code, file_url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to import {js_path.name}\n{result.stderr}")

        return json.loads(result.stdout)


def select_character_data_export(module_obj: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    # 1) default 우선
    if "default" in module_obj and isinstance(module_obj["default"], dict):
        d = module_obj["default"]
        if "name" in d and ("skills" in d or "teamSkill" in d):
            return ("default", d)

    # 2) named export 중 *Data 우선
    candidates: List[Tuple[int, str, Dict[str, Any]]] = []
    for k, v in module_obj.items():
        if isinstance(v, dict) and "name" in v and ("skills" in v or "teamSkill" in v):
            score = 0
            if k.lower().endswith("data"):
                score += 10
            if "rarity" in v:
                score += 2
            if "memoryCard" in v:
                score += 1
            if "awakenings" in v or "awakeningEffects" in v:
                score += 1
            candidates.append((score, k, v))

    if not candidates:
        raise RuntimeError("No character data export found (default/named).")

    candidates.sort(reverse=True)
    _, sel_key, sel_obj = candidates[0]
    return (sel_key, sel_obj)


# -----------------------
# Translate (FREE)
# -----------------------
def translate_text_free(text: str, glossary: Dict[str, str], cache: Dict[str, str]) -> str:
    if not text or not isinstance(text, str):
        return text
    if has_hangul(text):
        return text

    key = sha(text)
    if key in cache:
        return cache[key]

    protected, ph = protect_tokens(text)

    # Argos translate
    out = argos_translate.translate(protected, "en", "ko")
    out = restore_tokens(out, ph)

    # Glossary post-process + 가독성 보정(원하면 여기서 규칙 추가)
    out = apply_glossary(out, glossary).strip()

    # 원문 그대로면 캐시 저장하지 않음(고착 방지)
    if out != text:
        cache[key] = out

    return out


# -----------------------
# Translate requested fields only (overwrite existing keys)
# -----------------------
def translate_character_fields(obj: Dict[str, Any], glossary: Dict[str, str], cache: Dict[str, str]) -> int:
    changed = 0

    # 1) Skills description
    skills = obj.get("skills")

    # A) dict 형태 (normal/auto/ultimate/passive)
    if isinstance(skills, dict):
        for _, s in skills.items():
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text_free(src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # B) list 형태
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text_free(src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # 2) Team Skill description (+ alternativeConditions)
    team = obj.get("teamSkill")
    if isinstance(team, dict):
        if isinstance(team.get("description"), str):
            src = team["description"]
            dst = translate_text_free(src, glossary, cache)
            if dst != src:
                team["description"] = dst
                changed += 1

        req = team.get("requirements")
        if isinstance(req, dict) and isinstance(req.get("alternativeConditions"), str):
            src = req["alternativeConditions"]
            dst = translate_text_free(src, glossary, cache)
            if dst != src:
                req["alternativeConditions"] = dst
                changed += 1

    # 3) Awakening Effects
    for aw_key in ("awakenings", "awakeningEffects"):
        aw = obj.get(aw_key)
        if isinstance(aw, list):
            for item in aw:
                if isinstance(item, dict) and isinstance(item.get("effect"), str):
                    src = item["effect"]
                    dst = translate_text_free(src, glossary, cache)
                    if dst != src:
                        item["effect"] = dst
                        changed += 1

    # 4) Memory Card effects[]
    mc = obj.get("memoryCard")
    if isinstance(mc, dict):
        effects = mc.get("effects")
        if isinstance(effects, list):
            for i, e in enumerate(effects):
                if isinstance(e, str):
                    dst = translate_text_free(e, glossary, cache)
                    if dst != e:
                        effects[i] = dst
                        changed += 1

    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--cache", default=".cache/zone_nova_translate_cache_free.json")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    args = ap.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    cache_path = Path(args.cache)
    glossary_path = Path(args.glossary)

    if not src_dir.exists():
        raise RuntimeError(f"Source directory not found: {src_dir}")

    glossary: Dict[str, str] = {}
    if glossary_path.exists():
        glossary = json.loads(glossary_path.read_text(encoding="utf-8"))

    cache = load_cache(cache_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Argos EN->KO 설치 보장
    ensure_argos_en_ko()

    total_changed = 0
    files = sorted(src_dir.glob("*.js"))

    for js_file in files:
        module_obj = import_js_module(js_file)
        export_key, data_obj = select_character_data_export(module_obj)

        changed = translate_character_fields(data_obj, glossary, cache)
        total_changed += changed

        out_path = out_dir / f"{js_file.stem}.json"
        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {js_file.name} (export={export_key}) -> {out_path.name} | updated={changed}")

    save_cache(cache_path, cache)
    print(f"Done. Total updated fields: {total_changed}")


if __name__ == "__main__":
    main()
