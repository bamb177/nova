#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import hashlib
import argparse
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional


# =========================
# Paths
# =========================
BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
PUBLIC_DATA_DIR = BASE_DIR / "public" / "data" / "zone-nova"
DETAIL_OUT_DIR = PUBLIC_DATA_DIR / "characters"

OVERRIDES_NAMES = PUBLIC_DATA_DIR / "overrides_names.json"
OVERRIDES_FACTIONS = PUBLIC_DATA_DIR / "overrides_factions.json"

META_OUT = PUBLIC_DATA_DIR / "characters_meta.json"
UNMATCHED_OUT = PUBLIC_DATA_DIR / "_unmatched_gacha_wiki.json"
TRANSLATE_CACHE_OUT = PUBLIC_DATA_DIR / "_translate_cache_ko.json"


# =========================
# Defaults (user required)
# =========================
DEFAULT_NAME_OVERRIDES = {
    "Greed Mammon": "Mammon",
    "Kela": "Clara",
    "Morgan": "Morgan Le Fay",
    "Leviathan": "Behemoth",
    "Snow Girl": "Yuki-onna",
    "Shanna": "Saya",
    "Naiya": "Naya",
    "Afrodite": "Aphrodite",
    "apep": "Apep",
    "Belphegar": "Belphegor",
    "Chiya": "Cynia",
    "Freye": "Frigga",
    "gaia": "Gaia",
    "Jeanne D Arc": "Joan of Arc",
    "Penny": "Pennie",
    "Yuis": "Zeus",
}

DEFAULT_FACTION_OVERRIDES = {
    "A.S.A": "Asa",
    "Bicta Tower": "Bikta",
    "Chemic": "Kemich",
    "Monochrome Nation": "Monochrome Realm",
    "Oduis": "Otis",
    "Pingjing City": "Heikyo Castle",
    "Sapphire": "Safir",
}

# Element rename in your UI logic
ELEM_REMAP = {
    "Fire": "Blaze",
    "Wind": "Storm",
    "Ice": "Frost",
    # keep others as-is
}

# Translation mode: none | argos | openai
DEFAULT_TRANSLATE_MODE = os.getenv("TRANSLATE_MODE", "none").strip().lower()


# =========================
# Utilities
# =========================
def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def _load_json_if_exists(p: Path, default):
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _read_overrides() -> Tuple[Dict[str, str], Dict[str, str]]:
    names = dict(DEFAULT_NAME_OVERRIDES)
    factions = dict(DEFAULT_FACTION_OVERRIDES)

    names.update(_load_json_if_exists(OVERRIDES_NAMES, {}))
    factions.update(_load_json_if_exists(OVERRIDES_FACTIONS, {}))

    # normalize keys (keep original too; but add slug variants for robustness)
    def expand(m: Dict[str, str]) -> Dict[str, str]:
        out = dict(m)
        for k, v in list(m.items()):
            out[slug_id(k)] = v
        return out

    return expand(names), expand(factions)


def _normalize_element(x: Any) -> str:
    v = (x or "-")
    if not isinstance(v, str):
        v = str(v)
    v = v.strip() or "-"
    return ELEM_REMAP.get(v, v)


def _normalize_str(x: Any, default: str = "-") -> str:
    if x is None:
        return default
    if isinstance(x, str):
        s = x.strip()
        return s if s else default
    return str(x).strip() or default


