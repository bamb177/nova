# scripts/translate_zone_nova_characters.py
import argparse, json, os, subprocess, hashlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

# -----------------------
# 1) JS 모듈을 JSON으로 로드 (Node로 import)
# -----------------------
def load_js_module_as_json(js_path: Path) -> Dict[str, Any]:
    abs_path = js_path.resolve()
    file_url = abs_path.as_uri()

    # node --input-type=module -e "import(...)" 로 default export를 JSON.stringify
    code = r"""
import(process.argv[1]).then((m) => {
  const obj = (m && (m.default ?? m)) ?? {};
  process.stdout.write(JSON.stringify(obj));
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
        raise RuntimeError(f"Failed to import: {js_path}\n{result.stderr}")

    return json.loads(result.stdout)

# -----------------------
# 2) 캐시 (원문 -> 번역문)
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
# 3) 번역 함수(여기만 프로젝트 환경에 맞게 연결)
# -----------------------
def translate_text(text: str, glossary: Dict[str, str], cache: Dict[str, str]) -> str:
    if not text or not isinstance(text, str):
        return text

    key = sha(text)
    if key in cache:
        return cache[key]

    # (A) 용어 사전 강제 치환(전처리/후처리 중 택1)
    # 필요하면 text에 glossary를 적용하거나, 프롬프트에 포함시키세요.
    # 여기서는 “프롬프트 주입 방식”을 권장하므로 직접 치환은 최소화.

    # (B) 실제 번역 호출부: 사용 중인 OpenAI 호출 함수를 여기에 연결
    # 아래는 "자리"만 만들어 둔 것입니다. (프로젝트 기존 번역 함수 재사용 권장)
    translated = call_your_llm_translation(text, glossary)

    cache[key] = translated
    return translated

def call_your_llm_translation(text: str, glossary: Dict[str, str]) -> str:
    """
    TODO:
      - 프로젝트에서 이미 쓰고 있는 번역(OpenAI) 호출 로직을 여기에 연결하세요.
      - 요구사항: 한국 게임 전투문/툴팁 톤, 수치/기호 유지, 용어 사전 준수.
    """
    # 임시(미연결) 상태에서는 원문 반환하게 두면 안전합니다.
    # 실제로는 이 라인을 LLM 호출 결과로 교체하세요.
    return text

# -----------------------
# 4) 번역 대상 필드만 찾아서 덮어쓰기
# -----------------------
def translate_character_obj(obj: Dict[str, Any], glossary: Dict[str, str], cache: Dict[str, str]) -> Tuple[Dict[str, Any], int]:
    changed = 0

    # 1) Skills[].description
    skills = obj.get("skills")
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text(src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # 2) Team Skill.description
    team = obj.get("teamSkill")
    if isinstance(team, dict) and isinstance(team.get("description"), str):
        src = team["description"]
        dst = translate_text(src, glossary, cache)
        if dst != src:
            team["description"] = dst
            changed += 1

    # 3) Awakening Effects (6 levels) effect
    # 케이스 A: awakeningEffects: [{level:1, effect:"..."}, ...]
    ae = obj.get("awakeningEffects")
    if isinstance(ae, list):
        for item in ae:
            if isinstance(item, dict) and isinstance(item.get("effect"), str):
                src = item["effect"]
                dst = translate_text(src, glossary, cache)
                if dst != src:
                    item["effect"] = dst
                    changed += 1

    # 케이스 B(혹시): awakenings: { "1": {effect:"..."}, ... } 또는 리스트 구조
    aw = obj.get("awakenings")
    if isinstance(aw, list):
        for item in aw:
            if isinstance(item, dict) and isinstance(item.get("effect"), str):
                src = item["effect"]
                dst = translate_text(src, glossary, cache)
                if dst != src:
                    item["effect"] = dst
                    changed += 1
    elif isinstance(aw, dict):
        for _, item in aw.items():
            if isinstance(item, dict) and isinstance(item.get("effect"), str):
                src = item["effect"]
                dst = translate_text(src, glossary, cache)
                if dst != src:
                    item["effect"] = dst
                    changed += 1

    # 4) Memory Card.effects[]
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

    return obj, changed

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--src", default="public/data/zone-nova/characters")
    p.add_argument("--out", default="public/data/zone-nova/characters_ko")
    p.add_argument("--cache", default=".cache/zone_nova_translate_cache.json")
    p.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    args = p.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    cache_path = Path(args.cache)
    glossary_path = Path(args.glossary)

    glossary = {}
    if glossary_path.exists():
        glossary = json.loads(glossary_path.read_text(encoding="utf-8"))

    cache = load_cache(cache_path)

    out_dir.mkdir(parents=True, exist_ok=True)

    total_changed = 0
    files = sorted(src_dir.glob("*.js"))

    for js_file in files:
        try:
            obj = load_js_module_as_json(js_file)
        except Exception as e:
            print(f"[SKIP] {js_file.name}: {e}")
            continue

        obj2, changed = translate_character_obj(obj, glossary, cache)
        total_changed += changed

        out_path = out_dir / (js_file.stem + ".json")
        out_path.write_text(json.dumps(obj2, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {js_file.name} -> {out_path} (translated fields: {changed})")

    save_cache(cache_path, cache)
    print(f"Done. Total translated fields updated: {total_changed}")

if __name__ == "__main__":
    main()
