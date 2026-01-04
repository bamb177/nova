import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch
from transformers import MarianMTModel, MarianTokenizer


# =========================
# Utilities
# =========================
def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))

def hangul_ratio(s: str) -> float:
    if not s:
        return 0.0
    total = len(s)
    if total == 0:
        return 0.0
    h = sum(1 for ch in s if "가" <= ch <= "힣")
    return h / total

def letters_ratio(s: str) -> float:
    # 영문/숫자 비율(대략)
    if not s:
        return 0.0
    total = len(s)
    if total == 0:
        return 0.0
    cnt = sum(1 for ch in s if ("a" <= ch.lower() <= "z") or ("0" <= ch <= "9"))
    return cnt / total

def normalize_spaces(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s or "").strip()
    return s

def parse_only_list(s: str) -> Optional[set]:
    if not s:
        return None
    items = [x.strip() for x in s.split(",") if x.strip()]
    if not items:
        return None
    out = set()
    for it in items:
        if it.endswith(".js"):
            it = it[:-3]
        out.add(it)
    return out


# =========================
# Cache helpers
# =========================
def load_cache(cache_path: Path) -> Dict[str, str]:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            print(f"[WARN] cache load failed, start empty: {cache_path}")
            return {}
    return {}

def save_cache(cache_path: Path, cache: Dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


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
            if "memoryCard" in v:
                score += 2
            if "awakenings" in v or "awakeningEffects" in v:
                score += 2
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

        t0 = time.time()
        print(f"[MODEL] loading tokenizer: {model_name}")
        self.tokenizer = MarianTokenizer.from_pretrained(model_name)
        print(f"[MODEL] loading model: {model_name}")
        self.model = MarianMTModel.from_pretrained(model_name)
        self.model.eval()
        self.device = torch.device("cpu")
        self.model.to(self.device)

        self.tgt_token = self._pick_korean_target_token()
        dt = time.time() - t0
        print(f"[MODEL] ready in {dt:.1f}s | beams={self.num_beams} | tgt_token={self.tgt_token}")

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

    # skills/**/description, teamSkill/**/description
    if key == "description" and isinstance(value, str):
        if path_has_ancestor(path, "skills") or path_has_ancestor(path, "teamSkill"):
            return True

    # awakenings/awakeningEffects/**/effect
    if key == "effect" and isinstance(value, str):
        if path_has_ancestor(path, "awakenings") or path_has_ancestor(path, "awakeningEffects"):
            return True

    # memoryCard/effects: list[str]
    if key == "effects" and isinstance(value, list) and all(isinstance(x, str) for x in value):
        if path_has_ancestor(path, "memoryCard"):
            return True

    return False

def collect_targets(obj: Any) -> List[Tuple[List[Any], str, Any]]:
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


# =========================
# Heartbeat logger
# =========================
class Heartbeat:
    def __init__(self, interval_sec: int = 30):
        self.interval = max(5, int(interval_sec))
        self.last = time.time()

    def ping(self, extra: str = ""):
        now = time.time()
        if now - self.last >= self.interval:
            msg = "[HEARTBEAT] still running"
            if extra:
                msg += f" | {extra}"
            print(msg)
            sys.stdout.flush()
            self.last = now


# =========================
# Quality gate (SAFE MODE 핵심)
# =========================
DEFAULT_BAD_TOKENS = [
    # 실사용 중 관찰된 “쓰레기 토큰” 유형을 방어적으로 잡음
    "betterstshell",
    "get denied",
    "cookies",
    "skillsHan",
    "nonbit",
    "volru",
    "por live",
    "annex",
    "theopen",
    "inglas",
    "reground",
]

def is_garbage_translation(
    src: str,
    dst: str,
    min_hangul_ratio: float,
    max_len_mul: float,
    bad_tokens: List[str],
) -> bool:
    if not dst or not dst.strip():
        return True

    # 금칙 토큰
    low = dst.lower()
    for tok in bad_tokens:
        if tok.lower() in low:
            return True

    # 한글 비율이 너무 낮으면 실패
    if hangul_ratio(dst) < min_hangul_ratio:
        return True

    # 길이 폭주 방지
    src_len = max(1, len(src))
    if len(dst) > max(200, int(src_len * max_len_mul)):
        return True

    # 제어문자/이상 문자 방지
    weird = sum(1 for ch in dst if ord(ch) < 32 and ch not in "\n\t")
    if weird > 0:
        return True

    return False


# =========================
# Translation bulk (cache + gate + report)
# =========================
def translate_texts_bulk(
    translator: HFTranslator,
    texts: List[str],
    glossary: Dict[str, str],
    cache: Dict[str, str],
    cache_salt: str,
    batch_size: int,
    heartbeat: Heartbeat,
    min_hangul_ratio: float,
    max_len_mul: float,
    bad_tokens: List[str],
    report_rolled_back: List[str],
    context_prefix: str,
) -> Tuple[List[str], int, int, int]:
    """
    returns (translated_texts, cache_hit_count, generated_count, rolled_back_count)
    """
    protected_list = []
    placeholders_list = []
    keys = []

    for t in texts:
        prot, ph = protect_tokens(t)
        protected_list.append(prot)
        placeholders_list.append(ph)
        # cache key에 salt를 넣어 “오염된 과거 캐시” 자동 무효화
        keys.append(sha(cache_salt + "|" + translator.model_name + "|" + t))

    results: List[Optional[str]] = [None] * len(texts)

    cache_hits = 0
    to_do_idx = []
    to_do_texts = []

    for i, k in enumerate(keys):
        if k in cache:
            cached = cache[k]
            # 캐시 오염 방지: 캐시 결과도 품질 게이트 통과 못하면 버림(재번역 또는 롤백)
            if is_garbage_translation(texts[i], cached, min_hangul_ratio, max_len_mul, bad_tokens):
                # 오염 캐시 제거
                del cache[k]
                to_do_idx.append(i)
                to_do_texts.append(protected_list[i])
            else:
                results[i] = cached
                cache_hits += 1
        else:
            to_do_idx.append(i)
            to_do_texts.append(protected_list[i])

    generated = 0
    rolled_back = 0

    for start in range(0, len(to_do_texts), batch_size):
        heartbeat.ping(extra=f"batch {start//batch_size + 1}/{(len(to_do_texts)+batch_size-1)//batch_size}")
        chunk = to_do_texts[start:start + batch_size]
        outs = translator.translate_batch(chunk)
        generated += len(chunk)

        for j, out in enumerate(outs):
            idx = to_do_idx[start + j]
            src = texts[idx]

            out = restore_tokens(out, placeholders_list[idx])
            out = postprocess_ko(apply_glossary(out, glossary))
            out = normalize_spaces(out)

            # 안전 게이트: 실패면 원문 롤백 + 캐시 저장 금지
            if is_garbage_translation(src, out, min_hangul_ratio, max_len_mul, bad_tokens):
                rolled_back += 1
                report_rolled_back.append(f"{context_prefix}\t{src}")
                out = src  # 롤백

            results[idx] = out

            # 캐시 저장: “변경 + 게이트 통과”만 저장
            if out != src and not is_garbage_translation(src, out, min_hangul_ratio, max_len_mul, bad_tokens):
                cache[keys[idx]] = out

    final_results = [r if r is not None else texts[i] for i, r in enumerate(results)]
    return final_results, cache_hits, generated, rolled_back


# =========================
# Skip-if-translated helper
# =========================
def maybe_already_translated(out_json_path: Path) -> bool:
    if not out_json_path.exists():
        return False
    try:
        obj = json.loads(out_json_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    targets = collect_targets(obj)
    if not targets:
        return False

    for _, _, value in targets:
        if isinstance(value, str):
            if not has_hangul(value):
                return False
        elif isinstance(value, list):
            if any(isinstance(x, str) and not has_hangul(x) for x in value):
                return False

    return True


# =========================
# Main
# =========================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--cache", default=".cache/zone_nova_translate_cache_free_hf.json")
    ap.add_argument("--cache_salt", default="safe-v2", help="캐시 무효화/버전용 salt")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    ap.add_argument("--model_name", default="Helsinki-NLP/opus-mt-tc-big-en-ko")
    ap.add_argument("--num_beams", type=int, default=2)
    ap.add_argument("--batch_size", type=int, default=16)
    ap.add_argument("--skip_if_translated", action="store_true")
    ap.add_argument("--heartbeat_sec", type=int, default=30)
    ap.add_argument("--flush_each_file", action="store_true")

    # test controls
    ap.add_argument("--only", default="", help="comma-separated stems, e.g. afrodite,anubis,apollo")
    ap.add_argument("--limit", type=int, default=0, help="process first N files after filtering")

    # safety gate controls
    ap.add_argument("--min_hangul_ratio", type=float, default=0.20, help="번역문 한글 비율 최소값")
    ap.add_argument("--max_len_mul", type=float, default=3.0, help="번역문 길이 폭주 제한(원문 대비 배수)")
    ap.add_argument("--report", default="public/data/zone-nova/translation_rolled_back.tsv", help="롤백된 원문 리포트")

    args = ap.parse_args()

    print("[START] translate_zone_nova_characters_free_hf_optimized.py (SAFE)")
    print(f"[ENV] HF_HOME={os.getenv('HF_HOME')}")
    print(f"[ARGS] src={args.src} out={args.out}")
    print(f"[ARGS] model={args.model_name} beams={args.num_beams} batch={args.batch_size}")
    print(f"[ARGS] cache={args.cache} salt={args.cache_salt} skip={args.skip_if_translated}")
    print(f"[ARGS] only={args.only} limit={args.limit} heartbeat={args.heartbeat_sec}s")
    print(f"[GATE] min_hangul_ratio={args.min_hangul_ratio} max_len_mul={args.max_len_mul}")
    sys.stdout.flush()

    heartbeat = Heartbeat(interval_sec=args.heartbeat_sec)

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    cache_path = Path(args.cache)
    glossary_path = Path(args.glossary)
    report_path = Path(args.report)

    if not src_dir.exists():
        raise RuntimeError(f"Source directory not found: {src_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)

    cache = load_cache(cache_path)
    glossary = load_glossary(glossary_path)

    print(f"[CACHE] loaded entries: {len(cache)}")
    print(f"[GLOSSARY] loaded entries: {len(glossary)}")
    sys.stdout.flush()

    translator = HFTranslator(args.model_name, num_beams=args.num_beams)

    files = sorted(src_dir.glob("*.js"))
    only_set = parse_only_list(args.only)
    if only_set is not None:
        files = [f for f in files if f.stem in only_set]
        print(f"[FILTER] only={sorted(list(only_set))} -> {len(files)} files")

    if args.limit and args.limit > 0:
        files = files[: args.limit]
        print(f"[FILTER] limit={args.limit} -> {len(files)} files")

    print(f"[SCAN] selected {len(files)} js files")
    sys.stdout.flush()

    total_targets = 0
    total_changed = 0
    total_cache_hits = 0
    total_generated = 0
    total_rolled_back = 0
    rolled_back_report_rows: List[str] = []

    bad_tokens = list(DEFAULT_BAD_TOKENS)

    for i, js_file in enumerate(files, start=1):
        heartbeat.ping(extra=f"file {i}/{len(files)}")

        out_path = out_dir / f"{js_file.stem}.json"

        if args.skip_if_translated and maybe_already_translated(out_path):
            print(f"[SKIP] {i}/{len(files)} {js_file.name} (already translated)")
            sys.stdout.flush()
            continue

        print(f"[FILE] {i}/{len(files)} loading {js_file.name}")
        sys.stdout.flush()

        t_file0 = time.time()
        module_obj = import_js_module(js_file)
        export_key, data_obj = select_character_data_export(module_obj)

        targets = collect_targets(data_obj)

        field_refs = []
        texts: List[str] = []
        contexts: List[str] = []

        for path, key, value in targets:
            container = get_by_path(data_obj, path)
            ctx = f"{js_file.stem}:{'/'.join(str(x) for x in (path+[key]))}"
            if isinstance(value, str):
                field_refs.append((container, key, value))
                texts.append(value)
                contexts.append(ctx)
            elif isinstance(value, list):
                for idx2, item in enumerate(value):
                    field_refs.append((value, idx2, item))
                    texts.append(item)
                    contexts.append(f"{ctx}[{idx2}]")

        total_targets += len(texts)

        if not texts:
            out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
            dt = time.time() - t_file0
            print(f"[OK] {i}/{len(files)} {js_file.name} export={export_key} | targets=0 | {dt:.1f}s")
            sys.stdout.flush()
            continue

        # translate in one bulk call, but report uses contexts
        # (context_prefix is per-field inside report rows)
        translated_all: List[str] = []
        cache_hits = 0
        generated = 0
        rolled_back = 0

        # 배치 번역은 전체 texts를 대상으로 하되, report에 context를 넣기 위해
        # translate_texts_bulk()에 prefix를 넣고, 실제 row에는 prefix+src가 들어가도록 처리
        # 여기서는 prefix를 임시로 넣고, 사후에 context 매핑으로 다시 저장한다.
        tmp_report: List[str] = []
        translated_all, cache_hits, generated, rolled_back = translate_texts_bulk(
            translator=translator,
            texts=texts,
            glossary=glossary,
            cache=cache,
            cache_salt=args.cache_salt,
            batch_size=max(1, args.batch_size),
            heartbeat=heartbeat,
            min_hangul_ratio=args.min_hangul_ratio,
            max_len_mul=args.max_len_mul,
            bad_tokens=bad_tokens,
            report_rolled_back=tmp_report,
            context_prefix="CTX",  # placeholder
        )

        # tmp_report에는 "CTX\t<원문>" 형태만 쌓이므로, 실제 context를 붙여 재구성
        # rolled_back 된 건 “src 그대로 반환된 항목”을 기준으로 잡는 게 더 정확하다.
        # 여기서는 translate_texts_bulk에서 rolled_back 카운트를 신뢰하고,
        # 실제 리포트는 contexts와 src를 함께 쌓는다.
        for ctx, src, dst in zip(contexts, texts, translated_all):
            if dst == src and not has_hangul(src):  # 영문인데 그대로면 롤백/미번역
                rolled_back_report_rows.append(f"{ctx}\t{src}")

        dt_tr = time.time() - t_file0

        total_cache_hits += cache_hits
        total_generated += generated
        total_rolled_back += rolled_back

        changed_here = 0
        for (container, key_or_idx, old), new in zip(field_refs, translated_all):
            if new != old:
                container[key_or_idx] = new
                changed_here += 1

        total_changed += changed_here

        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        dt_file = time.time() - t_file0

        print(
            f"[OK] {i}/{len(files)} {js_file.name} export={export_key} | "
            f"targets={len(texts)} changed={changed_here} | "
            f"cache_hit={cache_hits} generated={generated} rolled_back~={rolled_back} | "
            f"file={dt_file:.1f}s"
        )
        sys.stdout.flush()

        if args.flush_each_file:
            save_cache(cache_path, cache)
            print(f"[CACHE] saved (flush_each_file) entries={len(cache)}")
            sys.stdout.flush()
        elif i % 10 == 0:
            save_cache(cache_path, cache)
            print(f"[CACHE] saved at file {i} entries={len(cache)}")
            sys.stdout.flush()

    # Save cache
    save_cache(cache_path, cache)

    # Save report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    header = "context\ttext\n"
    report_path.write_text(header + "\n".join(rolled_back_report_rows), encoding="utf-8")

    print(f"[DONE] files={len(files)} total_targets={total_targets} changed={total_changed}")
    print(f"[DONE] cache_entries={len(cache)} cache_hits={total_cache_hits} generated={total_generated}")
    print(f"[DONE] rolled_back_report_rows={len(rolled_back_report_rows)} -> {report_path}")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL] translator crashed:")
        print(str(e))
        raise
