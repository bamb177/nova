# -*- coding: utf-8 -*-
"""
Zone Nova KR tooltip post-processor.

목표:
- 번역 결과에 남은 영문 잔재(특히 반복 패턴)를 정규식/용어사전으로 후처리
- "깨진 번역(부분 영문/오타/기호 위치)" 재발을 최소화
- 최종 산출물에 영문이 남으면 lint 단계에서 빠르게 탐지 가능

사용:
    from ko_tooltip_fixes import fix_tooltip_text, has_english_residue
    text = fix_tooltip_text(text, glossary_dict)
"""

from __future__ import annotations

import re
from typing import Dict

ENG_RE = re.compile(r"[A-Za-z]")

# 1) 툴팁에서 자주 쓰는 영문 스탯/키워드 -> 한글 통일
STAT_MAP = {
    "attack power": "공격력",
    "attack": "공격력",
    "atk": "공격력",
    "defense": "방어력",
    "def": "방어력",
    "max hp": "최대 HP",
    "hp": "HP",
    "critical rate": "치명타 확률",
    "critical hit rate": "치명타 확률",
    "critical chance": "치명타 확률",
    "critical damage": "치명타 피해",
    "attack speed": "공격 속도",
    "movement speed": "이동 속도",
    "healing amount": "회복량",
    "damage dealt": "가하는 피해",
    "damage taken": "받는 피해",
    "skill damage": "스킬 피해량",
    "normal attack damage": "기본 공격 피해량",
    "cooldown time": "재사용 대기시간",
    "cooldown": "재사용 대기시간",
}

def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _map_stat(s: str) -> str:
    return STAT_MAP.get(_norm_key(s), (s or "").strip())

def apply_glossary(text: str, glossary: Dict[str, str]) -> str:
    if not glossary:
        return text
    t = text
    # 긴 구문 우선
    for k in sorted(glossary.keys(), key=len, reverse=True):
        v = glossary[k]
        if k and v:
            t = t.replace(k, v)
    return t

