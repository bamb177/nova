import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";

function exists(p) {
  try { fs.accessSync(p); return true; } catch { return false; }
}

function titleCase(s) {
  const v = (s ?? "").toString().trim();
  if (!v) return "";
  return v.charAt(0).toUpperCase() + v.slice(1).toLowerCase();
}

function safeStr(v) {
  return (v ?? "").toString().trim();
}

// 업스트림 캐릭터 JS 파일을 dynamic import로 로딩
async function loadCharacterModule(jsPath) {
  // Windows/Posix 호환 위해 file:// URL 사용
  const url = pathToFileURL(jsPath).href;
  const mod = await import(url);
  // export default / named export / module.exports 형태 모두 대응
  return mod?.default ?? mod?.character ?? mod ?? null;
}

async function main() {
  const args = process.argv.slice(2);
  const getArg = (k, def = null) => {
    const i = args.indexOf(k);
    if (i >= 0 && i + 1 < args.length) return args[i + 1];
    return def;
  };

  const dirPath = getArg("--dir");
  const outPath = getArg("--out");

  if (!dirPath || !outPath) {
    console.error("Usage: node extract_zone_nova_characters.mjs --dir <upstream_characters_dir> --out <output_json_path>");
    process.exit(1);
  }
  if (!exists(dirPath)) {
    throw new Error(`업스트림 캐릭터 디렉터리를 찾지 못했습니다: ${dirPath}`);
  }

  const files = fs.readdirSync(dirPath).filter(f => f.toLowerCase().endsWith(".js"));
  if (!files.length) {
    throw new Error(`업스트림 캐릭터 .js 파일이 없습니다: ${dirPath}`);
  }

  const chars = [];
  for (const f of files) {
    const jsPath = path.join(dirPath, f);
    let data;
    try {
      data = await loadCharacterModule(jsPath);
    } catch (e) {
      // 일부 파일이 import 불가해도 전체 실패하지 않도록 경고만 출력
      console.warn(`[warn] import failed: ${jsPath} :: ${e?.message || e}`);
      continue;
    }
    if (!data || typeof data !== "object") continue;

    // 업스트림 키들은 프로젝트마다 조금씩 다를 수 있으므로 다중 키 대응
    const id =
      safeStr(data.id) ||
      safeStr(data.key) ||
      safeStr(data.slug) ||
      safeStr(path.parse(f).name);

    const name =
      safeStr(data.name) ||
      safeStr(data.title) ||
      safeStr(data.displayName) ||
      id;

    const rarity = safeStr(data.rarity || data.grade).toUpperCase(); // SSR/SR/R
    const element = titleCase(data.element || data.attr || data.attribute);
    const cls = titleCase(data.class || data.job || data.roleClass); // ※ 여기서는 'class'를 뽑음(중요)
    const faction = safeStr(data.faction || data.camp || data.affiliation || data.group);

    chars.push({
      id,             // 내부 키(영문)
      name,           // 표시 이름(동기화 후 sync에서 오버라이드/변환 가능)
      rarity: rarity || "",
      element: element || "",
      class: cls || "",
      faction: faction || "",
    });
  }

  // id 기준 정렬 (안정적 diff)
  chars.sort((a, b) => (a.id || "").localeCompare(b.id || ""));

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(chars, null, 2), "utf-8");

  console.log(`[ok] extracted characters: ${chars.length}`);
  console.log(`[ok] output: ${outPath}`);
}

main().catch((e) => {
  console.error("[error]", e?.stack || e);
  process.exit(1);
});
