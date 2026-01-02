import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

// 출력은 고정: 여기 “싹 비우고” json로 채움
const OUT_DIR = path.resolve("public/data/zone-nova/characters");

// 소스 후보(원하는 경로가 있으면 여기 우선순위로 잡힘)
const SRC_CANDIDATES = [
  path.resolve("src/data/zone-nova/characters"),           // 사용자가 원한 경로
  path.resolve("public/data/zone-nova/characters"),        // 혹시 여기에 js가 있는 경우
  path.resolve("public/data/zone-nova/characters_src"),    // 예비
];

// 레포 전체에서 zone-nova/characters/*.js 자동 탐색 fallback
async function findAllJsUnderRepoRoot(repoRoot) {
  const results = [];
  async function walk(dir) {
    const ents = await fs.readdir(dir, { withFileTypes: true });
    for (const e of ents) {
      // .git, node_modules 등 대형 디렉토리 스킵
      if (e.isDirectory()) {
        if (e.name === ".git" || e.name === "node_modules") continue;
        await walk(path.join(dir, e.name));
      } else if (e.isFile() && e.name.toLowerCase().endsWith(".js")) {
        const full = path.join(dir, e.name);
        // 경로에 zone-nova/characters 포함되는 것만
        const norm = full.split(path.sep).join("/");
        if (norm.includes("/zone-nova/characters/")) results.push(full);
      }
    }
  }
  await walk(repoRoot);
  return results.sort();
}

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
      await fs.rm(full, { recursive: true, force: true });
    })
  );
}

async function listJsFiles(dirPath) {
  const out = [];
  async function walk(d) {
    const entries = await fs.readdir(d, { withFileTypes: true });
    for (const e of entries) {
      const full = path.join(d, e.name);
      if (e.isDirectory()) await walk(full);
      else if (e.isFile() && e.name.toLowerCase().endsWith(".js")) out.push(full);
    }
  }
  await walk(dirPath);
  return out.sort();
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
  const replacer = (_k, v) => {
    if (typeof v === "bigint") return v.toString();
    if (typeof v === "function") throw new Error("Function is not JSON-serializable");
    return v;
  };
  return JSON.stringify(sortKeysDeep(value), replacer, space) + "\n";
}

function looksLikeESM(sourceText) {
  return (
    /\bexport\s+(default|const|function|class|let|var)\b/.test(sourceText) ||
    /^export\s/m.test(sourceText)
  );
}

async function loadJsModuleValue(filePath, tmpDir) {
  const source = await fs.readFile(filePath, "utf8");

  // ESM이면 임시 .mjs로 만들어 import
  if (looksLikeESM(source)) {
    const base = path.basename(filePath, ".js");
    const hash = crypto.createHash("sha1").update(filePath).digest("hex").slice(0, 8);
    const tmpFile = path.join(tmpDir, `${base}.${hash}.mjs`);
    await fs.writeFile(tmpFile, source, "utf8");

    const mod = await import(pathToFileURL(tmpFile).href + `?v=${Date.now()}`);
    return pickExport(mod, filePath);
  }

  // CJS로 간주
  const mod = require(filePath);
  return mod && mod.default !== undefined ? mod.default : mod;
}

async function resolveSourceFiles() {
  // 1) 후보 디렉토리 중 존재하는 곳을 우선 사용
  for (const cand of SRC_CANDIDATES) {
    if (await exists(cand)) {
      const files = await listJsFiles(cand);
      if (files.length > 0) {
        console.log(`[SRC] Using candidate dir: ${cand} (${files.length} files)`);
        return files;
      }
    }
  }

  // 2) 레포 전체에서 fallback 탐색
  const repoRoot = process.cwd();
  const files = await findAllJsUnderRepoRoot(repoRoot);
  if (files.length > 0) {
    console.log(`[SRC] Auto-discovered files: ${files.length}`);
    // 같은 디렉토리로 모인 경우가 대부분이라, 참고용 출력
    const sample = files.slice(0, 5).map((f) => " - " + f).join("\n");
    console.log(`[SRC] Sample:\n${sample}`);
    return files;
  }

  // 3) 아무것도 못 찾으면 명확히 실패
  throw new Error(
    `No source .js files found.\n` +
      `Tried candidates:\n` +
      SRC_CANDIDATES.map((p) => ` - ${p}`).join("\n") +
      `\nAnd repo-wide search for */zone-nova/characters/*.js`
  );
}

async function main() {
  const jsFiles = await resolveSourceFiles();

  // 출력 폴더 “싹 비우기”
  await wipeDirContents(OUT_DIR);

  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "zone-nova-js2json-"));
  try {
    let ok = 0;

    for (const f of jsFiles) {
      const value = await loadJsModuleValue(f, tmpDir);

      // 파일명만 사용해서 OUT에 json 생성
      const base = path.basename(f, ".js");
      const outFile = path.join(OUT_DIR, `${base}.json`);

      await fs.writeFile(outFile, stableJsonStringify(value, 2), "utf8");
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
