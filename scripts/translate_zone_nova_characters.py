import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Tuple, Optional

from openai import OpenAI  # pip install openai


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
# Simple heuristics
# -----------------------
def looks_korean(text: str) -> bool:
    # Hangul syllables range
    for ch in text:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3:
            return True
    return False

def normalize_out(text: str) -> str:
    if not isinstance(text, str):
        return text
    t = text.strip()
    # 모델이 가끔 따옴표로 감싸면 제거
    if len(t) >= 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "'" and t[-1] == "'")):
        t = t[1:-1].strip()
    return t


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
    candidates = []
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
# OpenAI translation
# -----------------------
def build_glossary_text(glossary: Dict[str, str]) -> str:
    if not glossary:
        return ""
    # 너무 길면 토큰 낭비라, 200개 정도만 제한(필요 시 조정)
    items = list(glossary.items())[:200]
    lines = "\n".join([f"- {k} => {v}" for k, v in items])
    return lines


def call_llm_translate(client: OpenAI, model: str, text: str, glossary: Dict[str, str]) -> str:
    glossary_txt = build_glossary_text(glossary)

    instructions = (
        "You are a senior Korean game localization translator.\n"
        "Translate the input into natural Korean RPG tooltip/combat text.\n"
        "Rules:\n"
        "1) Preserve all numbers, %, decimals, multipliers, durations, cooldowns, and punctuation.\n"
        "2) Preserve placeholders/tokens like {x}, [Skill], <tag>, and newline structure.\n"
        "3) Use concise in-game Korean phrasing (e.g., '지정한 적 1명', '공격력의 120%만큼').\n"
        "4) Do NOT add explanations. Output Korean text only.\n"
    )
    if glossary_txt:
        instructions += "\nGlossary (must follow when applicable):\n" + glossary_txt + "\n"

    # Responses API example patterns are in OpenAI docs. :contentReference[oaicite:1]{index=1}
    resp = client.responses.create(
        model=model,
        instructions=instructions,
        input=text,
        # 최대한 깔끔한 출력 유도
        temperature=0.2,
    )
    return normalize_out(resp.output_text)


def translate_text(client: OpenAI, model: str, text: str, glossary: Dict[str, str], cache: Dict[str, str]) -> str:
    if not text or not isinstance(text, str):
        return text
    # 이미 한글이면 재번역하지 않음
    if looks_korean(text):
        return text

    key = sha(text)
    if key in cache:
        return cache[key]

    # 간단한 리트라이(일시적 429/5xx 대비)
    last_err = None
    for attempt in range(1, 4):
        try:
            out = call_llm_translate(client, model, text, glossary)
            cache[key] = out
            return out
        except Exception as e:
            last_err = e
            time.sleep(1.5 * attempt)

    raise RuntimeError(f"Translation failed after retries: {last_err}")


# -----------------------
# Translate only requested fields (overwrite existing keys)
# -----------------------
def translate_character_fields(
    client: OpenAI,
    model: str,
    obj: Dict[str, Any],
    glossary: Dict[str, str],
    cache: Dict[str, str],
) -> int:
    changed = 0

    # 1) Skills description
    skills = obj.get("skills")

    # A) skills is dict (normal/auto/ultimate/passive)
    if isinstance(skills, dict):
        for key, s in skills.items():
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text(client, model, src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # B) skills is list
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text(client, model, src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # 2) Team Skill description (+ alternativeConditions if present)
    team = obj.get("teamSkill")
    if isinstance(team, dict):
        if isinstance(team.get("description"), str):
            src = team["description"]
            dst = translate_text(client, model, src, glossary, cache)
            if dst != src:
                team["description"] = dst
                changed += 1

        req = team.get("requirements")
        if isinstance(req, dict) and isinstance(req.get("alternativeConditions"), str):
            src = req["alternativeConditions"]
            dst = translate_text(client, model, src, glossary, cache)
            if dst != src:
                req["alternativeConditions"] = dst
                changed += 1

    # 3) Awakening Effects (6 levels) effect
    for aw_key in ("awakenings", "awakeningEffects"):
        aw = obj.get(aw_key)
        if isinstance(aw, list):
            for item in aw:
                if isinstance(item, dict) and isinstance(item.get("effect"), str):
                    src = item["effect"]
                    dst = translate_text(client, model, src, glossary, cache)
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
                    dst = translate_text(client, model, e, glossary, cache)
                    if dst != e:
                        effects[i] = dst
                        changed += 1

    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--cache", default=".cache/zone_nova_translate_cache.json")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    ap.add_argument("--model", default=os.getenv("TRANSLATE_MODEL", "gpt-4.1"))
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

    # SDK는 OPENAI_API_KEY 환경변수에서 키를 읽습니다. :contentReference[oaicite:2]{index=2}
    client = OpenAI()

    total_changed = 0
    files = sorted(src_dir.glob("*.js"))

    for js_file in files:
        module_obj = import_js_module(js_file)
        export_key, data_obj = select_character_data_export(module_obj)

        changed = translate_character_fields(client, args.model, data_obj, glossary, cache)
        total_changed += changed

        out_path = out_dir / f"{js_file.stem}.json"
        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {js_file.name} (export={export_key}) -> {out_path.name} | updated={changed}")

    save_cache(cache_path, cache)
    print(f"Done. Total updated fields: {total_changed}")


if __name__ == "__main__":
    main()
