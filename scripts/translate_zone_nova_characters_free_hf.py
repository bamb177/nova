import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple, List, Optional

import torch
from transformers import MarianMTModel, MarianTokenizer


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
# Basic helpers
# -----------------------
def has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))

def normalize_out(text: str) -> str:
    return (text or "").strip()


# -----------------------
# Token protection (placeholders/tags)
# -----------------------
TOKEN_PATTERNS = [
    r"\{[^}]+\}",   # {x}
    r"\[[^\]]+\]",  # [Skill]
    r"<[^>]+>",     # <tag>
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
    for k in sorted(placeholders.keys(), key=len, reverse=True):
        text = text.replace(k, placeholders[k])
    return text


# -----------------------
# Glossary post-process
# -----------------------
def apply_glossary(text: str, glossary: Dict[str, str]) -> str:
    if not glossary:
        return text
    for k, v in glossary.items():
        if k and v:
            text = text.replace(k, v)
    return text


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

    # 2) named exports 중 *Data 우선
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
# HF Translator (OPUS-MT / MarianMT)
# -----------------------
class HFTranslator:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.tokenizer = MarianTokenizer.from_pretrained(model_name)
        self.model = MarianMTModel.from_pretrained(model_name)
        self.model.eval()
        self.device = torch.device("cpu")
        self.model.to(self.device)

        # multilingual target token auto-detection
        self.tgt_token = self._pick_korean_target_token()

    def _pick_korean_target_token(self) -> Optional[str]:
        toks = getattr(self.tokenizer, "additional_special_tokens", []) or []
        if not toks:
            return None

        candidates = []
        for t in toks:
            tt = t.lower()
            # OPUS-MT tc-big 계열은 >>id<< 형태 토큰을 요구할 수 있음. :contentReference[oaicite:2]{index=2}
            if tt.startswith(">>") and tt.endswith("<<") and ("kor" in tt or "ko" in tt):
                score = 0
                if "kor_hang" in tt or "kor-hang" in tt:
                    score += 30
                if "kor" in tt:
                    score += 10
                if "ko" in tt:
                    score += 5
                candidates.append((score, t))

        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _generate(self, texts: List[str], max_length: int = 512) -> List[str]:
        encoded = self.tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=max_length,
        ).to(self.device)

        with torch.no_grad():
            generated = self.model.generate(**encoded, max_length=max_length, num_beams=4)

        out = self.tokenizer.batch_decode(generated, skip_special_tokens=True)
        return [normalize_out(x) for x in out]

    def translate_one(self, text: str) -> str:
        # 1) auto token이 있으면 우선 사용
        if self.tgt_token:
            out = self._generate([f"{self.tgt_token} {text}"])[0]
            if out:
                return out

        # 2) 그래도 비정상이면(비한글/빈값) 후보 토큰으로 재시도
        # (환경/토크나이저에 따라 토큰 리스트가 비거나 감지 실패하는 케이스 방어)
        fallback_tokens = [">>kor_Hang<<", ">>kor<<", ">>ko<<"]
        for tok in fallback_tokens:
            out = self._generate([f"{tok} {text}"])[0]
            if out:
                return out

        # 3) 마지막: 토큰 없이
        return self._generate([text])[0]


def translate_text_hf(tr: HFTranslator, text: str, glossary: Dict[str, str], cache: Dict[str, str]) -> str:
    if not text or not isinstance(text, str):
        return text
    if has_hangul(text):
        return text

    key = sha(tr.model_name + "|" + text)
    if key in cache:
        return cache[key]

    protected, ph = protect_tokens(text)
    out = tr.translate_one(protected)
    out = restore_tokens(out, ph)
    out = apply_glossary(out, glossary).strip()

    # “영문 그대로” 결과는 캐시에 저장하지 않음(고착 방지)
    if out and out != text:
        cache[key] = out

    return out if out else text


# -----------------------
# Translate requested fields only (overwrite existing keys)
# -----------------------
def translate_character_fields(tr: HFTranslator, obj: Dict[str, Any], glossary: Dict[str, str], cache: Dict[str, str]) -> int:
    changed = 0

    # 1) Skills description
    skills = obj.get("skills")
    if isinstance(skills, dict):
        for _, s in skills.items():
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text_hf(tr, src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text_hf(tr, src, glossary, cache)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # 2) Team Skill description (+ alternativeConditions if present)
    team = obj.get("teamSkill")
    if isinstance(team, dict):
        if isinstance(team.get("description"), str):
            src = team["description"]
            dst = translate_text_hf(tr, src, glossary, cache)
            if dst != src:
                team["description"] = dst
                changed += 1

        req = team.get("requirements")
        if isinstance(req, dict) and isinstance(req.get("alternativeConditions"), str):
            src = req["alternativeConditions"]
            dst = translate_text_hf(tr, src, glossary, cache)
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
                    dst = translate_text_hf(tr, src, glossary, cache)
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
                    dst = translate_text_hf(tr, e, glossary, cache)
                    if dst != e:
                        effects[i] = dst
                        changed += 1

    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--cache", default=".cache/zone_nova_translate_cache_free_hf.json")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    ap.add_argument("--model_name", default="Helsinki-NLP/opus-mt-tc-big-en-ko")
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

    tr = HFTranslator(args.model_name)

    total_changed = 0
    files = sorted(src_dir.glob("*.js"))

    for js_file in files:
        module_obj = import_js_module(js_file)
        export_key, data_obj = select_character_data_export(module_obj)

        changed = translate_character_fields(tr, data_obj, glossary, cache)
        total_changed += changed

        out_path = out_dir / f"{js_file.stem}.json"
        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {js_file.name} (export={export_key}) -> {out_path.name} | updated={changed}")

    save_cache(cache_path, cache)
    print(f"Done. Total updated fields: {total_changed}")


if __name__ == "__main__":
    main()
