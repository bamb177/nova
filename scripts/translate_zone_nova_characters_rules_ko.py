import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =========================================================
# JS module import (default + named export)
# =========================================================
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
    if "default" in module_obj and isinstance(module_obj["default"], dict):
        d = module_obj["default"]
        if "name" in d:
            return ("default", d)

    candidates: List[Tuple[int, str, Dict[str, Any]]] = []
    for k, v in module_obj.items():
        if isinstance(v, dict) and "name" in v:
            score = 0
            if k.lower().endswith("data"):
                score += 10
            if "skills" in v:
                score += 3
            if "teamSkill" in v:
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
    return (sel_key, sel_obj)


# =========================================================
# Glossary
# =========================================================
def load_glossary(path: Path) -> Dict[str, str]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}

def apply_glossary(text: str, glossary: Dict[str, str]) -> str:
    if not glossary:
        return text
    for k in sorted(glossary.keys(), key=len, reverse=True):
        v = glossary[k]
        if k and v:
            text = text.replace(k, v)
    return text


# =========================================================
# Token protection
# =========================================================
TOKEN_PATTERNS = [
    r"\{[^}]+\}",
    r"\[[^\]]+\]",
    r"<[^>]+>",
]

def protect_tokens(text: str):
    if not text:
        return text, {}
    placeholders = {}
    combined = re.compile("|".join(f"({p})" for p in TOKEN_PATTERNS))

    def repl(m):
        token = m.group(0)
        key = f"__PH{len(placeholders)}__"
        placeholders[key] = token
        return key

    return combined.sub(repl, text), placeholders

def restore_tokens(text: str, placeholders: Dict[str, str]):
    if not placeholders:
        return text
    for k in sorted(placeholders.keys(), key=len, reverse=True):
        text = text.replace(k, placeholders[k])
    return text


