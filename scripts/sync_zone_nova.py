import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]  # /nova
PUBLIC_DATA_DIR = REPO_ROOT / "public" / "data" / "zone-nova"
DETAIL_DIR = PUBLIC_DATA_DIR / "characters"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ---- Local overrides (repo files) ----
OVERRIDE_NAMES = PUBLIC_DATA_DIR / "overrides_names.json"
OVERRIDE_FACTIONS = PUBLIC_DATA_DIR / "overrides_factions.json"

# ---- Element rename (game changed) ----
ELEMENT_MAP = {
    "Ice": "Frost",
    "Wind": "Storm",
    "Fire": "Blaze",
    "Frost": "Frost",
    "Storm": "Storm",
    "Blaze": "Blaze",
    "Holy": "Holy",
    "Chaos": "Chaos",
}

# ---- Faction rename (combo/faction fixed names) ----
FACTION_NAME_MAP = {
    "A.S.A": "Asa",
    "Bicta Tower": "Bikta",
    "Chemic": "Kemich",
    "Monochrome Nation": "Monochrome Realm",
    "Oduis": "Otis",
    "Odius": "Otis",  # upstream 오타 방어
    "Pingjing City": "Heikyo Castle",
    "Sapphire": "Safir",
}

# ---- Class normalize + Role mapping ----
CLASS_ALIAS = {
    "guard": "guardian",
    "guardian": "guardian",
    "tank": "guardian",

    "healer": "healer",
    "buffer": "buffer",
    "support": "buffer",

    "debuffer": "disruptor",
    "debuff": "disruptor",
    "disruptor": "disruptor",

    "mage": "mage",
    "rogue": "rogue",
    "warrior": "warrior",

    "dps": "warrior",  # 잘못 들어온 값 방어
}

CLASS_TO_ROLE = {
    "guardian": "tank",
    "healer": "healer",
    "buffer": "buffer",
    "mage": "dps",
    "rogue": "dps",
    "warrior": "dps",
    "disruptor": "dps",  # 요구사항: Disruptor는 역할에서 DPS로 보이게
}

VALID_RARITY = {"SSR", "SR", "R"}

# ----------------------------
# Translation controls
# ----------------------------
TRANSLATE_MODE = os.getenv("TRANSLATE_MODE", "none").strip().lower()  # none | openai
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
TRANSLATE_CACHE = PUBLIC_DATA_DIR / "_translate_cache_ko.json"

# “번역 대상 경로” (사용자 지정)
# 1) skills/*/description
# 2) teamSkill/*/description, teamSkill/*/alternativeConditions
# 3) awakenings/*/effect
# 4) memoryCard/effects/** (하위 모든 문자열)
def _should_translate_path(path: Tuple[str, ...]) -> bool:
    if not path:
        return False
    last = path[-1]

    if "skills" in path and last == "description":
        return True

    if "teamSkill" in path and last in ("description", "alternativeConditions"):
        return True

    if "awakenings" in path and last == "effect":
        return True

    if "memoryCard" in path:
        try:
            i = path.index("memoryCard")
            if "effects" in path[i + 1:]:
                return True
        except ValueError:
            pass

    return False


_PROTECTED_SPAN_RE = re.compile(r"(`[^`]*`|\{[^{}]*\}|<[^<>]*>|\[[^\[\]]*\])")

# 게임 용어 사전(후처리): 필요하면 여기만 계속 만지면 됨
_GAME_GLOSSARY_PATTERNS: list[tuple[str, str, int]] = [
    # cooldown / duration
    (r"재사용\s*대기\s*시간", "쿨타임", 0),
    (r"\bCooldown\b", "쿨타임", re.IGNORECASE),
    (r"지속\s*시간", "지속시간", 0),

    # buff/debuff/dispell/immune
    (r"\bBuffs?\b", "버프", re.IGNORECASE),
    (r"\bDebuffs?\b", "디버프", re.IGNORECASE),
    (r"\bDispel(s|led|ling)?\b", "해제", re.IGNORECASE),
    (r"\bRemove(s|d)?\b", "제거", re.IGNORECASE),
    (r"\bImmune\b", "면역", re.IGNORECASE),

    # stack/turn
    (r"\bStacks?\b", "중첩", re.IGNORECASE),
    (r"\bTurn(s)?\b", "턴", re.IGNORECASE),

    # damage phrasing
    (r"피해를\s*입힌다", "피해를 준다", 0),
    (r"추가\s*피해량", "추가 피해", 0),
    (r"받는\s*피해량", "받는 피해", 0),
    (r"가하는\s*피해량", "가하는 피해", 0),

    # heal/shield
    (r"\bHeal(s|ed|ing)?\b", "회복", re.IGNORECASE),
    (r"\bShield(s|ed|ing)?\b", "보호막", re.IGNORECASE),
    (r"\bBarrier\b", "보호막", re.IGNORECASE),

    # stats consistency (번역 결과가 한글로 튀어나온 경우 정리)
    (r"공격\s*력", "공격력", 0),
    (r"방어\s*력", "방어력", 0),
    (r"체\s*력", "체력", 0),

    # crit
    (r"\bCrit(ical)?\s*Rate\b", "치명타 확률", re.IGNORECASE),
    (r"\bCrit(ical)?\s*Damage\b", "치명타 피해", re.IGNORECASE),
]

