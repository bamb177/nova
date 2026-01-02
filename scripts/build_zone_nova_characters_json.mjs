import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

const SRC_DIR = path.resolve("src/data/zone-nova/characters");
const OUT_DIR = path.resolve("public/data/zone-nova/characters");

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
  await Promise.all(
    entries.map(async (e) => {
      const full = path.join(dirPath, e.name);
      if (e.isDirectory()) {
        await fs.rm(full, { recursive: true, force: true });
      } else {
        await fs.rm(full, { force: true });
      }
    })
  );
}

async function listJsFiles(dirPath) {
  const entries = await fs.readdir(dirPath, { withFileTypes: true });
  const out = [];
  for (const e of entries) {
    const full = path.join(dirPath, e.name);
    if (e.isDirectory()) {
      // 하위 폴더가 있을 수도 있으니 재귀 처리
      out.push(...(await listJsFiles(full)));
    } else if (e.isFile() && e.name.toLowerCase().endsWith(".js")) {
      out.push(full);
    }
  }
  return out.sort();
}

function pickExport(mod, filePath) {
  if (mod && mod.default !== undefined) return mod.default;

  // named export 중 하나만 있으면 그걸 사용
  if (mod && typeof mod === "object") {
    const keys = Object.keys(mod).filter((k) => mod[k] !== undefined);
    if (keys.length === 1) return mod[keys[0]];
    if (keys.length > 1) {
      // 우선순위 후보
      const preferred = ["data", "character", "characters", "meta", "payload"];
      for (const k of preferred) if (mod[k] !== undefined) return mod[k];
      // 마지막 fallback: 첫 번째
      return mod[keys[0]];
    }
  }

  throw new Error(`No usable export found in: ${filePath}`);
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
  // BigInt 등 JSON 불가 타입 처리
  const replacer = (_k, v) => {
    if (typeof v === "bigint") return v.toString();
    if (typeof v === "function") {
      throw new Error("Function value is not JSON-serializable");
    }
    return v;
  };
  return JSON.stringify(sortKeysDeep(value), replacer, space) + "\n";
}

function looksLikeESM(sourceText) {
  // “export …” 가 실제로 들어가면 ESM로 판단 (대부분의 데이터 모듈에서 충분)
  return /\bexport\s+(default|const|function|class|let|var)\b/.test(sourceText) || /^export\s/m.test(sourceText);
}

async function loadJsModuleValue(filePath, tmpDir) {
  const source = await fs.readFile(filePath, "utf8");

  // 1) ESM로 보이면 임시 .mjs로 복사 후 import
  if (looksLikeESM(source)) {
    const base = path.basename(filePath, ".js");
    const hash = crypto.createHash("sha1").update(filePath).digest("hex").slice(0, 8);
    const tmpFile = path.join(tmpDir, `${base}.${hash}.mjs`);
    await fs.writeFile(tmpFile, source, "utf8");

    // cache busting
    const mod = await import(pathToFileURL(tmpFile).href + `?v=${Date.now()}`);
    return pickExport(mod, filePath);
  }

  // 2) 그 외는 CJS로 간주하고 require
  //    (CJS를 ESM import로 불러오는 것도 가능하지만, require가 더 확실함)
  const mod = require(filePath);
  return (mod && mod.default !== undefined) ? mod.default : mod;
}

async function main() {
  if (!(await exists(SRC_DIR))) {
    throw new Error(`Source dir not found: ${SRC_DIR}`);
  }

  const jsFiles = await listJsFiles(SRC_DIR);
  if (jsFiles.length === 0) {
    throw new Error(`No .js files found under: ${SRC_DIR}`);
  }

  // 출력 폴더 “싹 비우기”
  await wipeDirContents(OUT_DIR);

  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "zone-nova-js2json-"));

  try {
    let ok = 0;
    for (const f of jsFiles) {
      const value = await loadJsModuleValue(f, tmpDir);

      const base = path.basename(f, ".js");
      const outFile = path.join(OUT_DIR, `${base}.json`);

      const json = stableJsonStringify(value, 2);
      await fs.writeFile(outFile, json, "utf8");
      ok += 1;
    }

    console.log(`[OK] Converted ${ok} files -> ${OUT_DIR}`);
  } finally {
    await fs.rm(tmpDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("[FAIL]", err);
  process.exit(1);
});
