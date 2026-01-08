// scripts/export_runes_json.mjs
import fs from "fs";
import path from "path";
import vm from "vm";
import { pathToFileURL } from "url";

const ROOT = process.cwd();
const RUNES_JS = path.join(ROOT, "public", "data", "zone-nova", "runes.js");
const OUT_JSON = path.join(ROOT, "public", "data", "zone-nova", "runes_export.json");

function pickExport(mod) {
  return (
    mod?.default ??
    mod?.runes ??
    mod?.RUNES ??
    mod?.RUNE_SETS ??
    mod?.sets ??
    mod
  );
}

function tryEvalFallback(src) {
  // 1) 가장 흔한 export default 변형 제거
  let code = src;

  // export default <expr>;
  code = code.replace(/export\s+default\s+/g, "const __DEFAULT_EXPORT__ = ");
  // export const X = ...
  code = code.replace(/export\s+(const|let|var)\s+/g, "$1 ");
  // export { a, b as c };
  code = code.replace(/export\s*\{[^}]*\}\s*;?/g, "");

  // 가장 그럴듯한 변수명을 추정 (const XXX = [ ... ])
  const m = code.match(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\[/);
  const guessedName = m?.[1];

  // sandbox 평가
  const sandbox = {
    console,
    __DEFAULT_EXPORT__: undefined,
  };
  vm.createContext(sandbox);

  vm.runInContext(code, sandbox, { timeout: 2000 });

  // 우선순위: __DEFAULT_EXPORT__ > guessedName > 전역에 존재하는 후보
  const candidates = [
    "__DEFAULT_EXPORT__",
    guessedName,
    "runes",
    "RUNES",
    "RUNE_SETS",
    "sets",
    "data",
  ].filter(Boolean);

  for (const key of candidates) {
    if (sandbox[key] != null) return sandbox[key];
  }

  // 마지막: sandbox의 object 중 array/list-like 찾기
  for (const [k, v] of Object.entries(sandbox)) {
    if (Array.isArray(v) && v.length) return v;
  }

  return null;
}

async function main() {
  if (!fs.existsSync(RUNES_JS)) {
    throw new Error(`runes.js not found: ${RUNES_JS}`);
  }

  let data = null;

  // 1) ESM import 시도
  try {
    const url = pathToFileURL(RUNES_JS).href + `?v=${Date.now()}`;
    const mod = await import(url);
    data = pickExport(mod);
  } catch (e) {
    // ignore and fallback
  }

  // 2) eval fallback
  if (!data) {
    const src = fs.readFileSync(RUNES_JS, "utf-8");
    data = tryEvalFallback(src);
  }

  if (!data) {
    throw new Error("Failed to export runes.js: no usable export detected.");
  }

  fs.writeFileSync(OUT_JSON, JSON.stringify(data, null, 2), "utf-8");
  console.log(`[OK] wrote ${path.relative(ROOT, OUT_JSON)}`);
}

main().catch((e) => {
  console.error("[ERR]", e);
  process.exit(1);
});
