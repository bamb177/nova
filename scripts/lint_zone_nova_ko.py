# -*- coding: utf-8 -*-
"""
Lint: characters_ko 산출물에 영문 잔재가 남았는지 검사.

- 요청 항목(스킬/팀스킬/각성효과/메모리카드 효과)만 검사
- 영문이 남으면 report TSV를 생성하고 exit 1 (CI에서 즉시 탐지)
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, List, Tuple

ENG_RE = re.compile(r"[A-Za-z]")

def extract_targets(obj: Any) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []

    def walk(node: Any, path: List[Any]):
        if isinstance(node, dict):
            for k, v in node.items():
                # skills/teamSkill description
                if k == "description" and isinstance(v, str) and any(p in path for p in ["skills", "teamSkill"]):
                    out.append((".".join(map(str, path + [k])), v))

                # awakenings/awakeningEffects effect
                if k == "effect" and isinstance(v, str) and any(p in path for p in ["awakenings", "awakeningEffects"]):
                    out.append((".".join(map(str, path + [k])), v))

                # memoryCard.effects list[str]
                if k == "effects" and isinstance(v, list) and all(isinstance(x, str) for x in v) and ("memoryCard" in path):
                    for i, s in enumerate(v):
                        out.append((".".join(map(str, path + [k, i])), s))

                walk(v, path + [k])

        elif isinstance(node, list):
            for i, it in enumerate(node):
                walk(it, path + [i])

    walk(obj, [])
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="public/data/zone-nova/characters_ko")
    ap.add_argument("--report", default="public/data/zone-nova/ko_lint_report.tsv")
    ap.add_argument("--max_rows", type=int, default=200)
    args = ap.parse_args()

    root = Path(args.dir)
    files = sorted(root.glob("*.json"))

    bad_rows = []
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            bad_rows.append((f.name, "PARSE_ERROR", str(e)))
            continue

        for p, s in extract_targets(obj):
            if ENG_RE.search(s or ""):
                bad_rows.append((f.name, p, s))

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    header = "file\tpath\ttext\n"
    sample = bad_rows[: args.max_rows]
    report_path.write_text(header + "\n".join(f"{a}\t{b}\t{c}" for a, b, c in sample), encoding="utf-8")

    total = len(bad_rows)
    print(f"[LINT] files={len(files)} bad={total} report={report_path}")
    if total > 0:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
