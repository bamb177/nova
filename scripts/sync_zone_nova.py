import os, re, json, time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

BASE = "https://gachawiki.info"
INDEX_URL = "https://gachawiki.info/guides/zone-nova/characters/"

OUT_JSON = os.path.join("public", "data", "zone-nova", "characters.json")
OUT_IMG_DIR = os.path.join("public", "images", "games", "zone-nova", "characters")

UA = "Mozilla/5.0 (ZoneNovaSync/1.0)"

def ensure_dirs():
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    os.makedirs(OUT_IMG_DIR, exist_ok=True)

def slug_id(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s

def get_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text

def parse_kv_table(soup: BeautifulSoup) -> dict:
    """
    캐릭터 페이지에서 Rarity/Element/Role 등을 텍스트로 긁어오는 범용 파서.
    레이아웃이 바뀌어도 'Rarity', 'Element', 'Role' 키 텍스트를 기준으로 최대한 복구.
    """
    text = soup.get_text("\n", strip=True)
    out = {}

    def pick(label):
        # label 다음 줄(또는 근처) 값을 대충 잡는 방식 (유연성 우선)
        # 예: "Rarity\nSSR\nElement\nWind\nRole\nDPS"
        pattern = rf"{label}\s*\n\s*([A-Za-z]+)"
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    out["rarity"] = pick("Rarity")
    out["element"] = pick("Element")
    out["role"] = pick("Role")
    out["class"] = pick("Class")
    return out

def pick_og_image(soup: BeautifulSoup) -> str | None:
    m = soup.find("meta", attrs={"property": "og:image"})
    if m and m.get("content"):
        return m["content"].strip()
    return None

def download_file(url: str, out_path: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60, stream=True)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 64):
            if chunk:
                f.write(chunk)

def main():
    ensure_dirs()

    index_html = get_html(INDEX_URL)
    soup = BeautifulSoup(index_html, "html.parser")

    # /guides/zone-nova/characters/<slug>/ 링크 수집
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/guides/zone-nova/characters/" in href and href.rstrip("/").count("/") >= 5:
            full = urljoin(BASE, href)
            if full.endswith("/"):
                links.append(full)
            else:
                links.append(full + "/")

    links = sorted(set(links))
    if not links:
        raise RuntimeError("캐릭터 링크를 찾지 못했습니다. (INDEX 레이아웃 변경 가능)")

    chars = []
    for url in links:
        try:
            html = get_html(url)
            ps = BeautifulSoup(html, "html.parser")

            # 이름: 페이지의 H1을 우선 사용
            h1 = ps.find("h1")
            name = h1.get_text(strip=True) if h1 else url.rstrip("/").split("/")[-1].title()

            meta = parse_kv_table(ps)
            og_img = pick_og_image(ps)

            # 이미지 파일명 결정: og:image URL의 path에서 basename을 사용
            img_file = None
            if og_img:
                p = urlparse(og_img).path
                img_file = os.path.basename(p)
                # 확장자가 없으면 jpg로 처리
                if "." not in img_file:
                    img_file = f"{slug_id(name)}.jpg"

                out_img = os.path.join(OUT_IMG_DIR, img_file)
                if not os.path.isfile(out_img):
                    download_file(og_img, out_img)

            cid = slug_id(name)

            chars.append({
                "id": cid,
                "name": name,              # UI에서 영어 이름 유지
                "rarity": meta.get("rarity") or "-",
                "element": meta.get("element") or "-",
                "role": meta.get("role") or "-",
                "class": meta.get("class") or "-",
                "image": f"/images/games/zone-nova/characters/{img_file}" if img_file else None,
                "source": url
            })

            time.sleep(0.2)  # 과도한 요청 방지(가볍게)
        except Exception as e:
            # 실패해도 전체 중단 대신 누락 표시
            chars.append({
                "id": url.rstrip("/").split("/")[-1],
                "name": url.rstrip("/").split("/")[-1],
                "rarity": "-",
                "element": "-",
                "role": "-",
                "class": "-",
                "image": None,
                "source": url,
                "error": str(e)
            })

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"count": len(chars), "characters": chars}, f, ensure_ascii=False, indent=2)

    print(f"[OK] saved: {OUT_JSON}")
    print(f"[OK] images in: {OUT_IMG_DIR}")

if __name__ == "__main__":
    main()
