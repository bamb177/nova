import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";

const ROOT = process.cwd();
const runesJsPath = path.join(ROOT, "public", "data", "zone-nova", "runes.js");
const outPath = path.join(ROOT, "public", "data", "zone-nova", "runes_export.json");

async function main() {
  const url = pathToFileURL(runesJsPath).href;

  // runes.js가 ESM(default export) 이든, named export든 최대한 흡수
  const mod = await import(url);
  const data =
    mod?.default ??
    mod?.runes ??
    mod?.RUNE_SETS ??
    mod;

  if (!data) {
    throw new Error("Failed to import runes.js (no export found).");
  }

  fs.writeFileSync(outPath, JSON.stringify(data, null, 2), "utf-8");
  console.log(`[OK] wrote ${path.relative(ROOT, outPath)}`);
}

main().catch((e) => {
  console.error("[ERR]", e);
  process.exit(1);
});
