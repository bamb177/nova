// scripts/extract_zone_nova_characters.mjs
import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";

function argValue(flag, fallback = null) {
  const i = process.argv.indexOf(flag);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return fallback;
}

function exists(p) {
  try { return fs.existsSync(p); } catch { return false; }
}

function walkJsFiles(dir) {
  const out = [];
  const stack = [dir];
  while (stack.length) {
    const cur = stack.pop();
    const entries = fs.readdirSync(cur, { withFileTypes: true });
    for (const e of entries) {
      const p = path.join(cur, e.name);
      if (e.isDirectory()) stack.push(p);
      else if (e.isFile() && e.name.toLowerCase().endsWith(".js")) out.push(p);
    }
  }
  return out;
}

function pickExportObject(mod) {
  if (!mod) return null;

  // 1) default 우선
  if (mod.default && typeof mod.default === "object") return mod.default;

  // 2) 흔한 named export 우선
  const preferred = ["character", "data", "info", "meta", "Character"];
  for (const k of preferred) {
    if (mod[k] && typeof mod[k] === "object") return mod[k];
  }

  // 3) 그 외: export 중 첫 object
  for (const [k, v] of Object.entries(mod)) {
    if (k === "__esModule") continue;
    if (v && typeof v === "object") return v;
  }
  return null;
}

function slugId(s) {
  const v = (s || "").toString().trim();
  if (!v) return "";
  // jeanne d arc -> jeannedarc / morgan le fay -> morganlefay
  return v.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function toTitleCase(s) {
  const v = (s || "").toString().trim();
  if (!v) return "";
  return v.charAt(0).toUpperCase() + v.slice(1).toLowerCase();
}

function normalizeElement(v) {
  // 데이터가 wind/Chaos 혼재해도 Wind/Chaos로 통일
  return toTitleCase(v);
}

function normalizeClass(v) {
  // mage/Warrior 등 혼재해도 Mage/Warrior로 통일
  return toTitleCase(v);
}

function normalizeRarity(v) {
  const t = (v || "").toString().trim().toUpperCase();
  if (t === "SSR" || t === "SR" || t === "R") return t;
  return t || "";
}

function deriveRoleFromClass(cls) {
  const c = normalizeClass(cls);
  if (!c) return "";
  if (c === "Guardian") return "Tank";
  if (c === "Healer") return "Healer";
  if (c === "Buffer") return "Buffer";
  if (c === "Debuffer") return "Debuffer";
  if (c === "Warrior" || c === "Mage" || c === "Rogue") return "DPS";
  return "";
}

function normalizeRole(role, cls) {
  const r = (role || "").toString().trim();
  if (r) {
    const t = toTitleCase(r);
    if (t.toUpperCase() === "DPS") return "DPS";
    // Buffer/Debuffer/Healer/Tank
    return t;
  }
  return deriveRoleFromClass(cls);
}

async function loadOne(filePath) {
  // 동적 import 시도 (가장 강력/정확)
  try {
    const mod = await import(pathToFileURL(filePath).href);
    const obj = pickExportObject(mod);
    if (obj) return obj;
  } catch (e) {
    // import 실패 시 아래 fallback regex로 진행
  }

  // fallback: 텍스트에서 최소 필드만 regex로 추출 (Apep/Gaia 케이스 포함 안정화)
  const txt = fs.readFileSync(filePath, "utf-8");
  const get = (re) => {
    const m = txt.match(re);
    return m ? (m[1] || "").trim() : "";
  };

  const name = get(/name\s*:\s*["'`]([^"'`]+)["'`]/i) || "";
  const element = get(/element\s*:\s*["'`]([^"'`]+)["'`]/i) || "";
  const cls = get(/\bclass\s*:\s*["'`]([^"'`]+)["'`]/i) || "";
  const rarity = get(/rarity\s*:\s*["'`]([^"'`]+)["'`]/i) || "";
  const role = get(/role\s*:\s*["'`]([^"'`]+)["'`]/i) || "";

  return { name, element, class: cls, rarity, role };
}

function findCharactersDir(upstreamRoot) {
  // 1순위: 사용자가 알려준 경로
  const p1 = path.join(upstreamRoot, "src", "data", "zone-nova", "characters");
  if (exists(p1)) return p1;

  // 2순위: 혹시 구조가 약간 다르면 src 아래에서 탐색
  const src = path.join(upstreamRoot, "src");
  if (!exists(src)) return null;

  // 간단 탐색: zone-nova/characters 포함 경로
  const stack = [src];
  while (stack.length) {
    const cur = stack.pop();
    const entries = fs.readdirSync(cur, { withFileTypes: true });
    for (const e of entries) {
      const p = path.join(cur, e.name);
      if (e.isDirectory()) {
        if (p.endsWith(path.join("zone-nova", "characters"))) return p;
        stack.push(p);
      }
    }
  }
  return null;
}

async function main() {
  const upstreamRoot = argValue("--upstream");
  const outPath = argValue("--out");

  if (!upstreamRoot || !outPath) {
    console.error("Usage: node scripts/extract_zone_nova_characters.mjs --upstream <dir> --out <file.json>");
    process.exit(1);
  }
  if (!exists(upstreamRoot)) {
    console.error(`Upstream root not found: ${upstreamRoot}`);
    process.exit(1);
  }

  const dir = findCharactersDir(upstreamRoot);
  if (!dir) {
    console.error(`Upstream characters dir not found under: ${upstreamRoot}`);
    process.exit(1);
  }

  const files = walkJsFiles(dir);
  if (!files.length) {
    console.error(`No .js files found in: ${dir}`);
    process.exit(1);
  }

  const result = [];
  for (const f of files) {
    const base = path.basename(f, ".js");
    const obj = await loadOne(f);

    const name = (obj?.name || base).toString();
    const element = normalizeElement(obj?.element || "");
    const cls = normalizeClass(obj?.class || obj?.classes || obj?.job || "");
    const rarity = normalizeRarity(obj?.rarity || obj?.rank || "");
    const role = normalizeRole(obj?.role || "", cls);

    const id = slugId(name) || slugId(base);

    result.push({
      id,
      name,
      element,
      class: cls,
      role,
      rarity,
      source_file: path.relative(upstreamRoot, f).replaceAll("\\", "/"),
    });
  }

  // 이름 기준으로 정렬
  result.sort((a, b) => a.name.localeCompare(b.name));

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(result, null, 2), "utf-8");
  console.log(`Wrote ${result.length} characters -> ${outPath}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
