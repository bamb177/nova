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

function loadCharactersFromJs(jsPath) {
  let code = fs.readFileSync(jsPath, "utf8");

  // 1) import 라인 제거 (데이터 파일에서 import만 쓰는 경우가 있어 VM에서 실패 방지)
  code = code.replace(/^\s*import\s+.*?;\s*$/gm, "");

  // 2) export 처리
  const hasExportDefault = /export\s+default\s+/m.test(code);

  // export const X = ...  => const X = ...
  const exportedNames = [];
  code = code.replace(/export\s+const\s+([A-Za-z0-9_]+)\s*=/g, (_, name) => {
    exportedNames.push(name);
    return `const ${name} =`;
  });

  // export { A, B } 제거
  code = code.replace(/export\s*\{\s*[^}]*\}\s*;?/gm, "");

  // export default Y  => module.exports = Y
  code = code.replace(/export\s+default\s+/g, "module.exports = ");

  // 3) export default가 없고 export const만 있는 경우 module.exports를 만들어줌
  if (!hasExportDefault) {
    if (exportedNames.length > 0) {
      code += `\n;module.exports = { ${exportedNames.join(", ")} };`;
    } else {
      // 마지막 안전장치: characters 변수가 있으면 내보내기
      code += `\n;module.exports = (typeof characters !== 'undefined') ? characters : module.exports;`;
    }
  }

  const context = {
    module: { exports: {} },
    exports: {},
    console,
  };
  vm.createContext(context);

  try {
    vm.runInContext(code, context, { filename: jsPath, timeout: 2000 });
  } catch (e) {
    throw new Error(`VM 실행 실패: ${e?.message || e}`);
  }

  const exp = context.module.exports;

  // exp가 배열인 경우
  if (Array.isArray(exp)) return exp;

  // exp.characters가 배열인 경우
  if (exp && Array.isArray(exp.characters)) return exp.characters;

  // exp.default가 배열인 경우(혹시 모를 케이스)
  if (exp && Array.isArray(exp.default)) return exp.default;

  throw new Error(`추출 실패: module.exports에서 캐릭터 배열을 찾지 못했습니다.`);
}

function main() {
  const args = parseArgs(process.argv);
  if (args.help || !args.upstream || !args.out) {
    usage();
    process.exit(args.help ? 0 : 1);
  }

  const upstreamRoot = path.resolve(args.upstream);
  const jsPath = path.join(upstreamRoot, "src", "data", "zone-nova", "characters.js");
  if (!fs.existsSync(jsPath)) {
    throw new Error(`업스트림 파일을 찾지 못했습니다: ${jsPath}`);
  }

  const chars = loadCharactersFromJs(jsPath);

  const outPath = path.resolve(args.out);
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(chars, null, 2), "utf8");

  console.log(`OK: extracted ${chars.length} characters -> ${outPath}`);
}

main();