# =========================================================
# KO style post-process (tooltip tone)
# =========================================================
def postprocess_ko(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t

    # honorific -> tooltip tone
    t = re.sub(r"합니다\.", "한다.", t)
    t = re.sub(r"됩니다\.", "된다.", t)
    t = re.sub(r"입니다\.", "이다.", t)

    t = re.sub(r"지정된 적", "지정한 적", t)
    t = re.sub(r"\s*%\s*", "%", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\.\.+", ".", t)
    return t.strip()


# =========================================================
# RULE translator (Afrodite-style subset)
# - 매칭되면 한국어 반환, 아니면 None
# =========================================================
DMG_TYPE = {
    "holy": "성속성",
    "chaos": "혼돈",
    "fire": "화염",
    "water": "수속성",
    "wind": "풍속성",
    "dark": "암속성",
    "light": "광속성",
    "physical": "물리",
}

def normalize_en(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[ \t]+", " ", s)
    return s

def ko_target_from_en(s: str) -> str:
    ss = s.lower()
    if "designated enemy unit" in ss or "designated enemy" in ss:
        return "지정한 적 1명"
    if "all enemies" in ss:
        return "모든 적"
    if "all allied units" in ss or "all allies" in ss:
        return "아군 전체"
    if "random enemy" in ss:
        return "무작위 적 1명"
    if "single enemy" in ss or "one enemy" in ss or "an enemy" in ss:
        return "적 1명"
    return "대상"

def rule_translate_line(en: str) -> Optional[str]:
    t = normalize_en(en)

    # Deals 120% attack power Chaos damage to designated enemy unit.
    m = re.match(
        r"^Deals\s+(?P<pct>\d+(?:\.\d+)?)%\s*attack\s*power\s*(?P<dtype>(Holy|Chaos|Fire|Water|Wind|Dark|Light|Physical)\s+damage)?\s*to\s+(?P<tgt>.+?)\.$",
        t,
        re.I,
    )
    if m:
        pct = m.group("pct")
        dtype = m.group("dtype") or ""
        elem = None
        if dtype:
            elem = DMG_TYPE.get(dtype.split()[0].lower())
        tgt = ko_target_from_en(m.group("tgt"))
        dmg = f"{elem} 피해" if elem else "피해"
        return f"{tgt}에게 공격력의 {pct}%만큼 {dmg}를 준다."

    # Attack power increased by 40%.
    m = re.match(r"^Attack\s+power\s+increased\s+by\s+(?P<x>\d+(?:\.\d+)?)%\.$", t, re.I)
    if m:
        x = m.group("x")
        return f"공격력이 {x}% 증가한다."

    return None


# =========================================================
# IMPORTANT: Recursive field discovery + apply translation
# =========================================================
def path_has_ancestor(path: List[Any], ancestor: str) -> bool:
    # ancestor must appear in the path (key names)
    return any(isinstance(p, str) and p == ancestor for p in path)

def is_target_field(path: List[Any], key: str, value: Any) -> bool:
    """
    Find requested fields robustly:
    - skills/**/description (string)
    - teamSkill/**/description (string)
    - awakenings or awakeningEffects/**/effect (string)
    - memoryCard/effects (list[str])
    """
    if not isinstance(key, str):
        return False

    if key == "description" and isinstance(value, str):
        if path_has_ancestor(path, "skills") or path_has_ancestor(path, "teamSkill"):
            return True

    if key == "effect" and isinstance(value, str):
        if path_has_ancestor(path, "awakenings") or path_has_ancestor(path, "awakeningEffects"):
            return True

    if key == "effects" and isinstance(value, list) and all(isinstance(x, str) for x in value):
        if path_has_ancestor(path, "memoryCard"):
            return True

    return False

def translate_value(value: Any, glossary: Dict[str, str], fallback: str) -> Any:
    """
    Translate a value according to our "Afrodite-like" style rules.
    - For string: rule translate line-by-line; if not match, keep or mark.
    - For list[str]: translate each element.
    """
    def translate_string(s: str) -> str:
        lines = s.split("\n")
        out_lines = []
        for line in lines:
            raw = line.strip()
            if not raw:
                out_lines.append(line)
                continue

            protected, ph = protect_tokens(raw)
            ruled = rule_translate_line(protected)
            if ruled is None:
                out = raw if fallback == "keep" else "[UNTRANSLATED] " + raw
            else:
                out = restore_tokens(ruled, ph)
                out = apply_glossary(out, glossary)
                out = postprocess_ko(out)

            out_lines.append(out)
        return "\n".join(out_lines)

    if isinstance(value, str):
        return translate_string(value)

    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return [translate_string(x) for x in value]

    return value


def walk_and_translate(obj: Any, glossary: Dict[str, str], fallback: str) -> int:
    """
    Walk dict/list recursively, translate target fields in-place.
    Returns number of modified fields (not strings).
    """
    changed = 0

    def _walk(node: Any, path: List[Any]):
        nonlocal changed

        if isinstance(node, dict):
            for k, v in list(node.items()):
                # identify target
                if is_target_field(path + [k], k, v):
                    new_v = translate_value(v, glossary, fallback)
                    if new_v != v:
                        node[k] = new_v
                        changed += 1
                # recurse
                _walk(node[k], path + [k])

        elif isinstance(node, list):
            for i in range(len(node)):
                _walk(node[i], path + [i])

    _walk(obj, [])
    return changed


# =========================================================
# Main
# =========================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    ap.add_argument("--fallback", choices=["keep", "mark"], default="keep")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    glossary_path = Path(args.glossary)

    if not src_dir.exists():
        raise RuntimeError(f"Source directory not found: {src_dir}")

    glossary = load_glossary(glossary_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_changed = 0
    files = sorted(src_dir.glob("*.js"))

    for js_file in files:
        module_obj = import_js_module(js_file)
        export_key, data_obj = select_character_data_export(module_obj)

        changed = walk_and_translate(data_obj, glossary, fallback=args.fallback)
        total_changed += changed

        out_path = out_dir / f"{js_file.stem}.json"
        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.debug:
            print(f"[DEBUG] {js_file.name} export={export_key} changed_fields={changed}")
        else:
            print(f"[OK] {js_file.name} -> {out_path.name} | changed_fields={changed}")

    print(f"Done. Total changed fields: {total_changed}")


if __name__ == "__main__":
    main()
