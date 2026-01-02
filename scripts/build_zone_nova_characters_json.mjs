import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";

// 출력은 고정 (여길 매번 싹 비우고 새로 생성)
const OUT_DIR = path.resolve("public/data/zone-nova/characters");

// 워크플로우에서 지정: 외부 원본 repo 루트 (예: _upstream_gachawiki)
const SEARCH_ROOT = process.env.ZONE_NOVA_SEARCH_ROOT
  ? path.resolve(process.env.ZONE_NOVA_SEARCH_ROOT)
  : process.cwd();

// ---------- util ----------
async function exists(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

async function wipeDirContents(dirPath) {
  await fs.mkdir(dirPath, { recursive: true });
  const entries = await fs.readdir(dirPath, { withFileTypes: true });
  await Promise.all(entries.map((e) => fs.rm(path.join(dirPath, e.name), { recursive: true, force: true })));
}

async function findZoneNovaCharacterFiles(root) {
  const results = [];
  async function walk(dir) {
    const ents = await fs.readdir(dir, { withFileTypes: true });
    for (const e of ents) {
      if (e.isDirectory()) {
        if (e.name === ".git" || e.name === "node_modules") continue;
        await walk(path.join(dir, e.name));
      } else if (e.isFile()) {
        const full = path.join(dir, e.name);
        const norm = full.split(path.sep).join("/");

        // zone-nova/characters 아래의 .js/.ts만
        if (norm.includes("/zone-nova/characters/") && (norm.endsWith(".js") || norm.endsWith(".ts"))) {
          results.push(full);
        }
      }
    }
  }
  await walk(root);
  return results.sort();
}

function pickExport(mod, filePath) {
  if (mod && mod.default !== undefined) return mod.default;

  if (mod && typeof mod === "object") {
    const keys = Object.keys(mod).filter((k) => mod[k] !== undefined);
    if (keys.length === 1) return mod[keys[0]];
    if (keys.length > 1) {
      const preferred = ["data", "character", "characters", "meta", "payload"];
      for (const k of preferred) if (mod[k] !== undefined) return mod[k];
      return mod[keys[0]];
    }
  }
  throw new Error(`No usable export found: ${filePath}`);
}

function sortKeysDeep(v) {
  if (Array.isArray(v)) return v.map(sortKeysDeep);
  if (v && typeof v === "object" && v.constructor === Object) {
    const o = {};
    for (const k of Object.keys(v).sort()) o[k] = sortKeysDeep(v[k]);
    return o;
  }
  return v;
}

function stableJsonStringify(value, space = 2) {
  const replacer = (_k, v) => {
    if (typeof v === "bigint") return v.toString();
    if (typeof v === "function") throw new Error("Function is not JSON-serializable");
    return v;
  };
  return JSON.stringify(sortKeysDeep(value), replacer, space) + "\n";
}

// esbuild로 JS/TS를 “import 가능한 ESM”으로 변환
async function transformToEsmWithEsbuild(sourceCode, loader) {
  const esbuild = await import("esbuild");
  const result = await esbuild.transform(sourceCode, {
    loader,              // "js" | "ts"
    format: "esm",
    target: "es2020",
    sourcemap: false,
  });
  return result.code;
}

async function loadModuleValue(filePath, tmpDir) {
  const source = await fs.readFile(filePath, "utf8");
  const ext = path.extname(filePath).toLowerCase();
  const loader = ext === ".ts" ? "ts" : "js";

  const esm = await transformToEsmWithEsbuild(source, loader);

  const base = path.basename(filePath, ext);
  const hash = crypto.createHash("sha1").update(filePath).digest("hex").slice(0, 8);
  const tmpFile = path.join(tmpDir, `${base}.${hash}.mjs`);
  await fs.writeFile(tmpFile, esm, "utf8");

  const mod = await import(pathToFileURL(tmpFile).href + `?v=${Date.now()}`);
  return pickExport(mod, filePath);
}

// ---------- main ----------
async function main() {
  if (!(await exists(SEARCH_ROOT))) {
    throw new Error(`SEARCH_ROOT not found: ${SEARCH_ROOT}`);
  }

  const srcFiles = await findZoneNovaCharacterFiles(SEARCH_ROOT);
  if (srcFiles.length === 0) {
    throw new Error(
      `No source .js/.ts found under SEARCH_ROOT=${SEARCH_ROOT}\n` +
      `Looking for */zone-nova/characters/*.(js|ts)`
    );
  }

  console.log(`[SRC] SEARCH_ROOT=${SEARCH_ROOT}`);
  console.log(`[SRC] Found ${srcFiles.length} files`);

  // 출력 폴더 싹 비우기
  await wipeDirContents(OUT_DIR);

  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "zone-nova-js2json-"));
  try {
    let ok = 0;
    for (const f of srcFiles) {
      const value = await loadModuleValue(f, tmpDir);
      const outName = path.basename(f).replace(/\.(js|ts)$/i, ".json");
      const outFile = path.join(OUT_DIR, outName);
      await fs.writeFile(outFile, stableJsonStringify(value, 2), "utf8");
      ok += 1;
    }
    console.log(`[OK] Wrote ${ok} json files -> ${OUT_DIR}`);
  } finally {
    await fs.rm(tmpDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("[FAIL]", err);
  process.exit(1);
});
