import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Optional HF fallback (FREE). If --fallback none, no need transformers.
HF_AVAILABLE = True
try:
    import torch
    from transformers import MarianMTModel, MarianTokenizer
except Exception:
    HF_AVAILABLE = False


# =========================================================
# 0) Cache
# =========================================================
def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_cache(cache_path: Path) -> Dict[str, str]:
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    return {}

def save_cache(cache_path: Path, cache: Dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================================================
# 1) JS module import (default + named export)
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
# 2) Glossary
# =========================================================
def load_glossary(path: Path) -> Dict[str, str]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}

def apply_glossary(text: str, glossary: Dict[str, str]) -> str:
    if not glossary:
        return text
    # 긴 키 먼저 치환
    for k in sorted(glossary.keys(), key=len, reverse=True):
        v = glossary[k]
        if k and v:
            text = text.replace(k, v)
    return text


# =========================================================
# 3) Token protection
# =========================================================
TOKEN_PATTERNS = [
    r"\{[^}]+\}",   # {x}
    r"\[[^\]]+\]",  # [Nightmare]
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


# =========================================================
# 4) KO tone postprocess (존댓말 제거 + 툴팁 톤)
# =========================================================
def postprocess_ko(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t

    # 존댓말 -> 툴팁 톤
    t = re.sub(r"합니다\.", "한다.", t)
    t = re.sub(r"됩니다\.", "된다.", t)
    t = re.sub(r"입니다\.", "이다.", t)
    t = re.sub(r"합니다$", "한다", t)
    t = re.sub(r"됩니다$", "된다", t)
    t = re.sub(r"입니다$", "이다", t)

    # 어색한 직역 교정
    t = re.sub(r"지정된 적", "지정한 적", t)
    t = re.sub(r"지정된 대상", "지정한 대상", t)
    t = re.sub(r"대상 적", "대상인 적", t)

    # 표기 정리
    t = re.sub(r"\s*%\s*", "%", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\.\.+", ".", t)
    return t.strip()


def has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))

def normalize_en(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"[ \t]+", " ", t)
    return t


# =========================================================
# 5) Rule-based translator (Afrodite-style 확장판)
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

def ko_target_from_en(s: str) -> str:
    ss = (s or "").lower()
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

    # (R1) Deals 120% attack power Chaos damage to designated enemy unit.
    m = re.match(
        r"^Deals\s+(?P<pct>\d+(?:\.\d+)?)%\s*attack\s*power\s*(?P<dtype>(Holy|Chaos|Fire|Water|Wind|Dark|Light|Physical)\s+damage)?\s*to\s+(?P<tgt>.+?)\.$",
        t, re.I
    )
    if m:
        pct = m.group("pct")
        dtype = m.group("dtype") or ""
        elem = DMG_TYPE.get(dtype.split()[0].lower()) if dtype else None
        tgt = ko_target_from_en(m.group("tgt"))
        dmg = f"{elem} 피해" if elem else "피해"
        return f"{tgt}에게 공격력의 {pct}%만큼 {dmg}를 준다."

    # (R2) Deals fire damage equal to 120% of Attack to designated enemy.
    m = re.match(
        r"^Deals\s+(?P<elem>holy|chaos|fire|water|wind|dark|light|physical)\s+damage\s+equal\s+to\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+Attack\s+to\s+(?P<tgt>.+?)\.$",
        t, re.I
    )
    if m:
        pct = m.group("pct")
        elem = DMG_TYPE[m.group("elem").lower()]
        tgt = ko_target_from_en(m.group("tgt"))
        return f"{tgt}에게 공격력의 {pct}%만큼 {elem} 피해를 준다."

    # (R3) Allied units gain 24% damage increase.
    m = re.match(r"^Allied\s+units\s+gain\s+(?P<x>\d+(?:\.\d+)?)%\s+damage\s+increase\.$", t, re.I)
    if m:
        x = m.group("x")
        return f"아군 전체가 주는 피해가 {x}% 증가한다."

    # (R4) Battle start: Team damage increases by 10%.
    m = re.match(r"^Battle\s+start:\s*Team\s+damage\s+increases\s+by\s+(?P<x>\d+(?:\.\d+)?)%\.$", t, re.I)
    if m:
        x = m.group("x")
        return f"전투 시작 시 팀이 주는 피해가 {x}% 증가한다."

    # (R5) Every 500 attack power adds 5% team damage, maximum 6 times (30% max).
    m = re.match(
        r"^Every\s+(?P<a>\d+)\s+attack\s+power\s+adds\s+(?P<b>\d+(?:\.\d+)?)%\s+team\s+damage,\s*maximum\s+(?P<n>\d+)\s+times\s*\((?P<max>\d+(?:\.\d+)?)%\s+max\)\.$",
        t, re.I
    )
    if m:
        a, b, n, mx = m.group("a"), m.group("b"), m.group("n"), m.group("max")
        return f"공격력 {a}마다 팀이 주는 피해가 {b}% 증가한다(최대 {n}회, 최대 {mx}%)."

    # (R6) During [Day Brilliance] state, damage taken reduced by 50%.
    m = re.match(r"^During\s+\[[^\]]+\]\s+state,\s*damage\s+taken\s+reduced\s+by\s+(?P<x>\d+(?:\.\d+)?)%\.$", t, re.I)
    if m:
        x = m.group("x")
        state = re.search(r"\[[^\]]+\]", t).group(0)
        return f"{state} 상태 동안 받는 피해가 {x}% 감소한다."

    # (R7) Attack power increased by 40%.
    m = re.match(r"^Attack\s+power\s+increased\s+by\s+(?P<x>\d+(?:\.\d+)?)%\.*$", t, re.I)
    if m:
        x = m.group("x")
        return f"공격력이 {x}% 증가한다."

    return None


# =========================================================
# 6) FREE HF fallback translator (optional)
# =========================================================
class HFTranslator:
    def __init__(self, model_name: str):
        if not HF_AVAILABLE:
            raise RuntimeError("HF fallback requested but transformers/torch not installed.")
        self.model_name = model_name
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
        return [x.strip() for x in out]

    def translate_one(self, text: str) -> str:
        if self.tgt_token:
            out = self._generate([f"{self.tgt_token} {text}"])[0]
            if out:
                return out
        for tok in [">>kor_Hang<<", ">>kor<<", ">>ko<<"]:
            out = self._generate([f"{tok} {text}"])[0]
            if out:
                return out
        return self._generate([text])[0]


# =========================================================
# 7) The pipeline per string:
#    - line split 유지
#    - rule 적용 -> 성공 시 즉시 사용
#    - rule 실패 시 fallback(hf/keep/mark)
#    - glossary + postprocess
# =========================================================
def translate_string_pipeline(
    s: str,
    glossary: Dict[str, str],
    fallback: str,
    hf: Optional[HFTranslator],
    cache: Dict[str, str],
    untranslated_sink: List[str],
    context_tag: str,
) -> str:
    if not isinstance(s, str) or not s.strip():
        return s

    # 이미 한글이면 후처리만
    if has_hangul(s):
        return postprocess_ko(apply_glossary(s, glossary))

    cache_key = sha(f"{fallback}|{getattr(hf, 'model_name', '')}|{s}")
    if cache_key in cache:
        return cache[cache_key]

    lines = s.split("\n")
    out_lines: List[str] = []

    for line in lines:
        raw = line.strip()
        if not raw:
            out_lines.append(line)
            continue

        protected, ph = protect_tokens(raw)
        ruled = rule_translate_line(protected)

        if ruled is not None:
            out = restore_tokens(ruled, ph)
            out = postprocess_ko(apply_glossary(out, glossary))
            out_lines.append(out)
            continue

        # rule 실패 -> fallback
        if fallback == "keep":
            out_lines.append(raw)
            untranslated_sink.append(f"{context_tag}\t{raw}")
            continue

        if fallback == "mark":
            out_lines.append("[UNTRANSLATED] " + raw)
            untranslated_sink.append(f"{context_tag}\t{raw}")
            continue

        if fallback == "hf":
            if hf is None:
                raise RuntimeError("fallback=hf but HFTranslator not initialized.")
            # HF 번역 후 후처리
            ko = hf.translate_one(protected)
            ko = restore_tokens(ko, ph)
            ko = postprocess_ko(apply_glossary(ko, glossary))
            out_lines.append(ko)

            # 그래도 영문이 대부분이면(실패로 간주) 원문 유지로 롤백 + 리포트
            # (부자연 방지)
            if not has_hangul(ko):
                out_lines[-1] = raw
                untranslated_sink.append(f"{context_tag}\t{raw}")
            continue

        raise RuntimeError(f"Unknown fallback: {fallback}")

    out = "\n".join(out_lines)
    if out != s:
        cache[cache_key] = out
    return out


# =========================================================
# 8) Recursive discovery: requested fields only
# =========================================================
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


def walk_and_translate(
    obj: Any,
    glossary: Dict[str, str],
    fallback: str,
    hf: Optional[HFTranslator],
    cache: Dict[str, str],
    untranslated_sink: List[str],
    file_tag: str,
) -> int:
    changed = 0

    def _walk(node: Any, path: List[Any]):
        nonlocal changed

        if isinstance(node, dict):
            for k, v in list(node.items()):
                if is_target_field(path + [k], k, v):
                    context_tag = f"{file_tag}:{'/'.join(str(x) for x in path+[k])}"
                    if isinstance(v, str):
                        new_v = translate_string_pipeline(
                            v, glossary, fallback, hf, cache, untranslated_sink, context_tag
                        )
                    else:
                        # list[str] only
                        new_list = []
                        for idx, item in enumerate(v):
                            ctx = f"{context_tag}[{idx}]"
                            new_list.append(
                                translate_string_pipeline(
                                    item, glossary, fallback, hf, cache, untranslated_sink, ctx
                                )
                            )
                        new_v = new_list

                    if new_v != v:
                        node[k] = new_v
                        changed += 1

                _walk(node[k], path + [k])

        elif isinstance(node, list):
            for i in range(len(node)):
                _walk(node[i], path + [i])

    _walk(obj, [])
    return changed


# =========================================================
# 9) main
# =========================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    ap.add_argument("--fallback", choices=["hf", "keep", "mark"], default="hf",
                    help="rule 실패 시 처리: hf(무료번역), keep(원문유지), mark(표시)")
    ap.add_argument("--model_name", default="Helsinki-NLP/opus-mt-tc-big-en-ko")
    ap.add_argument("--cache", default=".cache/zone_nova_translate_cache_free_hf.json")
    ap.add_argument("--report", default="public/data/zone-nova/translation_unmatched.tsv")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    glossary_path = Path(args.glossary)
    cache_path = Path(args.cache)
    report_path = Path(args.report)

    if not src_dir.exists():
        raise RuntimeError(f"Source directory not found: {src_dir}")

    glossary = load_glossary(glossary_path)
    cache = load_cache(cache_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    hf = None
    if args.fallback == "hf":
        if not HF_AVAILABLE:
            raise RuntimeError("fallback=hf인데 torch/transformers가 설치되지 않았다. workflow의 pip install을 확인해라.")
        hf = HFTranslator(args.model_name)

    untranslated: List[str] = []
    total_changed = 0
    files = sorted(src_dir.glob("*.js"))

    for js_file in files:
        module_obj = import_js_module(js_file)
        export_key, data_obj = select_character_data_export(module_obj)

        file_tag = js_file.stem
        changed = walk_and_translate(
            data_obj, glossary, args.fallback, hf, cache, untranslated, file_tag
        )
        total_changed += changed

        out_path = out_dir / f"{js_file.stem}.json"
        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.debug:
            print(f"[DEBUG] {js_file.name} export={export_key} changed_fields={changed}")
        else:
            print(f"[OK] {js_file.name} -> {out_path.name} | changed_fields={changed}")

    # report: rule 실패(keep/mark/hf 실패) 문장 목록
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if untranslated:
        header = "context\ttext\n"
        report_path.write_text(header + "\n".join(untranslated), encoding="utf-8")
    else:
        # 기존 리포트가 있으면 비워둠
        report_path.write_text("context\ttext\n", encoding="utf-8")

    save_cache(cache_path, cache)
    print(f"Done. Total changed fields: {total_changed}")
    print(f"Unmatched report: {report_path} (rows={len(untranslated)})")


if __name__ == "__main__":
    main()
