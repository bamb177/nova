import fs from "fs";
import path from "path";

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dir") args.dir = argv[++i];
    else if (a === "--out") args.out = argv[++i];
  }
  if (!args.dir || !args.out) {
    throw new Error("Usage: node extract_zone_nova_characters.mjs --dir <folder> --out <file.json>");
  }
  return args;
}

function isJsonFile(p) {
  return p.toLowerCase().endsWith(".json");
}

function walk(dir) {
  const out = [];
  const items = fs.readdirSync(dir, { withFileTypes: true });
  for (const it of items) {
    const full = path.join(dir, it.name);
    if (it.isDirectory()) out.push(...walk(full));
    else if (it.isFile() && isJsonFile(full)) out.push(full);
  }
  return out;
}

function safeReadJson(p) {
  try {
    const txt = fs.readFileSync(p, "utf-8");
    return JSON.parse(txt);
  } catch (e) {
    return null;
  }
}

function pick(obj, keys) {
  for (const k of keys) {
    if (obj && obj[k] !== undefined && obj[k] !== null && `${obj[k]}`.trim() !== "") return obj[k];
  }
  return "";
}

function normalizeRecord(filePath, j) {
  const base = path.basename(filePath, path.extname(filePath));

  // id 우선: json.id -> base filename
  const id = `${pick(j, ["id", "_id", "charId", "characterId"]) || base}`.trim();

  // name 후보: name, enName, title 등
  const name = `${pick(j, ["name", "enName", "displayName", "title"]) || id}`.trim();

  const rarity = `${pick(j, ["rarity", "grade", "tier"])}`.trim();
  const element = `${pick(j, ["element", "attr", "attribute"])}`.trim();
  const cls = `${pick(j, ["class", "job", "type", "role"])}`.trim();
  const faction = `${pick(j, ["faction", "group", "camp"])}`.trim();

  return { id, name, rarity, element, class: cls, faction };
}

function main() {
  const { dir, out } = parseArgs(process.argv);
  const absDir = path.resolve(dir);
  const absOut = path.resolve(out);

  if (!fs.existsSync(absDir)) {
    throw new Error(`dir not found: ${absDir}`);
  }

  const files = walk(absDir);
  const list = [];

  for (const fp of files) {
    const j = safeReadJson(fp);
    if (!j || typeof j !== "object") continue;

    // 파일 하나가 단일 캐릭터 json일 것으로 가정
    // (만약 배열/맵이면 최대한 풀어서 처리)
    if (Array.isArray(j)) {
      for (const item of j) {
        if (item && typeof item === "object") list.push(normalizeRecord(fp, item));
      }
    } else if (j.characters && Array.isArray(j.characters)) {
      for (const item of j.characters) {
        if (item && typeof item === "object") list.push(normalizeRecord(fp, item));
      }
    } else {
      list.push(normalizeRecord(fp, j));
    }
  }

  // id 기준 중복 제거
  const dedup = new Map();
  for (const x of list) {
    if (!x.id) continue;
    dedup.set(x.id, x);
  }

  const outList = Array.from(dedup.values()).sort((a, b) => a.id.localeCompare(b.id));

  fs.writeFileSync(absOut, JSON.stringify(outList, null, 2), "utf-8");
  console.log(`[ok] extracted: count=${outList.length} -> ${absOut}`);
}

main();