def _apply_game_glossary(text: str) -> str:
    if not text:
        return text
    parts = _PROTECTED_SPAN_RE.split(text)
    for i in range(len(parts)):
        seg = parts[i]
        if not seg or _PROTECTED_SPAN_RE.fullmatch(seg):
            continue
        for pat, rep, flags in _GAME_GLOSSARY_PATTERNS:
            seg = re.sub(pat, rep, seg, flags=flags)
        parts[i] = seg
    return "".join(parts)

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def _load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def normalize_rarity(v: str) -> str:
    s = (v or "").strip().upper()
    return s if s in VALID_RARITY else "-"

def normalize_element(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return "-"
    return ELEMENT_MAP.get(s, s)

def normalize_class(v: str) -> str:
    s = (v or "").strip().lower()
    if not s:
        return "-"
    return CLASS_ALIAS.get(s, s)

def class_to_role(cls: str) -> str:
    c = normalize_class(cls)
    return CLASS_TO_ROLE.get(c, "-")

def apply_faction_map(faction: str, overrides_factions: dict) -> str:
    f = (faction or "").strip()
    if not f:
        return ""
    # 1) 사용자 overrides 우선
    if f in overrides_factions:
        f = overrides_factions[f]
    # 2) 고정 변환
    return FACTION_NAME_MAP.get(f, f)

def apply_name_override(name: str, overrides_names: dict) -> str:
    n = (name or "").strip()
    if not n:
        return n
    return overrides_names.get(n, n)

def run_node_extract(upstream_char_dir: Path, out_json: Path):
    extractor = SCRIPTS_DIR / "extract_zone_nova_characters.mjs"
    if not extractor.exists():
        raise RuntimeError(f"extractor 파일이 없습니다: {extractor}")

    cmd = ["node", str(extractor), "--dir", str(upstream_char_dir), "--out", str(out_json)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Node 변환 실패:\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}\n"
        )

