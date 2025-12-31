#!/usr/bin/env node
/**
 * Extract Zone Nova character modules from upstream repo directory and emit JSON.
 * - input: a directory containing per-character .js files (upstream)
 * - output: JSON array of characters (normalized)
 *
 * Usage:
 *   node scripts/extract_zone_nova_characters.mjs --input-dir <DIR> --out <FILE>
 */

import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";

function argValue(flag) {
  const i = process.argv.indexOf(flag);
  if (i === -1) return null;
  return process.argv[i + 1] ?? null;
}

function existsDir(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function listJsFiles(dir) {
  return fs
    .readdirSync(dir, { withFileTypes: true })
    .filter((d) => d.isFile())
    .map((d) => d.name)
    .filter((n) => n.toLowerCase().endsWith(".js"))
    // 흔한 인덱스 파일/보조 파일 제외(있다면)
    .filter((n) => !/^index\.js$/i.test(n));
}

function normalizeText(x) {
  return (x ?? "").toString().trim();
}

function slugifyId(nameOrFile) {
  return normalizeText(nameOrFile)
    .toLowerCase()
    .replace(/['"]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function pickField(obj, keys) {
  for (const k of keys) {
    if (obj && Object.prototype.hasOwnProperty.call(obj, k) && obj[k] != null) {
      return obj[k];
    }
  }
  return null;
}

async function loadCharacterModule(jsPath) {
  // dynamic import (ESM/CJS 모두 대응)
  const url = pathToFileURL(jsPath).href;
  const mod = await import(url);
  const data = mod?.default ?? mod?.character ?? mod;
  if (!data || typeof data !== "object") {
    throw new Error(`Invalid export object: ${jsPath}`);
  }
  return data;
}

function deriveRoleFromClass(cls, name) {
  const c = normalizeText(cls);
  const nm = normalizeText(name).toLowerCase();

  // 사용자가 지정한 룰:
  // - DPS = Warrior/Mage/Rogue
  // - Tank = Guardian
  // - Healer/Buffer/Debuffer는 동일명 매칭
  // - 예외: Apep은 Warrior지만 Tank 역할 가능 (여기서는 기본 role은 DPS로 두고, tank_capable=true 추가)
  if (["warrior", "mage", "rogue"].includes(c.toLowerCase())) return "DPS";
  if (c.toLowerCase() === "guardian") return "Tank";
  if (c.toLowerCase() === "healer") return "Healer";
  if (c.toLowerCase() === "buffer") return "Buffer";
  if (c.toLowerCase() === "debuffer") return "Debuffer";

  // fallback
  if (nm === "apep") return "DPS";
  return "";
}

async function main() {
  const inputDir = argValue("--input-dir");
  const outFile = argValue("--out");

  if (!inputDir || !outFile) {
    console.error("Usage: node scripts/extract_zone_nova_characters.mjs --input-dir <DIR> --out <FILE>");
    process.exit(1);
  }
  if (!existsDir(inputDir)) {
    console.error(`ERROR: input-dir not found: ${inputDir}`);
    process.exit(2);
  }

  const files = listJsFiles(inputDir);
  if (files.length === 0) {
    console.error(`ERROR: no .js files in: ${inputDir}`);
    process.exit(3);
  }

  const results = [];
  for (const f of files) {
    const jsPath = path.join(inputDir, f);
    let obj;
    try {
      obj = await loadCharacterModule(jsPath);
    } catch (e) {
      console.error(`WARN: failed to import ${jsPath}: ${e?.message ?? e}`);
      continue;
    }

    const name = normalizeText(
      pickField(obj, ["name", "Name", "displayName", "title"]) ?? path.parse(f).name
    );

    // 업스트림이 "class" 키를 쓴다고 하셨으니 최우선
    const cls = normalizeText(pickField(obj, ["class", "Class", "job", "Job"]) ?? "");
    const rarity = normalizeText(pickField(obj, ["rarity", "Rarity"]) ?? "");
    const element = normalizeText(pickField(obj, ["element", "Element"]) ?? "");

    // 이미지 파일명(업스트림 오브젝트에 image/portrait 등 있으면 그걸 우선 사용)
    // 없으면 파일명 기반 추정(추후 예외는 메타 생성 단계에서 override 가능)
    const image = normalizeText(
      pickField(obj, ["image", "portrait", "img", "art", "thumbnail"]) ?? ""
    );

    const id =
      normalizeText(pickField(obj, ["id", "key", "slug"])) ||
      slugifyId(path.parse(f).name || name);

    const role = deriveRoleFromClass(cls, name);
    const tankCapable = normalizeText(name).toLowerCase() === "apep"; // 예외 조건

    results.push({
      id,
      name,
      rarity,
      element,
      class: cls,
      role,
      tank_capable: tankCapable,
      image, // 빈 값일 수 있음(후처리 가능)
      _src_file: f, // 디버그용(원하면 나중에 삭제 가능)
    });
  }

  // 안정 정렬
  results.sort((a, b) => a.name.localeCompare(b.name));

  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  fs.writeFileSync(outFile, JSON.stringify(results, null, 2), "utf-8");

  console.log(`OK: extracted ${results.length} characters`);
  console.log(`OUT: ${outFile}`);
}

main().catch((e) => {
  console.error(`FATAL: ${e?.stack ?? e}`);
  process.exit(99);
});
