import os
import json
import re
from datetime import datetime, timezone
from flask import Flask, jsonify, redirect, render_template

APP_TITLE = os.getenv("APP_TITLE", "Nova")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "public", "data", "zone-nova")
CHAR_KO_DIR = os.path.join(DATA_DIR, "characters_ko")

IMG_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "characters")
ELEM_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "element")
CLASS_ICON_DIR = os.path.join(BASE_DIR, "public", "images", "games", "zone-nova", "classes")

VALID_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# 업스트림 속성명 보정(표시용)
ELEMENT_RENAME = {"Fire": "Blaze", "Wind": "Storm", "Ice": "Frost"}

app = Flask(__name__, static_folder="public", static_url_path="")

CACHE = {
    "chars": [],
    "details": {},  # cid -> raw detail json
    "last_refresh": None,
    "error": None,
    "source": "public/data/zone-nova/characters_ko/*.json",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slug_id(s: str) -> str:
    s = (s or "").strip().lower().replace("’", "'")
    s = re.sub(r"[\s'\"`]+", "", s)
    s = re.sub(r"[^a-z0-9_-]", "", s)
    return s


def safe_load_json(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_file_map(folder: str) -> dict:
    """폴더 내 이미지 파일을 (여러 키 형태) -> 파일명 으로 매핑"""
    m = {}
    if not os.path.isdir(folder):
        return m

    pri = {".jpg": 4, ".jpeg": 4, ".png": 3, ".webp": 2}

    for fn in os.listdir(folder):
        ext = os.path.splitext(fn)[1].lower()
        if ext not in VALID_IMG_EXT:
            continue

        base = os.path.splitext(fn)[0]
        base_low = base.lower()

        keys = {base_low, slug_id(base)}
        stripped = re.sub(r"^[0-9]+[_\-\s]*", "", base_low).strip()
        if stripped:
            keys.add(stripped)
            keys.add(slug_id(stripped))

        compact = re.sub(r"[\s\-_]+", "", base_low)
        if compact:
            keys.add(compact)
            keys.add(slug_id(compact))

        for k in keys:
            if k not in m:
                m[k] = fn
            else:
                cur_ext = os.path.splitext(m[k])[1].lower()
                if pri.get(ext, 0) > pri.get(cur_ext, 0):
                    m[k] = fn
    return m


def normalize_char_name(name: str) -> str:
    name = (name or "").replace("’", "'").strip()
    return " ".join(name.split())


def normalize_element(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return "-"
    s2 = s[:1].upper() + s[1:].lower()
    return ELEMENT_RENAME.get(s2, s2)


def candidate_image_keys(cid: str, name: str, image_hint: str | None = None) -> list[str]:
    out = []
    cid = (cid or "").strip()
    nm = (name or "").strip()
    ih = (image_hint or "").strip()

    def add(x: str):
        x = (x or "").strip()
        if not x:
            return
        out.append(x.lower())
        out.append(slug_id(x))
        out.append(re.sub(r"[\s\-_]+", "", x.lower()))
        out.append(slug_id(re.sub(r"[\s\-_]+", "", x)))

    # characters_ko image 힌트 최우선
    add(ih)
    add(nm)
    add(nm.replace("'", ""))
    add(nm.replace("’", ""))
    add(nm.replace(" ", ""))
    add(cid)

    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def load_all(force: bool = False) -> None:
    if CACHE["chars"] and not force:
        return

    CACHE["error"] = None
    CACHE["chars"] = []
    CACHE["details"] = {}

    try:
        if not os.path.isdir(CHAR_KO_DIR):
            raise RuntimeError(f"characters_ko 디렉터리 없음: {CHAR_KO_DIR}")

        char_img_map = build_file_map(IMG_DIR)
        elem_icon_map = build_file_map(ELEM_ICON_DIR)
        class_icon_map = build_file_map(CLASS_ICON_DIR)

        chars = []
        details = {}

        files = [fn for fn in os.listdir(CHAR_KO_DIR) if fn.lower().endswith(".json")]
        files.sort()

        for fn in files:
            cid = slug_id(os.path.splitext(fn)[0])
            if not cid:
                continue

            path = os.path.join(CHAR_KO_DIR, fn)
            d = safe_load_json(path)
            if not isinstance(d, dict):
                continue

            details[cid] = d

            name = normalize_char_name(d.get("name") or cid)
            rarity = (d.get("rarity") or "-").strip().upper()
            element = normalize_element(str(d.get("element") or "-"))
            faction = (str(d.get("faction") or "-").strip() or "-")
            cls = (str(d.get("class") or "-").strip() or "-")
            role = (str(d.get("role") or "-").strip() or "-")

            image_url = None
            image_hint = d.get("image")
            image_hint = image_hint.strip() if isinstance(image_hint, str) else None

            for k in candidate_image_keys(cid, name, image_hint=image_hint):
                real = char_img_map.get(k)
                if real:
                    image_url = f"/images/games/zone-nova/characters/{real}"
                    break

            elem_icon = None
            if element and element != "-" and elem_icon_map:
                ek = element.lower()
                real = elem_icon_map.get(ek) or elem_icon_map.get(slug_id(ek))
                if real:
                    elem_icon = f"/images/games/zone-nova/element/{real}"

            class_icon = None
            if cls and cls != "-" and class_icon_map:
                ck = cls.lower()
                real = class_icon_map.get(ck) or class_icon_map.get(slug_id(ck))
                if real:
                    class_icon = f"/images/games/zone-nova/classes/{real}"

            chars.append({
                "id": cid,
                "name": name,
                "rarity": rarity,
                "element": element,
                "faction": faction,
                "class": cls,
                "role": role,
                "image": image_url,
                "element_icon": elem_icon,
                "class_icon": class_icon,
            })

        rarity_order = {"SSR": 0, "SR": 1, "R": 2, "-": 9}
        chars.sort(key=lambda x: (rarity_order.get(x.get("rarity", "-"), 9), (x.get("name") or "").lower()))

        CACHE["chars"] = chars
        CACHE["details"] = details
        CACHE["last_refresh"] = now_iso()

    except Exception as e:
        CACHE["error"] = str(e)
        CACHE["last_refresh"] = now_iso()


@app.get("/")
def home():
    return redirect("/ui/select")


@app.get("/refresh")
def refresh():
    load_all(force=True)
    return redirect("/ui/select")


@app.get("/meta")
def meta():
    load_all()
    return jsonify({
        "title": APP_TITLE,
        "characters_cached": len(CACHE["chars"]),
        "last_refresh": CACHE["last_refresh"],
        "error": CACHE["error"],
        "source": CACHE["source"],
        "characters_ko_dir": CHAR_KO_DIR,
    })


@app.get("/zones/zone-nova/characters")
def api_chars():
    load_all()
    return jsonify({
        "count": len(CACHE["chars"]),
        "last_refresh": CACHE["last_refresh"],
        "source": CACHE["source"],
        "error": CACHE["error"],
        "characters": CACHE["chars"],
    })


@app.get("/zones/zone-nova/characters/<cid>")
def api_char_detail(cid: str):
    load_all()
    cid2 = slug_id(cid)

    detail = CACHE["details"].get(cid2)
    if not isinstance(detail, dict):
        detail_path = os.path.join(CHAR_KO_DIR, f"{cid2}.json")
        detail = safe_load_json(detail_path)

    if not isinstance(detail, dict):
        return jsonify({"ok": False, "error": f"characters_ko json not found: {cid2}.json"}), 404

    # base는 리스트 캐시에서 가져오되 없으면 detail로 최소 생성
    base = next((c for c in CACHE["chars"] if c.get("id") == cid2), None)
    if not base:
        base = {
            "id": cid2,
            "name": detail.get("name") or cid2,
            "rarity": (detail.get("rarity") or "-").strip().upper(),
            "element": normalize_element(str(detail.get("element") or "-")),
            "faction": (str(detail.get("faction") or "-").strip() or "-"),
            "class": (str(detail.get("class") or "-").strip() or "-"),
            "role": (str(detail.get("role") or "-").strip() or "-"),
            "image": None,
            "element_icon": None,
            "class_icon": None,
        }

    return jsonify({
        "ok": True,
        "id": cid2,
        "character": base,
        "detail": detail,
        "detail_source": f"public/data/zone-nova/characters_ko/{cid2}.json",
    })


@app.get("/ui/select")
def ui_select():
    load_all()
    return render_template(
        "select.html",
        title=APP_TITLE,
        cache_count=len(CACHE["chars"]),
        last_refresh=CACHE["last_refresh"] or "N/A",
        error=CACHE["error"],
        chars_json=json.dumps(CACHE["chars"], ensure_ascii=False),
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
