// Pure helpers for the dsh-xskill bundle. No Cordis / schemastery imports so
// Node's built-in test runner can exercise them without a harness install.

import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { readdir, readFile, stat } from "node:fs/promises";

export const SKILL_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
export const DEFAULT_RANK = 350;
export const PROVIDER_NAME = "xskill";
export const SKIP_DIR_NAMES = new Set([".canary", ".git", "node_modules"]);

export function isSkillName(name) {
  return typeof name === "string" && SKILL_NAME.test(name);
}

export function expandUserPath(raw, home = homedir()) {
  const value = String(raw ?? "").trim();
  if (!value) return "";
  if (value === "~") return home;
  if (value.startsWith("~/") || value.startsWith("~\\")) {
    return join(home, value.slice(2));
  }
  return value;
}

export function parseSkillDirFromConfig(text) {
  if (typeof text !== "string" || !text) return "";
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const match = /^skill_dir:\s*(.+?)\s*$/.exec(trimmed);
    if (!match) continue;
    let value = match[1];
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (!value || value.endsWith(":") || value === "|" || value === ">") {
      return "";
    }
    return value;
  }
  return "";
}

export function resolveSkillRoot(configured, env = process.env, home = homedir()) {
  const explicit = expandUserPath(configured, home);
  if (explicit) return resolve(explicit);
  const fromEnv = String(env.XSKILL_SKILL_DIR || "").trim();
  if (fromEnv) return resolve(expandUserPath(fromEnv, home));
  const xskillHome = String(env.XSKILL_HOME || "").trim();
  if (xskillHome) return resolve(join(expandUserPath(xskillHome, home), "skill"));
  return resolve(join(home, ".xskill", "skill"));
}

export function parseFrontmatter(raw) {
  if (typeof raw !== "string") return undefined;
  const normalized = raw.replace(/^\uFEFF/, "");
  const firstLineEnd = normalized.indexOf("\n");
  if (firstLineEnd < 0) return undefined;
  if (normalized.slice(0, firstLineEnd).replace(/\r$/, "") !== "---") {
    return undefined;
  }
  const start = firstLineEnd + 1;
  const closing = findClosingFrontmatter(normalized, start);
  if (closing === undefined) return undefined;
  const data = parseSimpleYamlMap(normalized.slice(start, closing.start));
  if (data === undefined) return undefined;
  return {
    data,
    body: normalized.slice(closing.bodyStart),
  };
}

function findClosingFrontmatter(raw, start) {
  let lineStart = start;
  while (lineStart <= raw.length) {
    const nextNewline = raw.indexOf("\n", lineStart);
    const lineEnd = nextNewline < 0 ? raw.length : nextNewline;
    if (raw.slice(lineStart, lineEnd).replace(/\r$/, "") === "---") {
      return {
        start: lineStart,
        bodyStart: nextNewline < 0 ? raw.length : nextNewline + 1,
      };
    }
    if (nextNewline < 0) return undefined;
    lineStart = nextNewline + 1;
  }
  return undefined;
}

function parseSimpleYamlMap(text) {
  const data = {};
  const lines = text.split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith("#")) {
      i += 1;
      continue;
    }
    if (/^\s/.test(line)) return undefined;
    const match = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (!match) return undefined;
    const key = match[1];
    const rest = match[2];
    if (rest === "|" || rest === ">") {
      const folded = rest === ">";
      const block = [];
      i += 1;
      while (i < lines.length) {
        const next = lines[i];
        if (next === "" || /^\s/.test(next)) {
          block.push(next.replace(/^\s{2}/, ""));
          i += 1;
          continue;
        }
        break;
      }
      data[key] = folded
        ? block.join(" ").replace(/\s+/g, " ").trim()
        : block.join("\n").replace(/\s+$/, "");
      continue;
    }
    data[key] = unquoteScalar(rest);
    i += 1;
  }
  return data;
}

function unquoteScalar(value) {
  const trimmed = value.trim();
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1);
  }
  if (trimmed === "true") return true;
  if (trimmed === "false") return false;
  return trimmed;
}

export function skillFromFrontmatter(raw) {
  const parsed = parseFrontmatter(raw);
  if (!parsed) return undefined;
  const name = stringField(parsed.data, "name");
  const description = stringField(parsed.data, "description");
  if (!name || !description || !isSkillName(name)) return undefined;
  const whenToUse = stringField(parsed.data, "whenToUse");
  return {
    name,
    description,
    ...(whenToUse ? { whenToUse } : {}),
    content: parsed.body.trim(),
  };
}

function stringField(data, key) {
  const value = data[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export async function discoverSkills(skillRoot) {
  let entries;
  try {
    entries = await readdir(skillRoot, { withFileTypes: true });
  } catch (error) {
    if (error && (error.code === "ENOENT" || error.code === "ENOTDIR")) {
      return [];
    }
    throw error;
  }
  const skills = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.name.startsWith(".") || SKIP_DIR_NAMES.has(entry.name)) continue;
    const directory = join(skillRoot, entry.name);
    let kind = entry.isDirectory()
      ? "directory"
      : entry.isFile()
        ? "file"
        : "other";
    if (entry.isSymbolicLink()) {
      try {
        const info = await stat(directory);
        kind = info.isDirectory() ? "directory" : info.isFile() ? "file" : "other";
      } catch {
        continue;
      }
    }
    if (kind !== "directory") continue;
    const skillFile = join(directory, "SKILL.md");
    let raw;
    try {
      raw = await readFile(skillFile, "utf8");
    } catch {
      continue;
    }
    const parsed = skillFromFrontmatter(raw);
    if (!parsed) continue;
    skills.push({
      ...parsed,
      directory,
      path: skillFile,
    });
  }
  return skills;
}

export function searchSkills(skills, query, limit = 20) {
  const needle = String(query || "").trim().toLowerCase();
  const cap = Number.isInteger(limit) && limit > 0 ? limit : 20;
  if (!needle) return skills.slice(0, cap);
  const hits = [];
  for (const skill of skills) {
    const hay = [
      skill.name,
      skill.description,
      skill.whenToUse || "",
      skill.content || "",
    ]
      .join("\n")
      .toLowerCase();
    if (hay.includes(needle)) hits.push(skill);
    if (hits.length >= cap) break;
  }
  return hits;
}

export function summarizeSkill(skill) {
  return {
    name: skill.name,
    description: skill.description,
    ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
    path: skill.path,
  };
}