# 2) 정규식 보정 룰 (반드시 "구체적 패턴 -> 일반 패턴" 순서)
_RULES = [
    # (A) 한글 뒤에 붙는 잘못된 복수형 s 제거: 대상s, 공격s 등
    (re.compile(r"([가-힣])s\b"), r"\1"),

    # (B) 숫자 + 초 띄어쓰기 정리: "10 초" -> "10초"
    (re.compile(r"(\d+)\s*초"), r"\1초"),

    # (C) 리스트 표기: "] and [" -> "] 및 ["
    (re.compile(r"\]\s*and\s*\[", re.I), "] 및 ["),
    (re.compile(r",\s*and\s*\[", re.I), ", ["),

    # (D) 레벨/상한 패턴
    (re.compile(r"\blevels?\s*&\s*max\s*levels?\s*\+(\d+)", re.I), r"레벨 및 레벨 상한 +\1"),
    (re.compile(r"\blevels?\s*and\s*level\s*cap\s*\+(\d+)", re.I), r"레벨 및 레벨 상한 +\1"),
    (re.compile(r"\blevel\s*and\s*level\s*cap\s*\+(\d+)", re.I), r"레벨 및 레벨 상한 +\1"),
    (re.compile(r"\blevel\s*cap\s*\+(\d+)", re.I), r"레벨 상한 +\1"),

    # (E) 중첩
    (re.compile(r"\bstacking\s*up\s*to\s*(\d+)\s*times\b", re.I), r"최대 \1회 중첩"),
    (re.compile(r"\bstacks?\s*up\s*to\s*(\d+)\s*times\b", re.I), r"최대 \1회 중첩"),

    # (F) 지속시간
    (re.compile(r"\bfor\s*(\d+)\s*(?:seconds|second|sec|s)\b", re.I), r"\1초 동안"),
    (re.compile(r"\blasts\s*(\d+)\s*(?:seconds|second|sec|s)\b", re.I), r"\1초 지속된다"),

    # (G) "equal to 120%of Attack" 류 (공백/전치사 누락 케이스 포함)
    (re.compile(r"equal\s*to\s*(\d+)%\s*of\s*(?:attack|attack power|atk)", re.I), r"공격력의 \1%에 해당하는"),
    (re.compile(r"(\d+)%\s*of\s*(?:attack|attack power|atk)", re.I), r"공격력의 \1%"),
    (re.compile(r"(\d+)%of\s*(?:attack|attack power|atk)", re.I), r"공격력의 \1%"),

    # (H) "dealing 240%atk" 류
    (re.compile(r"dealing\s*(\d+)%\s*(?:of\s*)?(?:atk|attack|attack power)\b", re.I), r"공격력의 \1%만큼 피해를 주며"),

    # (I) "to designated enemy" -> "~에게" (glossary로 지정한 적으로 바뀐 뒤 처리)
    (re.compile(r"\bto\s+지정한 적(?: 1명)?\b", re.I), "지정한 적에게"),
    (re.compile(r"\bto\s+모든 적\b", re.I), "모든 적에게"),

    # (J) 속성 전치사: "as 신성 속성 피해" -> "신성 속성 피해로"
    (re.compile(r"\bas\s+(혼돈 속성 피해|신성 속성 피해|바람 속성 피해|화염 속성 피해)\b", re.I), r"\1로"),

    # (K) 퍼센트 위치가 뒤집힌 케이스: "58 증가한다%" -> "58% 증가한다"
    (re.compile(r"(\d+)\s*증가한다%"), r"\1% 증가한다"),
    (re.compile(r"(\d+)\s*감소한다%"), r"\1% 감소한다"),

    # (L) HP 조건 패턴 (then/than 오타 포함)
    (re.compile(r"\bWhen\s+HP\s+is\s+higher\s+th[ae]n\s+(\d+)%\s*:\s*", re.I), r"HP가 \1% 초과일 때: "),
    (re.compile(r"\bWhen\s+HP\s+is\s+lower\s+th[ae]n\s+(\d+)%\s*:\s*", re.I), r"HP가 \1% 미만일 때: "),

    # (M) 흔한 잔재 구문
    (re.compile(r"\bwhen\s+taking\s+damage\b", re.I), "피해를 받을 때"),
    (re.compile(r"\bon\s+all\s+outgoing\s+damage\b", re.I), "가하는 피해에 대해"),
    (re.compile(r"\bAt\s+battle\s+start\s*:\s*", re.I), "전투 시작 시: "),
]

def _fix_stat_sentences(text: str) -> str:
    """
    "<stat> increases by 20%" / "<stat> decreased by 10%" 류를 문장형으로 통일.
    이미 일부 한글이 섞여 있어도 안정적으로 처리.
    """
    def inc(m: re.Match) -> str:
        stat = _map_stat(m.group(1))
        val = m.group(3)
        return f"{stat}이 {val} 증가한다"

    def dec(m: re.Match) -> str:
        stat = _map_stat(m.group(1))
        val = m.group(3)
        return f"{stat}이 {val} 감소한다"

    t = text
    t = re.sub(r"\b([A-Za-z가-힣 ]{1,30})\s+(increases|increased)\s+by\s+(\d+%?)\b", inc, t, flags=re.I)
    t = re.sub(r"\b([A-Za-z가-힣 ]{1,30})\s+(decreases|decreased)\s+by\s+(\d+%?)\b", dec, t, flags=re.I)
    return t

def fix_tooltip_text(text: str, glossary: Dict[str, str] | None = None) -> str:
    """
    후처리 메인 함수. (glossary -> regex -> stat 정리 -> glossary) 2-pass로 안정화.
    """
    if not text:
        return text

    t = text

    # 1) 긴 구문 glossary 먼저
    if glossary:
        t = apply_glossary(t, glossary)

    # 2) regex 룰
    for pat, rep in _RULES:
        t = pat.sub(rep, t)

    # 3) 문장형 스탯 패턴 정리
    t = _fix_stat_sentences(t)

    # 4) 다시 glossary(남은 잔재 정리)
    if glossary:
        t = apply_glossary(t, glossary)

    # 5) 공백 정리
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t

def has_english_residue(text: str) -> bool:
    return bool(text and ENG_RE.search(text))
