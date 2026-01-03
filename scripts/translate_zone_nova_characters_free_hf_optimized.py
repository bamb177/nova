import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch
from transformers import MarianMTModel, MarianTokenizer


# =========================
# Cache helpers
# =========================
def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_cache(cache_path: Path) -> Dict[str, str]:
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    return {}

def save_cache(cache_path: Path, cache: Dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

def has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


# =========================
# Glossary
# =========================
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


# =========================
# Token protection
# =========================
TOKEN_PATTERNS = [
    r"\{[^}]+\}",
    r"\[[^\]]+\]",
    r"<[^>]+>",
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

    return combined.sub(repl, text), placeholders

def restore_tokens(text: str, placeholders: Dict[str, str]) -> str:
    if not placeholders:
        return text
    for k in sorted(placeholders.keys(), key=len, reverse=True):
        text = text.replace(k, placeholders[k])
    return text


# =========================
# Style postprocess
# =========================
def postprocess_ko(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t

    # honorific -> tooltip tone
    t = re.sub(r"합니다\.", "한다.", t)
    t = re.sub(r"됩니다\.", "된다.", t)
    t = re.sub(r"입니다\.", "이다.", t)
    t = re.sub(r"합니다$", "한다", t)
    t = re.sub(r"됩니다$", "된다", t)
    t = re.sub(r"입니다$", "이다", t)

    # common fixes
    t = re.sub(r"지정된 적", "지정한 적", t)
    t = re.sub(r"지정된 대상", "지정한 대상", t)

    # spacing
    t = re.sub(r"\s*%\s*", "%", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\.\.+", ".", t)
    return t.strip()


# =========================
# JS loader
# =========================
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
        return ("default", module_obj["default"])

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
            candidates.append((score, k, v))

    if not candidates:
        raise RuntimeError("No character data export found.")

    candidates.sort(reverse=True)
    _, key, obj = candidates[0]
    return (key, obj)


# =========================
# HF Marian batch translator
# =========================
class HFTranslator:
    def __init__(self, model_name: str, num_beams: int = 2):
        self.model_name = model_name
        self.num_beams = max(1, int(num_beams))
        self.tokenizer = MarianTokenizer.from_pretrained(model_name)
        self.model = MarianMTModel.from_pretrained(model_name)
        self.model.eval()
        self.device = torch.device("cpu")
        self.model.to(self.device)
        self.tgt_token = self._pick_korean_target_token()

    def _pick_korean_target_token(self) -> Optional[str]:
        toks = getattr(self.tokenizer, "additional_special_tokens", []) or []
        if not toks:
            return None
        candidates = []
        for t in toks:
            tt = t.lower()
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

    def translate_batch(self, texts: List[str], max_length: int = 512) -> List[str]:
        if self.tgt_token:
            texts = [f"{self.tgt_token} {t}" for t in texts]

        encoded = self.tokenizer(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=max_length,
        ).to(self.device)

        with torch.no_grad():
            generated = self.model.generate(
                **encoded,
                max_length=max_length,
                num_beams=self.num_beams,
            )
        out = self.tokenizer.batch_decode(generated, skip_special_tokens=True)
        return [x.strip() for x in out]


# =========================
# Target field discovery (schema-robust)
# =========================
def path_has_ancestor(path: List[Any], ancestor: str) -> bool:
    return any(isinstance(p, str) and p == ancestor for p in path)

def is_target_field(path: List[Any], key: str, value: Any) -> bool:
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


# =========================
# Collect & apply translations
# =========================
def collect_targets(obj: Any) -> List[Tuple[List[Any], str, Any]]:
    """
    Returns list of (path, key, value) for target fields.
    Path includes keys/indexes leading to the field (excluding the field key itself).
    """
    targets = []

    def _walk(node: Any, path: List[Any]):
        if isinstance(node, dict):
            for k, v in node.items():
                if is_target_field(path + [k], k, v):
                    targets.append((path, k, v))
                _walk(v, path + [k])
        elif isinstance(node, list):
            for i, it in enumerate(node):
                _walk(it, path + [i])

    _walk(obj, [])
    return targets

def get_by_path(root: Any, path: List[Any]) -> Any:
    cur = root
    for p in path:
        cur = cur[p]
    return cur


def translate_texts_bulk(
    translator: HFTranslator,
    texts: List[str],
    glossary: Dict[str, str],
    cache: Dict[str, str],
    batch_size: int,
) -> List[str]:
    # protect tokens per line; store placeholders
    protected_list = []
    placeholders_list = []
    keys = []

    for t in texts:
        # line split 유지
        # 여기서는 “한 필드 전체”를 1문장으로 처리(속도 우선)
        # 줄바꿈이 많으면 필드 자체에서 split해서 여러 건으로 넣는 구조로 확장 가능
        prot, ph = protect_tokens(t)
        protected_list.append(prot)
        placeholders_list.append(ph)
        keys.append(sha(translator.model_name + "|" + t))

    results = [None] * len(texts)

    # 1) cache hit 먼저 채우기
    to_do_idx = []
    to_do_texts = []
    for i, k in enumerate(keys):
        if k in cache:
            results[i] = cache[k]
        else:
            to_do_idx.append(i)
            to_do_texts.append(protected_list[i])

    # 2) batch translate
    for start in range(0, len(to_do_texts), batch_size):
        chunk = to_do_texts[start:start + batch_size]
        outs = translator.translate_batch(chunk)
        for j, out in enumerate(outs):
            idx = to_do_idx[start + j]
            # restore tokens
            out = restore_tokens(out, placeholders_list[idx])
            # glossary + postprocess
            out = postprocess_ko(apply_glossary(out, glossary))

            # HF가 영문을 그대로 내놓는 케이스 방어: 한글 없으면 원문 유지
            if not has_hangul(out):
                out = texts[idx]

            results[idx] = out
            # cache store (원문과 동일한 경우는 저장하지 않음)
            if out != texts[idx]:
                cache[keys[idx]] = out

    return results


def maybe_already_translated(out_json_path: Path) -> bool:
    """
    이미 생성된 json이 있고, 그 안의 target fields가 한글이면 스킵.
    """
    if not out_json_path.exists():
        return False
    try:
        obj = json.loads(out_json_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    # target fields 중 문자열이 하나라도 영문이면 재번역 필요
    targets = collect_targets(obj)
    if not targets:
        return False

    for path, key, value in targets:
        v = get_by_path(obj, path).get(key) if isinstance(get_by_path(obj, path), dict) else None
        if isinstance(value, str):
            if not has_hangul(value):
                return False
        elif isinstance(value, list):
            if any(isinstance(x, str) and not has_hangul(x) for x in value):
                return False

    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--cache", default=".cache/zone_nova_translate_cache_free_hf.json")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    ap.add_argument("--model_name", default="Helsinki-NLP/opus-mt-tc-big-en-ko")
    ap.add_argument("--num_beams", type=int, default=2)
    ap.add_argument("--batch_size", type=int, default=16)
    ap.add_argument("--skip_if_translated", action="store_true")
    args = ap.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    cache_path = Path(args.cache)
    glossary_path = Path(args.glossary)

    if not src_dir.exists():
        raise RuntimeError(f"Source directory not found: {src_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    cache = load_cache(cache_path)
    glossary = load_glossary(glossary_path)

    translator = HFTranslator(args.model_name, num_beams=args.num_beams)

    files = sorted(src_dir.glob("*.js"))
    total_fields = 0
    total_changed = 0

    for idx, js_file in enumerate(files, start=1):
        out_path = out_dir / f"{js_file.stem}.json"

        if args.skip_if_translated and maybe_already_translated(out_path):
            print(f"[SKIP] {idx}/{len(files)} {js_file.name} (already translated)")
            continue

        module_obj = import_js_module(js_file)
        export_key, data_obj = select_character_data_export(module_obj)

        targets = collect_targets(data_obj)

        # gather strings
        field_refs = []  # (container, key, old_value)
        texts = []
        for path, key, value in targets:
            container = get_by_path(data_obj, path)
            if isinstance(value, str):
                field_refs.append((container, key, value))
                texts.append(value)
            elif isinstance(value, list):
                # list[str]
                for i2, item in enumerate(value):
                    # we update in-place by index
                    # to keep ref simple: store (list_obj, index, old)
                    field_refs.append((value, i2, item))
                    texts.append(item)

        total_fields += len(texts)
        if not texts:
            out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[OK] {idx}/{len(files)} {js_file.name} export={export_key} | targets=0")
            continue

        # translate bulk
        translated = translate_texts_bulk(
            translator=translator,
            texts=texts,
            glossary=glossary,
            cache=cache,
            batch_size=max(1, args.batch_size),
        )

        # apply
        changed_here = 0
        for (container, key_or_idx, old), new in zip(field_refs, translated):
            if new != old:
                container[key_or_idx] = new
                changed_here += 1

        total_changed += changed_here

        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {idx}/{len(files)} {js_file.name} export={export_key} | targets={len(texts)} changed={changed_here}")

        # 중간 캐시 flush (긴 작업 중 안전)
        if idx % 10 == 0:
            save_cache(cache_path, cache)
            print(f"[PROGRESS] saved cache at file {idx}")

    save_cache(cache_path, cache)
    print(f"Done. files={len(files)} total_targets={total_fields} changed={total_changed}")


if __name__ == "__main__":
    main()