# =========================
# JS loader (gacha-wiki *.js)
# =========================
def load_js_as_json(fp: Path) -> Any:
    """
    Supports:
      - ESM: export default {...}
      - CJS: module.exports = {...}
    Uses Node to load and JSON.stringify result.
    """
    js = r"""
import { pathToFileURL } from 'url';
import { createRequire } from 'module';
const p = process.argv[1];
let data = null;

try {
  const m = await import(pathToFileURL(p).href);
  data = (m && (m.default ?? m)) ?? null;
} catch (e) {
  try {
    const require = createRequire(import.meta.url);
    const m2 = require(p);
    data = (m2 && (m2.default ?? m2)) ?? null;
  } catch (e2) {
    console.error(String(e));
    console.error(String(e2));
    process.exit(2);
  }
}

process.stdout.write(JSON.stringify(data));
""".strip()

    proc = subprocess.run(
        ["node", "--input-type=module", "-e", js, str(fp)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node import failed: {fp}\n{proc.stderr}")

    try:
        return json.loads(proc.stdout)
    except Exception as e:
        raise RuntimeError(f"node output is not json: {fp}\n{e}\n{proc.stdout[:2000]}")


def find_upstream_char_dir(upstream_root: Path) -> Path:
    """
    Expected gacha-wiki structure:
      gacha-wiki/src/data/zone-nova/characters/*.js
    """
    cands = [
        upstream_root / "src" / "data" / "zone-nova" / "characters",
        upstream_root / "src" / "data" / "zone_nova" / "characters",
        upstream_root / "data" / "zone-nova" / "characters",
    ]
    for p in cands:
        if p.is_dir():
            return p
    raise RuntimeError(f"Upstream characters dir not found. tried: {', '.join(str(x) for x in cands)}")


# =========================
# Translation (zero-cost default: Argos)
# =========================
_argos_ready = False

def _argos_init():
    global _argos_ready
    if _argos_ready:
        return

    try:
        import argostranslate.package
        import argostranslate.translate
    except Exception as e:
        raise RuntimeError(
            "Argos Translate not installed. Add to requirements.txt: argostranslate==1.6.1 "
            f"(import error: {e})"
        )

    # Install en->ko model if not present
    import argostranslate.package as pkg
    import argostranslate.translate as tr

    # Update package index (downloads metadata)
    pkg.update_package_index()
    available = pkg.get_available_packages()

    target = None
    for p in available:
        if p.from_code == "en" and p.to_code == "ko":
            target = p
            break

    if target:
        installed_langs = tr.get_installed_languages()
        have = any(l.code == "en" for l in installed_langs) and any(l.code == "ko" for l in installed_langs)
        if not have:
            # download + install model
            model_path = target.download()
            pkg.install_from_path(model_path)

    _argos_ready = True


def _argos_translate(text: str) -> str:
    _argos_init()
    import argostranslate.translate as tr
    return tr.translate(text, "en", "ko")


def _openai_translate(text: str, api_key: str, model: str) -> str:
    """
    Optional paid mode. Only used when TRANSLATE_MODE=openai.
    Uses Requests-free urllib to avoid extra deps.
    """
    import urllib.request

    url = "https://api.openai.com/v1/chat/completions"
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
                    "Preferred KR terminology (be consistent):\n"
                    "- damage → 피해, additional damage → 추가 피해, dealt/receive damage → 가하는/받는 피해\n"
                    "- heal/restore → 회복, shield/barrier → 보호막\n"
                    "- buff → 버프, debuff → 디버프, dispel/remove → 해제\n"
                    "- stack → 중첩, turn → 턴, duration → 지속 시간, cooldown → 재사용 대기시간\n"
                    "- chance/probability → 확률, immune → 면역\n"
                    "\n"
                    "Style:\n"
                    "- Keep sentences concise. Do not add subjects or explanations not present in the source.\n"
                    "- Avoid overly literal translation; prefer natural KR phrasing while preserving meaning.\n"
                )

            },
            {"role": "user", "content": text},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    j = json.loads(raw)
    out = (j.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
    return (out or "").strip()


def _should_translate_string(s: str) -> bool:
    if not s:
        return False
    t = s.strip()
    if len(t) <= 1:
        return False

    # if already has Hangul, skip
    if any("\uac00" <= ch <= "\ud7a3" for ch in t):
        return False

    # Looks like id/slug/path: skip
    if len(t) < 40 and all(ch.isalnum() or ch in "-_./ " for ch in t):
        return False

    return True


def translate_detail_object_to_ko(detail: Any, character_name: str, cache_path: Path, mode: str) -> Any:
    """
    Translate ONLY selected fields into Korean and keep everything else as-is (English).
    Selected translation targets (requested):
      1) skills -> description
      2) teamSkill -> description, alternativeConditions
      3) awakenings -> (each level) effect
      4) memoryCard -> effects (all strings under this subtree)
    Keep root detail['name'] (character name) as-is.
    """
    mode = (mode or "none").strip().lower()
    if mode == "none":
        return detail

    cache = _load_json_if_exists(cache_path, default={})

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    def _should_translate_path(path: Tuple[str, ...]) -> bool:
        """
        path includes dict keys, and list markers as '[]'.
        We translate only if path matches the user-approved scopes.
        """
        if not path:
            return False

        last = path[-1]

        # 1) skills/*/description
        if "skills" in path and last == "description":
            return True

        # 2) teamSkill/*/(description | alternativeConditions)
        if "teamSkill" in path and last in ("description", "alternativeConditions"):
            return True

        # 3) awakenings/*/effect  (level-based arrays/maps are handled by 'effect' key)
        if "awakenings" in path and last == "effect":
            return True

        # 4) memoryCard/effects/**  (translate all strings under this subtree)
        # If path contains memoryCard and effects after it, translate strings in that subtree.
        if "memoryCard" in path:
            try:
                i = path.index("memoryCard")
                if "effects" in path[i + 1:]:
                    return True
            except ValueError:
                pass

        return False

    def tr(s: str, path: Tuple[str, ...]) -> str:
        # not in scope => keep English
        if not _should_translate_path(path):
            return s

        # string-level heuristic filter
        if not _should_translate_string(s):
            return s

        key = _sha1(s)
        if key in cache:
            return cache[key]

        # small sleep to avoid runaway in CI
        time.sleep(0.05)

        if mode == "openai":
            if not api_key:
                return s
            # Higher-quality prompt: preserve numbers/tokens, keep skill/term casing as-is
            ko = _openai_translate(
                text=s,
                api_key=api_key,
                model=model,
            )
        elif mode == "argos":
            ko = _argos_translate(s)
        else:
            return s

        cache[key] = ko if ko else s
        return cache[key]

    def walk(obj, path: Tuple[str, ...] = ()):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                # Keep character name at root
                if isinstance(v, str) and k == "name" and len(path) == 0:
                    out[k] = v
                    continue

                # Keep exact character name string anywhere
                if isinstance(v, str) and character_name and v.strip() == character_name.strip():
                    out[k] = v
                    continue

                out[k] = walk(v, path + (k,))
            return out

        if isinstance(obj, list):
            return [walk(x, path + ("[]",)) for x in obj]

        if isinstance(obj, str):
            return tr(obj, path)

        return obj

    translated = walk(detail)

    # persist cache for future runs (cost control)
    _save_json(cache_path, cache)
    return translated


# =========================
# Extraction helpers
# =========================
def _extract_meta_fields(raw: Any) -> Dict[str, Any]:
    """
    Heuristic extraction for meta fields from gacha-wiki detail object.
    """
    if not isinstance(raw, dict):
        return {}

    # common candidates
    name = raw.get("name") or raw.get("characterName") or raw.get("title") or ""
    rarity = raw.get("rarity") or raw.get("grade") or raw.get("rank") or "-"
    element = raw.get("element") or raw.get("attr") or raw.get("attribute") or "-"
    faction = raw.get("faction") or raw.get("group") or raw.get("camp") or raw.get("tribe") or "-"
    cls = raw.get("class") or raw.get("job") or raw.get("type") or raw.get("roleClass") or raw.get("Class") or "-"
    role = raw.get("role") or raw.get("Role") or "-"  # optional

    # normalize strings
    return {
        "name": _normalize_str(name, default=""),
        "rarity": _normalize_str(rarity, default="-").upper(),
        "element": _normalize_element(element),
        "faction": _normalize_str(faction, default="-"),
        "class": _normalize_str(cls, default="-").lower(),
        "role": _normalize_str(role, default="-").lower(),
    }


def build_meta_and_details(
    upstream_char_dir: Path,
    name_overrides: Dict[str, str],
    faction_overrides: Dict[str, str],
    translate_mode: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns:
      meta_list: list of meta dicts (for characters_meta.json)
      unmatched: list of problems
    """
    DETAIL_OUT_DIR.mkdir(parents=True, exist_ok=True)

    meta_list: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []

    js_files = sorted([p for p in upstream_char_dir.glob("*.js") if p.is_file()])
    # ignore index.js or similar barrels
    js_files = [p for p in js_files if p.stem.lower() not in ("index", "_index")]

    for fp in js_files:
        try:
            raw = load_js_as_json(fp)
        except Exception as e:
            unmatched.append({"file": fp.name, "reason": "load_failed", "error": str(e)})
            continue

        meta = _extract_meta_fields(raw)

        # id: prefer explicit id, else filename stem
        rid = None
        if isinstance(raw, dict):
            rid = raw.get("id") or raw.get("_id") or raw.get("key")
        rid = _normalize_str(rid, default=fp.stem)
        cid = slug_id(rid) or slug_id(fp.stem)

        # Name override (by original name or slug(original))
        raw_name = meta.get("name") or fp.stem
        canonical_name = name_overrides.get(raw_name) or name_overrides.get(slug_id(raw_name)) or raw_name

        # apply faction override
        raw_faction = meta.get("faction") or "-"
        canonical_faction = faction_overrides.get(raw_faction) or faction_overrides.get(slug_id(raw_faction)) or raw_faction

        # element remap already done in normalize_element
        canonical_element = meta.get("element") or "-"

        # Ensure raw detail is dict so we can enforce root name
        if isinstance(raw, dict):
            raw["id"] = raw.get("id") or rid
            raw["name"] = canonical_name
            # Align element/faction in detail too (optional but helps consistency)
            if "element" in raw or canonical_element != "-":
                raw["element"] = canonical_element
            if "faction" in raw or canonical_faction != "-":
                raw["faction"] = canonical_faction
        else:
            # wrap unknown types
            raw = {"id": rid, "name": canonical_name, "element": canonical_element, "faction": canonical_faction, "data": raw}

        # Translate detail (keep character name)
        try:
            raw_translated = translate_detail_object_to_ko(
                raw,
                character_name=canonical_name,
                cache_path=TRANSLATE_CACHE_OUT,
                mode=translate_mode,
            )
        except Exception as e:
            # If translation fails, still write English detail to avoid losing data
            unmatched.append({"id": cid, "name": canonical_name, "reason": "translate_failed", "error": str(e)})
            raw_translated = raw

        # write detail json
        out_path = DETAIL_OUT_DIR / f"{cid}.json"
        _save_json(out_path, raw_translated)

        # build meta item
        item = {
            "id": cid,
            "name": canonical_name,             # keep EN name
            "rarity": meta.get("rarity", "-"),
            "element": canonical_element,
            "faction": canonical_faction,
            "class": meta.get("class", "-"),
            "role": meta.get("role", "-"),
        }

        # sanity
        if not item["name"]:
            unmatched.append({"id": cid, "file": fp.name, "reason": "missing_name"})
            item["name"] = cid
        if item["rarity"] not in ("SSR", "SR", "R", "-"):
            # do not fail; just note
            unmatched.append({"id": cid, "name": item["name"], "reason": "unknown_rarity", "rarity": item["rarity"]})

        meta_list.append(item)

    # Deduplicate by id (first wins)
    seen = set()
    uniq = []
    for c in meta_list:
        if c["id"] in seen:
            unmatched.append({"id": c["id"], "name": c.get("name"), "reason": "duplicate_id"})
            continue
        seen.add(c["id"])
        uniq.append(c)

    # Sort by rarity desc, then name asc
    rarity_order = {"SSR": 3, "SR": 2, "R": 1, "-": 0}
    uniq.sort(key=lambda x: (-rarity_order.get(x.get("rarity", "-"), 0), (x.get("name") or x.get("id") or "")))

    return uniq, unmatched


# =========================
# CLI
# =========================
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True, help="Path to gacha-wiki repo root (checked out by Actions)")
    ap.add_argument("--write", action="store_true", help="Write outputs to public/data/zone-nova")
    ap.add_argument("--sync-details", action="store_true", help="Generate public/data/zone-nova/characters/*.json")
    ap.add_argument("--translate-mode", default=DEFAULT_TRANSLATE_MODE, help="none|argos|openai (default: env TRANSLATE_MODE or none)")
    return ap.parse_args()


def main():
    args = parse_args()

    upstream_root = Path(args.upstream).resolve()
    upstream_char_dir = find_upstream_char_dir(upstream_root)

    name_overrides, faction_overrides = _read_overrides()

    if not args.sync_details:
        print("Nothing to do: --sync-details not set.")
        return

    translate_mode = (args.translate_mode or "none").strip().lower()
    if translate_mode not in ("none", "argos", "openai"):
        raise RuntimeError(f"Invalid translate mode: {translate_mode}")

    meta_list, unmatched = build_meta_and_details(
        upstream_char_dir=upstream_char_dir,
        name_overrides=name_overrides,
        faction_overrides=faction_overrides,
        translate_mode=translate_mode,
    )

    if args.write:
        _save_json(META_OUT, {"characters": meta_list})
        _save_json(UNMATCHED_OUT, {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "upstream": str(upstream_char_dir),
            "translate_mode": translate_mode,
            "detail_count": len(list(DETAIL_OUT_DIR.glob("*.json"))),
            "unmatched_count": len(unmatched),
            "items": unmatched,
        })

    print(f"[OK] meta_count={len(meta_list)} detail_dir={DETAIL_OUT_DIR} translate_mode={translate_mode}")
    if unmatched:
        print(f"[WARN] unmatched_count={len(unmatched)} -> {UNMATCHED_OUT.name}")


if __name__ == "__main__":
    main()