def _node_dump_js_to_json(js_file: Path) -> dict:
    """
    gacha-wiki의 character detail은 .js(ESM)로 되어 있으므로
    Node로 dynamic import 후 JSON으로 덤프한다.
    """
    tmp_script = REPO_ROOT / ".tmp_dump_detail.mjs"
    if not tmp_script.exists():
        tmp_script.write_text(
            """
import { pathToFileURL } from "url";
import path from "path";

const p = process.argv[2];
if (!p) {
  console.error("missing file path");
  process.exit(2);
}
const url = pathToFileURL(path.resolve(p)).href;

try {
  const mod = await import(url);
  const data = mod?.default ?? mod?.character ?? mod?.data ?? mod;
  process.stdout.write(JSON.stringify(data ?? {}, null, 2));
} catch (e) {
  console.error(String(e?.stack ?? e));
  process.exit(1);
}
""".strip() + "\n",
            encoding="utf-8",
        )

    proc = subprocess.run(
        ["node", str(tmp_script), str(js_file)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "node import failed")
    return json.loads(proc.stdout)

def _should_translate_string(s: str) -> bool:
    s2 = (s or "").strip()
    if not s2:
        return False
    # 숫자/기호 위주면 패스
    if re.fullmatch(r"[\d\s\W]+", s2):
        return False
    # 너무 짧으면 품질이 흔들려서(예: "Deal DMG") 굳이 번역 안함
    if len(s2) <= 2:
        return False
    return True

def _openai_translate_ko(text: str, api_key: str, model: str) -> str:
    """
    OpenAI Chat Completions API(기본)로 번역.
    """
    import urllib.request

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional game localization translator for Korean (ko-KR). "
                    "Translate the user's English text into natural Korean suitable for in-game UI, skill tooltips, and effects.\n"
                    "\n"
                    "Hard rules:\n"
                    "1) Output ONLY the translated text. No explanations, no quotes, no extra commentary.\n"
                    "2) Preserve formatting exactly (line breaks, bullet points, punctuation, spacing). If the input uses Markdown, keep Markdown.\n"
                    "3) Do NOT change numbers, percentages, units, or symbols (+, -, ×, /, =, →, ↑, ↓). Keep them exactly.\n"
                    "4) Keep placeholders/tokens as-is: anything in backticks `...`, {braces}, <tags>, [brackets], or variables like %s, {0}, {value}.\n"
                    "5) Keep proper nouns as-is when they look like names (character/skill/item names). If unsure, keep as-is.\n"
                    "6) Keep ALL-CAPS abbreviations and stat tokens unchanged (e.g., HP, ATK, DEF, SPD, CRIT, DMG, DoT, AoE, CC, CD).\n"
                    "\n"
                    "Terminology preferences:\n"
                    "- cooldown → 쿨타임\n"
                    "- duration → 지속시간\n"
                    "- buff/debuff → 버프/디버프\n"
                    "- dispel/remove → 해제\n"
                    "- shield/barrier → 보호막\n"
                    "- stack → 중첩\n"
                    "- damage dealt / damage taken → 가하는 피해 / 받는 피해\n"
                    "\n"
                    "Style:\n"
                    "- Keep sentences concise. Do not add information not present in the source.\n"
                    "- Avoid overly literal translation; prefer natural KR phrasing while preserving meaning.\n"
                ),
            },
            {"role": "user", "content": text},
        ],
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    j = json.loads(raw)
    out = (j.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    out = _apply_game_glossary(out)
    return out

def _translate_detail_selected(detail: Any, cache: dict) -> Any:
    def tr(s: str, path: Tuple[str, ...]) -> str:
        if TRANSLATE_MODE != "openai":
            return s
        if not _should_translate_path(path):
            return s
        if not _should_translate_string(s):
            return s
        if not OPENAI_API_KEY:
            return s

        key = _sha1(s)
        if key in cache:
            return cache[key]

        # 과도한 호출 방지(액션 환경)
        time.sleep(0.05)

        ko = _openai_translate_ko(s, OPENAI_API_KEY, OPENAI_MODEL)
        cache[key] = ko if ko else s
        return cache[key]

    def walk(obj, path: Tuple[str, ...] = ()):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                # 캐릭터명은 항상 영문 유지
                if k == "name" and isinstance(v, str):
                    out[k] = v
                    continue
                out[k] = walk(v, path + (k,))
            return out
        if isinstance(obj, list):
            return [walk(x, path + ("[]",)) for x in obj]
        if isinstance(obj, str):
            return tr(obj, path)
        return obj

    return walk(detail)

def build_characters_meta(raw_list: list, local_chars_json: list, overrides_names: dict, overrides_factions: dict) -> dict:
    chars: list[dict] = []

    def add_one(c: dict):
        cid = (c.get("id") or "").strip()
        if not cid:
            return

        name = apply_name_override((c.get("name") or cid).strip(), overrides_names)
        rarity = normalize_rarity(c.get("rarity") or "")
        element = normalize_element(c.get("element") or "")
        cls = normalize_class(c.get("class") or c.get("Class") or "")
        faction = apply_faction_map(c.get("faction") or c.get("Faction") or "", overrides_factions)
        role = class_to_role(cls)

        chars.append({
            "id": cid,
            "name": name,
            "rarity": rarity,
            "element": element,
            "class": cls,
            "role": role,
            "faction": faction,
        })

    # 1) upstream extractor 결과
    for c in raw_list:
        if isinstance(c, dict):
            add_one(c)

    # 2) 로컬 characters.json 병합(업스트림 meta 누락(Apep/Gaia) 방어)
    for c in (local_chars_json or []):
        if isinstance(c, dict):
            add_one(c)

    # 3) 중복 제거(id 기준 최종)
    dedup: dict[str, dict] = {}
    for c in chars:
        dedup[c["id"]] = c
    chars = list(dedup.values())

    # 4) 정렬: rarity(SSR>SR>R>-), 이름
    rarity_rank = {"SSR": 0, "SR": 1, "R": 2, "-": 9}
    chars.sort(key=lambda x: (rarity_rank.get(x.get("rarity", "-"), 9), (x.get("name") or "")))

    last_refresh = datetime.now(timezone.utc).isoformat()
    factions = sorted({c["faction"] for c in chars if c.get("faction")})
    elements = sorted({c["element"] for c in chars if c.get("element")})
    classes = sorted({c["class"] for c in chars if c.get("class")})

    return {
        "last_refresh": last_refresh,
        "count": len(chars),
        "factions_count": len(factions),
        "factions": factions,
        "elements": elements,
        "classes": classes,
        "characters": chars,
    }

def sync_details_from_upstream(upstream_char_dir: Path, out_dir: Path) -> dict:
    """
    upstream_char_dir: gacha-wiki/src/data/zone-nova/characters (JS files)
    out_dir: public/data/zone-nova/characters (JSON)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    cache = _load_json(TRANSLATE_CACHE, default={})

    detail_count = 0
    failed = []

    # *.js 만 대상으로
    for js_file in sorted(upstream_char_dir.glob("*.js")):
        stem = js_file.stem.strip()
        if not stem:
            continue

        try:
            detail = _node_dump_js_to_json(js_file)
            if not isinstance(detail, dict) or not detail:
                raise RuntimeError("empty detail")

            # 번역은 지정 범위만
            if TRANSLATE_MODE == "openai":
                detail = _translate_detail_selected(detail, cache)

            out_path = out_dir / f"{stem}.json"
            _save_json(out_path, detail)
            detail_count += 1

        except Exception as e:
            failed.append({"file": str(js_file), "id": stem, "error": str(e)})

    # cache 저장(비용 절감 핵심)
    if TRANSLATE_MODE == "openai":
        _save_json(TRANSLATE_CACHE, cache)

    return {"detail_count": detail_count, "failed": failed}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write json into public/data/zone-nova")
    ap.add_argument("--upstream", type=str, required=True, help="upstream repo folder name (cloned path)")
    ap.add_argument("--sync-details", action="store_true", help="sync character detail files into public/data/zone-nova/characters")
    ap.add_argument("--clean", action="store_true", help="delete generated outputs before syncing (fresh start)")
    args = ap.parse_args()

    upstream_root = REPO_ROOT / args.upstream
    if not upstream_root.exists():
        raise RuntimeError(f"업스트림 루트가 존재하지 않습니다: {upstream_root}")

    upstream_char_dir = upstream_root / "src" / "data" / "zone-nova" / "characters"
    if not upstream_char_dir.exists():
        raise RuntimeError(f"업스트림 캐릭터 디렉터리가 없습니다: {upstream_char_dir}")

    # ---- clean outputs (fresh restart) ----
    if args.clean and args.write:
        if DETAIL_DIR.exists():
            shutil.rmtree(DETAIL_DIR)
        tmp = REPO_ROOT / ".tmp_zone_nova_characters.json"
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    # ---- load overrides ----
    overrides_names = _load_json(OVERRIDE_NAMES, default={})
    overrides_factions = _load_json(OVERRIDE_FACTIONS, default={})

    # ---- upstream meta extract via node (existing extractor) ----
    tmp_out = REPO_ROOT / ".tmp_zone_nova_characters.json"
    run_node_extract(upstream_char_dir, tmp_out)
    raw = json.loads(tmp_out.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("추출 결과 포맷 오류: list여야 합니다.")

    # ---- local characters.json merge source ----
    local_char_path = PUBLIC_DATA_DIR / "characters.json"
    local_chars = _load_json(local_char_path, default=[])
    if not isinstance(local_chars, list):
        local_chars = []

    meta = build_characters_meta(raw, local_chars, overrides_names, overrides_factions)

    # ---- write outputs ----
    if args.write:
        PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
        _save_json(PUBLIC_DATA_DIR / "characters_meta.json", meta)

        # 디테일 동기화
        detail_res = {"detail_count": 0, "failed": []}
        if args.sync_details:
            detail_res = sync_details_from_upstream(upstream_char_dir, DETAIL_DIR)

        # 실패 목록 저장
        _save_json(PUBLIC_DATA_DIR / "_unmatched_gacha_wiki.json", detail_res.get("failed", []))

        print(f"[ok] characters_meta.json generated: count={meta['count']}")
        print(f"[ok] detail_count={detail_res.get('detail_count', 0)} failed={len(detail_res.get('failed', []))}")

    else:
        print(json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
