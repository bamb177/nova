// scripts/extract_zone_nova_characters.mjs
import fs from "fs";
import path from "path";
import vm from "vm";

function usage() {
  console.log(`Usage:
  node scripts/extract_zone_nova_characters.mjs --upstream <dir> --out <file>

Example:
  node scripts/extract_zone_nova_characters.mjs --upstream _upstream_gacha_wiki --out public/data/zone-nova/characters.json
`);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--upstream") args.upstream = argv[++i];
    else if (a === "--out") args.out = argv[++i];
    else if (a === "-h" || a === "--help") args.help = true;
  }
  return args;
}

function sanitizeAndExecModule(jsPath) {
  let code = fs.readFileSync(jsPath, "utf8");

  // import 제거(데이터 모듈이 import를 쓰는 경우가 있어 VM 실패 방지)
  code = code.replace(/^\s*import\s+.*?;\s*$/gm, "");

  // export 변환
  const exportedNames = [];

  // export const X = ...  => const X = ...
  code = code.replace(/export\s+const\s+([A-Za-z0-9_]+)\s*=/g, (_, name) => {
    exportedNames.push(name);
    return `const ${name} =`;
  });

  // export { A, B } 제거
  code = code.replace(/export\s*\{\s*[^}]*\}\s*;?/gm, "");

  // export default Y  => module.exports = Y
  code = code.replace(/export\s+default\s+/g, "module.exports = ");

  // export default가 없고 export const만 있으면 그걸 module.exports로 묶기
  if (!/module\.exports\s*=/.test(code) && exportedNames.length > 0) {
    code += `\n;module.exports = { ${exportedNames.join(", ")} };`;
  }

  const context = { module: { exports: {} }, exports: {}, console };
  vm.createContext(context);

  vm.runInContext(code, context, { filename: jsPath, timeout: 2000 });
  return context.module.exports;
}

function loadSingleFile(jsPath) {
  const exp = sanitizeAndExecModule(jsPath);

  if (Array.isArray(exp)) return exp;
  if (exp && Array.isArray(exp.characters)) return exp.characters;
  if (exp && Array.isArray(exp.default)) return exp.default;

  // 단일 파일이 “객체 1개”를 export 하는 경우도 있으므로 배열로 감싸기
  if (exp && typeof exp === "object") return [exp];
  throw new Error(`추출 실패: ${jsPath}에서 캐릭터 데이터를 찾지 못했습니다.`);
}

function loadFromDirectory(dirPath) {
  if (!fs.existsSync(dirPath) || !fs.statSync(dirPath).isDirectory()) {
    throw new Error(`업스트림 캐릭터 디렉토리를 찾지 못했습니다: ${dirPath}`);
  }

  const files = fs.readdirSync(dirPath)
    .filter(f => f.toLowerCase().endsWith(".js"))
    .sort((a, b) => a.localeCompare(b));

  if (files.length === 0) {
    throw new Error(`디렉토리에 .js 파일이 없습니다: ${dirPath}`);
  }

  const out = [];
  for (const fn of files) {
    const fp = path.join(dirPath, fn);
    const exp = sanitizeAndExecModule(fp);

    // 케이스 1: export default { ...캐릭터... }
    if (exp && typeof exp === "object" && !Array.isArray(exp)) {
      const one = { ...exp };
      // id가 없으면 파일명 기반으로 보강(혹시 모를 케이스)
      if (!one.id && !one.name) {
        one.id = path.parse(fn).name;
      }
      out.push(one);
      continue;
    }

    // 케이스 2: export default [ ... ] (드물지만)
    if (Array.isArray(exp)) {
      for (const item of exp) {
        if (item && typeof item === "object") out.push(item);
      }
      continue;
    }

    // 케이스 3: module.exports = { character: {...} } 같은 형태
    if (exp && typeof exp === "object") {
      const vals = Object.values(exp);
      const dict = vals.find(v => v && typeof v === "object" && !Array.isArray(v));
      if (dict) out.push(dict);
      else throw new Error(`알 수 없는 export 형태: ${fp}`);
      continue;
    }

    throw new Error(`알 수 없는 export 형태: ${fp}`);
  }

  return out;
}

function main() {
  const args = parseArgs(process.argv);
  if (args.help || !args.upstream || !args.out) {
    usage();
    process.exit(args.help ? 0 : 1);
  }

  const upstreamRoot = path.resolve(args.upstream);

  const singlePath = path.join(upstreamRoot, "src", "data", "zone-nova", "characters.js");
  const dirPath = path.join(upstreamRoot, "src", "data", "zone-nova", "characters");

  let chars;
  if (fs.existsSync(singlePath)) {
    chars = loadSingleFile(singlePath);
  } else {
    chars = loadFromDirectory(dirPath);
  }

  const outPath = path.resolve(args.out);
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(chars, null, 2), "utf8");

  console.log(`OK: extracted ${chars.length} characters -> ${outPath}`);
}

main();
