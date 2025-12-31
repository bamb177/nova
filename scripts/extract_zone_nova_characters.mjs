// scripts/extract_zone_nova_characters.mjs
import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";

function arg(flag) {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? process.argv[i + 1] : null;
}

function walk(dir) {
  let files = [];
  for (const f of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, f.name);
    if (f.isDirectory()) files = files.concat(walk(p));
    else if (f.isFile() && p.endsWith(".js")) files.push(p);
  }
  return files;
}

function pickObject(mod) {
  if (!mod) return null;
  if (mod.default && typeof mod.default === "object") return mod.default;
  for (const v of Object.values(mod)) {
    if (v && typeof v === "object") return v;
  }
  return null;
}

function normalize(v) {
  return (v || "").toString().trim();
}

function slug(v) {
  return normalize(v).toLowerCase().replace(/[^a-z0-9]/g, "");
}

async function load(file) {
  try {
    const mod = await import(pathToFileURL(file).href);
    const obj = pickObject(mod);
    if (obj) return obj;
  } catch {}

  // fallback regex
  const txt = fs.readFileSync(file, "utf-8");
  const get = (r) => (txt.match(r)?.[1] || "").trim();
  return {
    name: get(/name\s*:\s*["'`]([^"'`]+)["'`]/i),
    element: get(/element\s*:\s*["'`]([^"'`]+)["'`]/i),
    class: get(/\bclass\s*:\s*["'`]([^"'`]+)["'`]/i),
    rarity: get(/rarity\s*:\s*["'`]([^"'`]+)["'`]/i),
    role: get(/role\s*:\s*["'`]([^"'`]+)["'`]/i),
  };
}

async function main() {
  const upstream = arg("--upstream");
  const out = arg("--out");
  if (!upstream || !out) {
    console.error("usage: --upstream <dir> --out <file>");
    process.exit(1);
  }

  const base = path.join(upstream, "src", "data", "zone-nova", "characters");
  if (!fs.existsSync(base)) {
    console.error("zone-nova characters dir not found:", base);
    process.exit(1);
  }

  const files = walk(base);
  const list = [];

  for (const f of files) {
    const obj = await load(f);
    const name =
      typeof obj.name === "string"
        ? obj.name
        : obj.name?.en || obj.name?.kr || obj.name?.jp || "";

    const id = slug(name) || slug(path.basename(f, ".js"));

    list.push({
      id,
      name,
      element: normalize(obj.element),
      class: normalize(obj.class || obj.classes),
      rarity: normalize(obj.rarity),
      role: normalize(obj.role),
      source_file: f.replace(upstream, "").replaceAll("\\", "/"),
    });
  }

  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(list, null, 2), "utf-8");
  console.log(`Extracted ${list.length} characters`);
}

main();
