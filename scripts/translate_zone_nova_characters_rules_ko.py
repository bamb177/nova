import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple, List, Optional

# =========================================================
# A) JS module import (default + named export)
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
        if "name" in d and ("skills" in d or "teamSkill" in d):
            return ("default", d)

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


# =========================================================
# B) Glossary (string replace)
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
# C) Token protection (avoid breaking placeholders/tags)
# =========================================================
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


# =========================================================
# D) Style post-process (remove honorifics, tooltip tone)
# =========================================================
def postprocess_ko(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t

    # honorific -> plain tooltip tone
    rules = [
        (re.compile(r"합니다\.", re.M), "한다."),
        (re.compile(r"됩니다\.", re.M), "된다."),
        (re.compile(r"입니다\.", re.M), "이다."),
        (re.compile(r"합니다$", re.M), "한다"),
        (re.compile(r"됩니다$", re.M), "된다"),
        (re.compile(r"입니다$", re.M), "이다"),
        (re.compile(r"합니다,", re.M), "한다,"),
        (re.compile(r"됩니다,", re.M), "된다,"),
        (re.compile(r"입니다,", re.M), "이다,"),
    ]
    for pat, rep in rules:
        t = pat.sub(rep, t)

    # common unnatural phrases -> afrodite style
    t = re.sub(r"지정된 적", "지정한 적", t)
    t = re.sub(r"지정된 대상", "지정한 대상", t)

    # spacing
    t = re.sub(r"\s*%\s*", "%", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\.\.+", ".", t)
    return t.strip()


# =========================================================
# E) Core: rule-based EN->KO tooltip translator (Afrodite-style)
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

def ko_element_from_phrase(phrase: str) -> Optional[str]:
    # "holy damage" 등
    m = re.search(r"\b(holy|chaos|fire|water|wind|dark|light|physical)\s+damage\b", phrase, re.I)
    if not m:
        return None
    return DMG_TYPE.get(m.group(1).lower())

def rule_translate_line(en: str) -> Optional[str]:
    """
    성공하면 Afrodite-style 한국어 한 줄 반환, 실패하면 None
    """
    t = normalize_en(en)

    # 1) Deals X% attack power as <elem> damage to <target>.
    m = re.match(
        r"^Deals\s+(?P<pct>\d+(?:\.\d+)?)%\s*attack\s*power\s+as\s+(?P<elem>holy|chaos|fire|water|wind|dark|light|physical)\s+damage\s+to\s+(?P<tgt>.+?)\.$",
        t, re.I
    )
    if m:
        pct = m.group("pct")
        elem = DMG_TYPE[m.group("elem").lower()]
        tgt = ko_target_from_en(m.group("tgt"))
        return f"{tgt}에게 공격력의 {pct}%만큼 {elem} 피해를 준다."

    # 2) Automatically deals X% attack power as <elem> damage to <target>.
    m = re.match(
        r"^Automatically\s+deals\s+(?P<pct>\d+(?:\.\d+)?)%\s*attack\s*power\s+as\s+(?P<elem>holy|chaos|fire|water|wind|dark|light|physical)\s+damage\s+to\s+(?P<tgt>.+?)\.$",
        t, re.I
    )
    if m:
        pct = m.group("pct")
        elem = DMG_TYPE[m.group("elem").lower()]
        tgt = ko_target_from_en(m.group("tgt"))
        return f"{tgt}에게 공격력의 {pct}%만큼 {elem} 피해를 준다."

    # 3) Increases self <stat> by X%.
    m = re.match(
        r"^Increases\s+self\s+(?P<stat>attack\s+speed|attack\s+power|defense|max\s+hp|critical\s+rate|critical\s+damage)\s+by\s+(?P<val>\d+(?:\.\d+)?)%\.$",
        t, re.I
    )
    if m:
        stat = m.group("stat").lower()
        val = m.group("val")
        stat_map = {
            "attack speed": "공격 속도",
            "attack power": "공격력",
            "defense": "방어력",
            "max hp": "최대 HP",
            "critical rate": "치명타 확률",
            "critical damage": "치명타 피해",
        }
        return f"또한 자신의 {stat_map[stat]}가 {val}% 증가한다."

    # 4) Normal Ultimate: Deals ... Enhanced Ultimate (...): Deals ... , recovers ..., Counts as ...
    if t.lower().startswith("normal ultimate:"):
        # split into segments by ". " but keep meaning
        # 기대 형태: "Normal Ultimate: ... . Enhanced Ultimate (...): ... , recovers ... , Counts as ..."
        # 원문은 종종 마침표/콤마가 섞여 있으니 완전 엄격 파싱 대신 핵심 패턴만 추출
        # Normal Ultimate dmg
        n = re.search(
            r"Normal\s+Ultimate:\s+Deals\s+(?P<p1>\d+(?:\.\d+)?)%\s*attack\s*power\s+as\s+(?P<e1>\w+)\s+damage\s+to\s+(?P<t1>.+?)\.",
            t, re.I
        )
        e = re.search(
            r"Enhanced\s+Ultimate\s*\(after\s+(?P<hits>\d+)\s+normal\s+attacks\):\s+Deals\s+(?P<p2>\d+(?:\.\d+)?)%\s*attack\s*power\s+as\s+(?P<e2>\w+)\s+damage\s+to\s+(?P<t2>.+?)(?:,|\.|\s)",
            t, re.I
        )
        rec = re.search(r"recovers\s+(?P<eng>\d+)\s+energy\s+at\s+the\s+end", t, re.I)
        cnt = re.search(r"Counts\s+as\s+a\s+basic-attack\s+hit\s+for\s+any\s+on-hit\s+or\s+combo\s+effects", t, re.I)

        if n and e:
            p1 = n.group("p1")
            e1 = DMG_TYPE.get(n.group("e1").lower(), "성속성")
            t1 = ko_target_from_en(n.group("t1"))
            hits = e.group("hits")
            p2 = e.group("p2")
            e2 = DMG_TYPE.get(e.group("e2").lower(), e1)
            t2 = ko_target_from_en(e.group("t2"))

            out = f"일반 궁극기: {t1}에게 공격력의 {p1}%만큼 {e1} 피해를 준다. "
            out += f"강화 궁극기(일반 공격 {hits}회 후): {t2}에게 공격력의 {p2}%만큼 {e2} 피해를 준다."
            if rec:
                out += f" 효과 종료 시 에너지를 {rec.group('eng')} 회복하며,"
            if cnt:
                out += " 적중 시 기본 공격 1회로 간주되어 적중/연계(콤보) 효과 발동에 포함된다."
            return out.strip()

    # 5) When HP is higher then 50%: ... When HP is lower then 50%: ...
    m = re.match(
        r"^When\s+HP\s+is\s+higher\s+then\s+50%:\s*\+?(?P<c1>\d+(?:\.\d+)?)\s*%?\s*Crit\s+Rate\s+on\s+all\s+outgoing\s+damage\.\s*When\s+HP\s+is\s+lower\s+then\s+50%:\s*\+?(?P<d1>\d+(?:\.\d+)?)\s*%?\s*Defense\s+when\s+taking\s+damage\.$",
        t, re.I
    )
    if m:
        c1 = m.group("c1")
        d1 = m.group("d1")
        return (
            f"HP가 50%를 초과할 때: 자신이 주는 모든 피해의 치명타 확률이 {c1}% 증가한다. "
            f"HP가 50% 이하일 때: 피해를 받을 때 방어력이 {d1}% 증가한다."
        )

    # 6) Team skill: Self attack power increased by X%. At battle start: For every A attack power, increase self <elem> damage by B% (maximum N times). Maximum scaling: ...
    m = re.match(
        r"^Self\s+attack\s+power\s+increased\s+by\s+(?P<x>\d+(?:\.\d+)?)%\.\s*At\s+battle\s+start:\s*For\s+every\s+(?P<a>\d+)\s+attack\s+power,\s*increase\s+self\s+(?P<elem>holy|chaos|fire|water|wind|dark|light|physical)\s+damage\s+by\s+(?P<b>\d+(?:\.\d+)?)%\s*\(maximum\s+(?P<n>\d+)\s+times\)\.\s*Maximum\s+scaling:\s*(?P<max>\d+(?:\.\d+)?)%\s+(?P<elem2>holy|chaos|fire|water|wind|dark|light|physical)\s+damage\s+boost\s+at\s+(?P<th>\d[\d,]*)\+\s+attack\s+power\.$",
        t, re.I
    )
    if m:
        x = m.group("x")
        a = m.group("a")
        elem = DMG_TYPE[m.group("elem").lower()]
        b = m.group("b")
        n = m.group("n")
        maxv = m.group("max")
        th = m.group("th").replace(",", "")
        return (
            f"자신의 공격력이 {x}% 증가한다. "
            f"전투 시작 시: 공격력 {a}마다 자신의 {elem} 피해가 {b}% 증가한다(최대 {n}회). "
            f"최대 적용: 공격력 {th} 이상일 때 {elem} 피해가 최대 {maxv}% 증가한다."
        )

    # 7) Awakenings samples (afrodite-like)
    m = re.match(
        r"^When\s+you\s+using\s+auto\s+skill\s+\(Self\)\s+it\s+counts\s+as\s+(?P<n>\d+)\s+extra\s+basic-attack\s+hits\s+toward\s+the\s+Enhanced\s+Ultimate\s+counter\.$",
        t, re.I
    )
    if m:
        n = m.group("n")
        return f"자신이 자동 스킬을 사용하면, 강화 궁극기 카운트용 기본 공격 적중 수에 추가로 {n}회가 더해진다."

    m = re.match(
        r"^When\s+using\s+ultimate\s+or\s+auto\s+skill:\s*Damage\s+taken\s+reduced\s+by\s+(?P<x>\d+(?:\.\d+)?)%$",
        t, re.I
    )
    if m:
        x = m.group("x")
        return f"궁극기 또는 자동 스킬 사용 시 받는 피해가 {x}% 감소한다."

    m = re.match(
        r"^\[Skills\]\s+and\s+\[Normal\s+Attack\]\s+level\s+and\s+level\s+cap\s+\+(?P<x>\d+)$",
        t, re.I
    )
    if m:
        x = m.group("x")
        return f"[스킬]과 [일반 공격]의 레벨 및 레벨 상한이 {x} 증가한다."

    m = re.match(r"^Normal\s+attack\s+damage\s+increased\s+by\s+(?P<x>\d+(?:\.\d+)?)%$", t, re.I)
    if m:
        x = m.group("x")
        return f"일반 공격 피해가 {x}% 증가한다."

    m = re.match(r"^\[Ultimate\]\s+and\s+\[Passive\]\s+level\s+and\s+level\s+cap\s+\+(?P<x>\d+)$", t, re.I)
    if m:
        x = m.group("x")
        return f"[궁극기]와 [패시브]의 레벨 및 레벨 상한이 {x} 증가한다."

    m = re.match(r"^\[Enhanced\s+Ultimate\]\s+ignores\s+(?P<x>\d+(?:\.\d+)?)%\s+Holy\s+resistance$", t, re.I)
    if m:
        x = m.group("x")
        return f"[강화 궁극기]가 성속성 저항을 {x}% 무시한다."

    # 8) Memory card effects
    m = re.match(r"^Attack\s+power\s+increased\s+by\s+(?P<x>\d+(?:\.\d+)?)%$", t, re.I)
    if m:
        x = m.group("x")
        return f"공격력이 {x}% 증가한다."

    m = re.match(
        r"^If\s+the\s+equipped\s+units\s+Ultimate\s+costs\s+higher\s+then\s+(?P<e>\d+)\s+Energy\s+and\s+used\s+ultimate\s*:\s*Damage\s+increased\s+by\s+(?P<x>\d+(?:\.\d+)?)%\s+for\s+(?P<s>\d+)\s+seconds$",
        t, re.I
    )
    if m:
        e = m.group("e")
        x = m.group("x")
        s = m.group("s")
        return f"장착 유닛의 궁극기 소모 에너지가 {e}을 초과하고 궁극기를 사용하면, {s}초 동안 자신이 주는 피해가 {x}% 증가한다."

    return None


def translate_text_rules_only(text: str, glossary: Dict[str, str], fallback: str = "keep") -> str:
    """
    fallback:
      - keep: 패턴 미매칭은 원문 유지
      - mark: 미매칭은 '[UNTRANSLATED]' 접두어로 표시
    """
    if not isinstance(text, str) or not text.strip():
        return text

    # line split preserve
    lines = text.split("\n")
    out_lines: List[str] = []

    for line in lines:
        raw = line.strip()
        if not raw:
            out_lines.append(line)
            continue

        protected, ph = protect_tokens(raw)
        ruled = rule_translate_line(protected)

        if ruled is None:
            if fallback == "mark":
                out = "[UNTRANSLATED] " + raw
            else:
                out = raw
        else:
            out = restore_tokens(ruled, ph)
            out = apply_glossary(out, glossary)
            out = postprocess_ko(out)

        out_lines.append(out)

    return "\n".join(out_lines)


# =========================================================
# F) Apply only requested fields
# =========================================================
def translate_character_fields(obj: Dict[str, Any], glossary: Dict[str, str], fallback: str) -> int:
    changed = 0

    # 1) Skills.description
    skills = obj.get("skills")
    if isinstance(skills, dict):
        for _, s in skills.items():
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text_rules_only(src, glossary, fallback=fallback)
                if dst != src:
                    s["description"] = dst
                    changed += 1
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict) and isinstance(s.get("description"), str):
                src = s["description"]
                dst = translate_text_rules_only(src, glossary, fallback=fallback)
                if dst != src:
                    s["description"] = dst
                    changed += 1

    # 2) Team Skill.description
    team = obj.get("teamSkill")
    if isinstance(team, dict) and isinstance(team.get("description"), str):
        src = team["description"]
        dst = translate_text_rules_only(src, glossary, fallback=fallback)
        if dst != src:
            team["description"] = dst
            changed += 1

    # 3) Awakenings effect
    for aw_key in ("awakenings", "awakeningEffects"):
        aw = obj.get(aw_key)
        if isinstance(aw, list):
            for item in aw:
                if isinstance(item, dict) and isinstance(item.get("effect"), str):
                    src = item["effect"]
                    dst = translate_text_rules_only(src, glossary, fallback=fallback)
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
                    dst = translate_text_rules_only(e, glossary, fallback=fallback)
                    if dst != e:
                        effects[i] = dst
                        changed += 1

    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="public/data/zone-nova/characters")
    ap.add_argument("--out", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--glossary", default="public/data/zone-nova/glossary_ko.json")
    ap.add_argument("--fallback", choices=["keep", "mark"], default="keep")
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

        changed = translate_character_fields(data_obj, glossary, fallback=args.fallback)
        total_changed += changed

        out_path = out_dir / f"{js_file.stem}.json"
        out_path.write_text(json.dumps(data_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {js_file.name} (export={export_key}) -> {out_path.name} | updated={changed}")

    print(f"Done. Total updated fields: {total_changed}")


if __name__ == "__main__":
    main()
