import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple, Optional

# -----------------------
# Cache (원문 -> 번역문)
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
# JS 모듈 로드 (default export + named export 모두 대응)
#   - package.json type=module 여부에 영향받지 않도록:
#     원본을 임시 .mjs로 복사해서 import 시도
# -----------------------
def import_js_module(js_path: Path) -> Dict[str, Any]:
    js_path = js_path.resolve()

    with tempfile.TemporaryDirectory() as td:
        tmp_mjs = Path(td) / (js_path.stem + ".mjs")
        shutil.copyfile(js_path, tmp_mjs)

        file_url = tmp_mjs.as_uri()

        code = r"""
import(process.argv[1]).then((m) => {
  // module namespace object 그대로 JSON으로 출력
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


def select_character_data_export(module_obj: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    module_obj: import 결과의 module namespace object (default 포함 가능)
    return: (selected_key, data_obj, other_exports)
    """
    # 1) default가 있고 캐릭터 데이터처럼 보이면 우선
    if "default" in module_obj and isinstance(module_obj["default"], dict):
        d = module_obj["default"]
        if "name" in d and ("skills" in d or "teamSkill" in d) and "rarity" in d:
            other = {k: v for k, v in module_obj.items() if k != "default"}
            return ("default", d, other)

    # 2) named exports 중 "*Data" 우선 탐색 (apepData, gaiaData 등)
    candidates = []
    for k, v in module_obj.items():
        if isinstance(v, dict) and ("skills" in v or "teamSkill" in v) and "name" in v:
            score = 0
            if k.lower().endswith("data"):
                score += 10
            if "rarity" in v:
                score += 3
            if "memoryCard" in v:
                score += 2
            if "awakenings" in v or "awakeningEffects" in v:
                score += 2
            candidates.append((score, k, v))

    if not candidates:
        raise RuntimeError("No character data export found (default/named).")

    candidates.sort(reverse=True)
    _, sel_key, sel_obj = candidates[0]
    other = {k: v for k, v in module_obj.items() if k != sel_key}
    return (sel_key, sel_obj, other)


# -----------------------
# 번역 호출부 (여기만 실제 LLM 연결)
# -----------------------
def translate_text(text: str, glossary: Dict[str, str], cache: Dict[str, str]) -> str:
    if not text or not isinstance(text, str):
        return text

    key = sha(text)
    if key in cache:
        return cache[key]

    translated = call_your_llm_translation(text, glossary)
    cache[key] = translated
    return translated


def call_your_llm_translation(text: str, glossary: Dict[str, str]) -> str:
    """
    TODO: 여기에서 OpenAI(또는 기존 번역 파이프라인)를 호출하도록 연결.
    요구사항(툴팁 톤):
      - 수치/기호/쿨타임/퍼센트/배율은 그대로 유지
      - 'designated enemy single unit' 류는 '지정한 적 1명' 스타일
      - 'Max HP/DEF/ATK'는 '최대 HP/방어력/공격력' 등 한국 게임 표기
      - [Skill Name]처럼 대괄호로 감싼 토큰은 가능한 한 유지
      - glossary 용어 우선 적용
    """
    # 미연결 상태 안전장치: 원문 그대로 반환
    return text


# -----------------------
# 번역 대상만 정확히 덮어쓰기
# -----------------------
def translate_character_fields(obj: Dict[str, Any], glossary: Dict[str, str], cache: Dict[str, str]) -> int:
    changed = 0

    # 1) Skills 하위 description
    # - 케이스 A: skills: { normal:{description}, auto:{description}, ... }  (apep/gaia)
    skills = obj.get("skills")
    if isinstance(skills, dict):
        for key in ("normal", "auto", "ultimate", "passive", "skill"):  # 혹시 skill 키가 있는 변형 대비
            s = skills.get(key)
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text(src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # - 케이스 B: skills: [ {description:...}, ... ] (다른 파일에서 리스트일 가능성 대비)
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text(src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # 2) Team Skill 하위 description (+ requirements.alternativeConditions 있으면 함께)
    team = obj.get("teamSkill")
    if isinstance(team, dict):
        if isinstance(team.get("description"), str):
            src = team["description"]
            dst = translate_text(src, glossary, cache)
            if dst != src:
                team["description"] = dst
                changed += 1

        req = team.get("requirements")
        if isinstance(req, dict) and isinstance(req.get("alternativeConditions"), str):
            src = req["alternativeConditions"]
            dst = translate_text(src, glossary, cache)
            if dst != src:
                req["alternativeConditions"] = dst
                changed += 1

    # 3) Awakening Effects: awakenings[] / awakeningEffects[]
    for aw_key in ("awakenings", "awakeningEffects"):
        aw = obj.get(aw_key)
        if isinstance(aw, list):
            for item in aw:
                if isinstance(item, dict) and isinstance(item.get("effect"), str):
                    src = item["effect"]
                    dst = translate_text(src, glossary, cache)
                    if dst != src:
                        item["effect"] = dst
                        changed += 1

    # 4) Memory Card 하위 effects[]
    mc = obj.get("memoryCard")
    if isinstance(mc, dict):
        effects = mc.get("effects")
        if isinstance(effects, list):
            for i, e in enumerate(effects):
                if isinstance(e, str):
                    dst = translate_text(e, glossary, cache)
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
    ap.add_argument("--wrap_exports", action="store_true",
                    help="true면 JSON에 other exports(SEO 등)도 함께 저장")
    args = ap.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    cache_path = Path(args.cache)
    glossary_path = Path(args.glossary)

    glossary: Dict[str, str] = {}
    if glossary_path.exists():
        glossary = json.loads(glossary_path.read_text(encoding="utf-8"))

    cache = load_cache(cache_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_changed = 0
    files = sorted(src_dir.glob("*.js"))

    for js_file in files:
        try:
            module_obj = import_js_module(js_file)
            sel_key, data_obj, other = select_character_data_export(module_obj)
        except Exception as e:
            print(f"[SKIP] {js_file.name}: {e}")
            continue

        changed = translate_character_fields(data_obj, glossary, cache)
        total_changed += changed

        # 저장 포맷:
        # - 기본: 캐릭터 data_obj만 저장 (요청하신 “나머지 유지”는 data_obj 내부에서 보장)
        # - 옵션: --wrap_exports면 SEO 등 다른 export도 같이 JSON에 묶어서 저장
        if args.wrap_exports:
            payload = {"exportKey": sel_key, "data": data_obj, "otherExports": other}
        else:
            payload = data_obj

        out_path = out_dir / f"{js_file.stem}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {js_file.name} -> {out_path.name} (translated fields updated: {changed})")

    save_cache(cache_path, cache)
    print(f"Done. Total translated fields updated: {total_changed}")


if __name__ == "__main__":
    main()
