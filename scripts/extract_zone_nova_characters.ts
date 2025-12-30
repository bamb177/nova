import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";

function slugId(s: string): string {
  return (s || "")
    .trim()
    .toLowerCase()
    .replace(/[’']/g, "")
    .replace(/\s+/g, "")
    .replace(/[^a-z0-9_-]/g, "");
}

function canonRole(role: any): string {
  const r = String(role || "").trim().toLowerCase();
  if (!r) return "-";
  if (r === "dps" || r.includes("damage") || r.includes("attacker")) return "dps";
  if (r === "tank" || r.includes("guard") || r.includes("defen")) return "tank";
  if (r === "healer" || r.includes("heal")) return "healer";
  if (r === "buffer" || r.includes("buff") || r.includes("support")) return "buffer";
  if (r === "debuffer" || r.includes("debuff")) return "debuffer";
  // 혹시 대문자/원문 유지된 경우
  if (["DPS", "TANK", "HEALER", "BUFFER", "DEBUFFER"].includes(String(role))) {
    return String(role).toLowerCase();
  }
  return r;
}

function pickAny(obj: any, keys: string[]): any {
  for (const k of keys) {
    if (obj && Object.prototype.hasOwnProperty.call(obj, k) && obj[k] != null) return obj[k];
  }
  return undefined;
}

function normalizeToList(raw: any): any[] {
  if (Array.isArray(raw)) return raw;
  if (raw && typeof raw === "object") {
    // {characters:[...]} 형태
    if (Array.isArray(raw.characters)) return raw.characters;
    // 맵 형태: { id: {...}, id2: {...} }
    const values = Object.values(raw);
    const dictLike = values.filter(v => v && typeof v === "object" && !Array.isArray(v));
    if (dictLike.length >= 3) {
      const out: any[] = [];
      for (const [k, v] of Object.entries(raw)) {
        if (v && typeof v === "object" && !Array.isArray(v)) {
          out.push({ _id: k, ...(v as any) });
        }
      }
      return out;
    }
  }
  return [];
}

function pickExport(mod: any): any {
  // 흔히 쓰는 export 이름들 우선 탐색
  const candidates = [
    "characters",
    "CHARACTERS",
    "zoneNovaCharacters",
    "ZONE_NOVA_CHARACTERS",
    "data",
    "DATA",
    "default",
  ];
  for (const k of candidates) {
    if (k in mod) return (mod as any)[k];
  }
  // 마지막 fallback: export가 하나면 그 값을 사용
  const keys = Object.keys(mod || {});
  if (keys.length === 1) return (mod as any)[keys[0]];
  return undefined;
}

function resolveEntry(it: any): { id: string; name: string; rarity: string; element: string; role: string } | null {
  const name = String(
    pickAny(it, ["name", "Name", "title", "Title", "displayName", "display_name"]) ?? ""
  ).trim();

  const idRaw = pickAny(it, ["id", "ID", "_id", "key", "slug", "code"]);
  const id = slugId(String(idRaw ?? name));

  if (!id || !name) return null;

  let rarity = pickAny(it, ["rarity", "Rarity", "grade", "Grade", "rank", "Rank"]);
  let element = pickAny(it, ["element", "Element", "attr", "Attr", "attribute", "Attribute"]);
  let role = pickAny(it, ["role", "Role", "class", "Class", "type", "Type"]);

  rarity = String(rarity ?? "-").trim().toUpperCase();
  element = String(element ?? "-").trim();
  role = canonRole(role);

  // Jeanne D Arc 통일(네가 이전에 겪은 오류 방지)
  const sid = slugId(name);
  if (sid === "joanofarc" || sid === "jeannedarc" || sid.includes("jeanne")) {
    return { id: "jeannedarc", name: "Jeanne D Arc", rarity, element, role };
  }

  return { id, name, rarity, element, role };
}

function findModuleFile(upstreamRoot: string): string {
  // 사용자가 준 경로: src/data/zone-nova/characters (확장자/디렉토리 가능)
  const base = path.join(upstreamRoot, "src", "data", "zone-nova", "characters");

  const candidates = [
    base + ".ts",
    base + ".js",
    base + ".mjs",
    base + ".cjs",
    // 디렉토리일 가능성 대응
    path.join(base, "index.ts"),
    path.join(base, "index.js"),
    path.join(base, "index.mjs"),
    path.join(base, "index.cjs"),
    path.join(base, "characters.ts"),
    path.join(base, "characters.js"),
  ];

  for (const p of candidates) {
    if (fs.existsSync(p) && fs.statSync(p).isFile()) return p;
  }

  // 디렉토리면 내부 파일을 한번 더 훑어봄
  if (fs.existsSync(base) && fs.statSync(base).isDirectory()) {
    const files = fs.readdirSync(base).map(f => path.join(base, f));
    const hit = files.find(f =>
      /\.(ts|js|mjs|cjs)$/.test(f) && /char/i.test(path.basename(f))
    );
    if (hit) return hit;
  }

  throw new Error(
    `업스트림에서 characters 모듈 파일을 찾지 못했습니다. 확인 필요 경로: ${base}{.ts/.js} 또는 ${base}/index.ts`
  );
}

function writeJson(outPath: string, obj: any) {
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(obj, null, 2), "utf-8");
}

async function main() {
  const args = process.argv.slice(2);
  const getArg = (k: string) => {
    const i = args.indexOf(k);
    return i >= 0 ? args[i + 1] : undefined;
  };

  const upstream = getArg("--upstream");
  const out = getArg("--out") ?? "public/data/zone-nova/characters_meta.json";

  if (!upstream) {
    throw new Error("사용법: --upstream <path-to-upstream-repo> [--out <output-json>]");
  }

  const upstreamRoot = path.resolve(upstream);
  const modFile = findModuleFile(upstreamRoot);

  const url = pathToFileURL(modFile).href;
  const mod = await import(url);

  const raw = pickExport(mod);
  if (raw == null) {
    throw new Error(`모듈 export에서 데이터를 찾지 못했습니다: ${modFile}`);
  }

  const list = normalizeToList(raw);
  if (!list.length) {
    throw new Error(
      `캐릭터 목록을 list로 변환하지 못했습니다. export 형태가 예상과 다릅니다: ${modFile}`
    );
  }

  const meta: Record<string, any> = {};
  for (const it of list) {
    const e = resolveEntry(it);
    if (!e) continue;
    meta[e.id] = { name: e.name, rarity: e.rarity, element: e.element, role: e.role };
  }

  if (Object.keys(meta).length < 20) {
    throw new Error(
      `캐릭터 변환 결과가 너무 적습니다(${Object.keys(meta).length}). 데이터 구조 확인 필요: ${modFile}`
    );
  }

  const payload = {
    _meta: {
      game: "zone-nova",
      source: `upstream:${path.relative(upstreamRoot, modFile).replace(/\\/g, "/")}`,
      generated_at: new Date().toISOString(),
      count: Object.keys(meta).length,
      roles: ["Buffer", "Debuffer", "Tank", "DPS", "Healer"],
    },
    characters: meta,
  };

  writeJson(path.resolve(process.cwd(), out), payload);
  console.log(`[OK] wrote ${out} (count=${payload._meta.count}) from ${payload._meta.source}`);
}

main().catch((e) => {
  console.error("[ERROR]", e?.message || e);
  process.exit(1);
});
