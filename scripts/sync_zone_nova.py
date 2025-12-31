# scripts/sync_zone_nova.py
from __future__ import annotations
import json, subprocess, argparse, re
from pathlib import Path
from datetime import datetime, timezone

# ===== 실제 게임 기준 이름 고정 =====
NAME_OVERRIDES = {
    "greed mammon": "Mammon",
    "kela": "Clara",
    "morgan": "Morgan Le Fay",
    "leviathan": "Behemoth",
    "snow girl": "Yuki-onna",
    "shanna": "Saya",
    "apep": "Apep",
}

def canon(s: str) -> str:
    return " ".join((s or "").strip().lower().split())

def apply_name(name: str) -> str:
    return NAME_OVERRIDES.get(canon(name), name)

# ===== 표준화 =====
def title(s: str) -> str:
    return s[:1].upper() + s[1:].lower() if s else ""

def norm_element(v): return title(v)
def norm_class(v): return title(v)

def norm_rarity(v):
    v = (v or "").upper()
    return v if v in {"SSR","SR","R"} else ""

def role_from_class(cls):
    if cls == "Guardian": return "Tank"
    if cls == "Healer": return "Healer"
    if cls == "Buffer": return "Buffer"
    if cls == "Debuffer": return "Debuffer"
    if cls in {"Warrior","Mage","Rogue"}: return "DPS"
    return ""

def norm_role(role, cls):
    if role:
        r = title(role)
        return "DPS" if r.upper()=="DPS" else r
    return role_from_class(cls)

def slug(s): return re.sub(r"[^a-z0-9]", "", s.lower())

# ===== image =====
def build_img_index(img_dir: Path):
    m = {}
    if not img_dir.exists(): return m
    for p in img_dir.iterdir():
        if p.is_file():
            m[p.stem.lower()] = p.name
    return m

def pick_image(name, idx):
    k = name.lower()
    if k in idx: return f"/images/games/zone-nova/characters/{idx[k]}"
    s = slug(name)
    if s in idx: return f"/images/games/zone-nova/characters/{idx[s]}"
    return None

# ===== main =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--upstream", required=True)
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    upstream = root / args.upstream
    if not upstream.exists():
        raise RuntimeError(f"업스트림 루트가 존재하지 않습니다: {upstream}")

    data_dir = root / "public/data/zone-nova"
    chars_json = data_dir / "characters.json"
    meta_json = data_dir / "characters_meta.json"

    # Node extract
    subprocess.run(
        ["node", "scripts/extract_zone_nova_characters.mjs",
         "--upstream", str(upstream), "--out", str(chars_json)],
        check=True
    )

    raw = json.loads(chars_json.read_text("utf-8"))
    img_idx = build_img_index(
        root / "public/images/games/zone-nova/characters"
    )

    out = []
    for c in raw:
        raw_name = c.get("name") or c.get("id","")
        name = apply_name(raw_name)
        cls = norm_class(c.get("class",""))
        out.append({
            "id": c.get("id") or slug(name),
            "name": name,
            "element": norm_element(c.get("element","")),
            "class": cls,
            "role": norm_role(c.get("role",""), cls),
            "rarity": norm_rarity(c.get("rarity","")),
            "image": pick_image(name, img_idx),
        })

    payload = {
        "last_refresh": datetime.now(timezone.utc).isoformat(),
        "count": len(out),
        "characters": sorted(out, key=lambda x: x["name"].lower())
    }

    if args.write:
        meta_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    print("OK:", payload["count"])

if __name__ == "__main__":
    main()
