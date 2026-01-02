/**
 * Zone Nova 캐릭터 데이터 "원본 그대로" 동기화 스크립트
 *
 * 목적:
 * - upstream(외부 원본 repo 등)에서 zone-nova/characters 아래의 .js/.ts 파일을
 *   "주석/포맷/내용 변경 없이" 그대로 복사한다.
 * - public/data/zone-nova/characters 폴더는 매번 실행 시 "싹 비우고(wipe)" 다시 채운다.
 *
 * 주의:
 * - 이 스크립트는 JSON 변환을 하지 않는다.
 *   (JSON으로 변환하면 주석이 유실되고 포맷이 변경되므로 '원본 그대로' 요구사항을 충족할 수 없음)
 *
 * 사용:
 * - GitHub Actions에서 upstream repo를 clone 한 뒤,
 *   env.ZONE_NOVA_SEARCH_ROOT를 upstream 루트 폴더로 지정해 실행한다.
 *   예) ZONE_NOVA_SEARCH_ROOT=_upstream_gachawiki
 */

import fs from "node:fs/promises";
import path from "node:path";

const OUT_DIR = path.resolve("public/data/zone-nova/characters");

// 워크플로우에서 upstream 루트 경로를 넘겨준다.
const SEARCH_ROOT = process.env.ZONE_NOVA_SEARCH_ROOT
  ? path.resolve(process.env.ZONE_NOVA_SEARCH_ROOT)
  : process.cwd();

/**
 * OUT_DIR 내부를 완전히 비운다(폴더 자체는 유지).
 */
async function wipeDirContents(dirPath) {
  await fs.mkdir(dirPath, { recursive: true });
  const entries = await fs.readdir(dirPath, { withFileTypes: true });
  await Promise.all(
    entries.map((e) => fs.rm(path.join(dirPath, e.name), { recursive: true, force: true }))
  );
}

/**
 * SEARCH_ROOT 이하에서 "zone-nova/characters" 경로를 포함하는 .js/.ts 파일을 전부 수집한다.
 * - .git / node_modules 는 탐색에서 제외한다.
 */
async function findSourceFiles(root) {
  const results = [];

  async function walk(dir) {
    const ents = await fs.readdir(dir, { withFileTypes: true });

    for (const e of ents) {
      const full = path.join(dir, e.name);

      if (e.isDirectory()) {
        if (e.name === ".git" || e.name === "node_modules") continue;
        await walk(full);
        continue;
      }

      if (!e.isFile()) continue;

      const norm = full.split(path.sep).join("/");

      // zone-nova/characters 아래의 JS/TS만 대상으로 한다.
      if (
        norm.includes("/zone-nova/characters/") &&
        (norm.endsWith(".js") || norm.endsWith(".ts"))
      ) {
        results.push(full);
      }
    }
  }

  await walk(root);
  return results.sort();
}

/**
 * upstream에서 찾은 파일 경로로부터, OUT_DIR에 복사될 상대 경로를 만든다.
 * - 예: .../zone-nova/characters/sub/a.js -> sub/a.js
 * - 이렇게 하면 upstream에 하위 폴더가 있어도 구조를 보존한다.
 */
function toRelativeUnderCharacters(absPath) {
  const norm = absPath.split(path.sep).join("/");
  const marker = "/zone-nova/characters/";
  const idx = norm.lastIndexOf(marker);

  if (idx < 0) {
    // 이 케이스는 findSourceFiles 조건상 거의 발생하지 않지만, 방어적으로 처리.
    return path.basename(absPath);
  }

  const rel = norm.slice(idx + marker.length); // marker 이후 경로
  // rel은 '/' 기준이므로, 현재 OS 경로 구분자로 변환
  return rel.split("/").join(path.sep);
}

/**
 * 파일 복사(원본 그대로). 읽어서 쓰는 방식이 아니라 copyFile을 사용해 변경 여지를 제거한다.
 */
async function copyFilePreserve(src, dst) {
  await fs.mkdir(path.dirname(dst), { recursive: true });
  await fs.copyFile(src, dst);
}

async function main() {
  // SEARCH_ROOT 존재 여부 확인
  try {
    await fs.access(SEARCH_ROOT);
  } catch {
    throw new Error(`SEARCH_ROOT not found: ${SEARCH_ROOT}`);
  }

  const files = await findSourceFiles(SEARCH_ROOT);

  if (files.length === 0) {
    throw new Error(
      `No source files found under SEARCH_ROOT=${SEARCH_ROOT}\n` +
      `Looking for */zone-nova/characters/*.(js|ts)`
    );
  }

  console.log(`[SRC] SEARCH_ROOT=${SEARCH_ROOT}`);
  console.log(`[SRC] Found ${files.length} files`);

  // 출력 폴더 싹 비움
  await wipeDirContents(OUT_DIR);

  // 원본 그대로 복사
  for (const f of files) {
    const rel = toRelativeUnderCharacters(f);
    const outFile = path.join(OUT_DIR, rel);
    await copyFilePreserve(f, outFile);
  }

  console.log(`[OK] Copied ${files.length} files -> ${OUT_DIR}`);
}

main().catch((err) => {
  console.error("[FAIL]", err);
  process.exit(1);
});
