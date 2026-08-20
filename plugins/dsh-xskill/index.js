// dsh-xskill — DeepSeek Harness bundle for the xskill skill library.
//
// Ships as plain ESM so `dsh plugin add github:SkillNerds/xskill` needs no
// prepare / allowBuilds step. peer services (skills, tools, schemastery)
// come from the harness install.

import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { spawnSync } from "node:child_process";
import z from "@deepseek-ai/schemastery";
import {
  DEFAULT_RANK,
  PROVIDER_NAME,
  discoverSkills,
  parseSkillDirFromConfig,
  resolveSkillRoot,
  searchSkills,
  skillFromFrontmatter,
  summarizeSkill,
} from "./lib.js";

export const name = "dsh-xskill";
export const inject = ["skills", "tools"];

export const Config = z.object({
  skillRoot: z
    .string()
    .default("")
    .description(
      "Absolute or ~ path of the xskill skill library. Empty: XSKILL_SKILL_DIR, then skill_dir in ~/.xskill/config.yaml, then ~/.xskill/skill.",
    ),
  rank: z
    .number()
    .default(DEFAULT_RANK)
    .description(
      "Provider rank. Lower wins a duplicate name. 350 sits between dsh custom (300) and user-dsh (400).",
    ),
  registerGuide: z
    .boolean()
    .default(true)
    .description("Register the bundled using-xskill guide skill."),
});

const GUIDE_PATH = join(dirname(fileURLToPath(import.meta.url)), "using-xskill.md");

export function apply(ctx, config) {
  const skillRootPromise = resolveConfiguredSkillRoot(config.skillRoot);

  ctx.skills.registerProvider((control) => {
    return new XskillSkillProvider(ctx, control, {
      skillRootPromise,
      rank: Number.isFinite(config.rank) ? config.rank : DEFAULT_RANK,
    });
  });

  if (config.registerGuide !== false) {
    registerGuideSkill(ctx);
  }

  registerTools(ctx, skillRootPromise);
}

async function resolveConfiguredSkillRoot(configured) {
  const explicit = String(configured || "").trim();
  if (explicit) return resolveSkillRoot(explicit);
  try {
    const text = await readFile(join(homedir(), ".xskill", "config.yaml"), "utf8");
    const fromConfig = parseSkillDirFromConfig(text);
    if (fromConfig) return resolveSkillRoot(fromConfig);
  } catch {
    // Missing or unreadable config is fine: fall through to defaults.
  }
  return resolveSkillRoot("");
}

function registerGuideSkill(ctx) {
  // Cordis services are only safe to touch synchronously inside apply().
  let raw;
  try {
    raw = readFileSync(GUIDE_PATH, "utf8");
  } catch (error) {
    ctx.logger?.warn?.(`dsh-xskill: bundled using-xskill.md missing: ${error}`);
    return;
  }
  const parsed = skillFromFrontmatter(raw);
  if (!parsed) {
    ctx.logger?.warn?.("dsh-xskill: bundled using-xskill.md is invalid");
    return;
  }
  ctx.skills.register({
    name: parsed.name,
    description: parsed.description,
    ...(parsed.whenToUse ? { whenToUse: parsed.whenToUse } : {}),
    source: "bundled",
    content: parsed.content,
    path: GUIDE_PATH,
    resourceBase: {
      kind: "directory",
      path: dirname(GUIDE_PATH),
    },
  });
}

class XskillSkillProvider {
  constructor(ctx, control, options) {
    this.ctx = ctx;
    this.name = PROVIDER_NAME;
    this.skillRootPromise = options.skillRootPromise;
    this.rank = options.rank;
    this.control = control;
  }

  async list(options) {
    options?.signal?.throwIfAborted?.();
    const skillRoot = await this.skillRootPromise;
    const found = await discoverSkills(skillRoot);
    options?.signal?.throwIfAborted?.();
    return found.map((skill) => ({
      name: skill.name,
      description: skill.description,
      ...(skill.whenToUse ? { whenToUse: skill.whenToUse } : {}),
      invocation: { modelInvocable: true, userInvocable: true },
      provider: this.name,
      source: "xskill",
      rank: this.rank,
      locator: { path: skill.path, directory: skill.directory },
      resourceBase: { kind: "directory", path: skill.directory },
      path: skill.path,
    }));
  }

  async get(candidate, options) {
    options?.signal?.throwIfAborted?.();
    const path = candidate?.locator?.path || candidate?.path;
    if (!path) return undefined;
    let raw;
    try {
      raw = await readFile(path, "utf8");
    } catch {
      return undefined;
    }
    const parsed = skillFromFrontmatter(raw);
    if (!parsed) return undefined;
    return {
      name: parsed.name,
      description: parsed.description,
      ...(parsed.whenToUse ? { whenToUse: parsed.whenToUse } : {}),
      invocation: candidate.invocation || { modelInvocable: true, userInvocable: true },
      provider: this.name,
      source: candidate.source || "xskill",
      content: parsed.content,
      path,
      resourceBase: {
        kind: "directory",
        path: candidate?.locator?.directory || dirname(path),
      },
    };
  }
}

function registerTools(ctx, skillRootPromise) {
  const register = (def) => ctx.tools.register(def);
  const text = (s) => [{ type: "text", text: s }];

  register({
    name: "xskill_status",
    description:
      "Report the local xskill skill library this plugin is reading: root path, skill count, and whether the xskill CLI is on PATH. Does not start the daemon.",
    parameters: { type: "object", properties: {}, required: [] },
    output: {
      schema: { type: "object" },
      render: (_a, v) => {
        const lines = [
          `[xskill] root: ${v.skillRoot}`,
          `[xskill] exists: ${v.exists}`,
          `[xskill] skills: ${v.skillCount}`,
          `[xskill] cli: ${v.cliOnPath ? v.cliPath : "not on PATH"}`,
        ];
        if (v.note) lines.push(`[xskill] ${v.note}`);
        return text(lines.join("\n"));
      },
    },
    async execute() {
      const skillRoot = await skillRootPromise;
      let exists = false;
      let skillCount = 0;
      try {
        const skills = await discoverSkills(skillRoot);
        exists = true;
        skillCount = skills.length;
      } catch (error) {
        if (error && (error.code === "ENOENT" || error.code === "ENOTDIR")) {
          exists = false;
        } else {
          throw error;
        }
      }
      const cli = detectXskillCli();
      return {
        skillRoot,
        exists,
        skillCount,
        cliOnPath: Boolean(cli),
        cliPath: cli || "",
        note: exists
          ? "Plugin reads this directory. Run `xskill serve` or `xskill connect` to distill new skills."
          : "Skill root is missing. Install xskill (`pip install xskill`) and run `xskill serve` once to create ~/.xskill.",
      };
    },
  });

  register({
    name: "xskill_list",
    description:
      "List skills in the local xskill library (default ~/.xskill/skill). Returns name, description, and path. Use dsh's skill tool to load a body.",
    parameters: {
      type: "object",
      properties: {
        limit: { type: "number", description: "Max rows to return (default 100)." },
      },
      required: [],
    },
    output: {
      schema: { type: "array" },
      render: (_a, v) => {
        if (!v.length) return text("[xskill] no skills in the local library.");
        const lines = [`[xskill] ${v.length} skill(s):`];
        for (const skill of v) {
          lines.push(` - ${skill.name}: ${skill.description}`);
        }
        return text(lines.join("\n"));
      },
    },
    async execute(args) {
      const skillRoot = await skillRootPromise;
      const skills = await discoverSkills(skillRoot);
      const limit = Number.isInteger(args.limit) && args.limit > 0 ? args.limit : 100;
      return skills.slice(0, limit).map(summarizeSkill);
    },
  });

  register({
    name: "xskill_search",
    description:
      "Search the local xskill library by case-insensitive substring over name, description, and skill body. This is on-disk search, not the team-server HTTP API.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "Substring to match. Required." },
        limit: { type: "number", description: "Max matches (default 20)." },
      },
      required: ["query"],
    },
    output: {
      schema: { type: "array" },
      render: (_a, v) => {
        if (!v.length) return text("[xskill] no matches.");
        const lines = ["[xskill] matches:"];
        for (const skill of v) {
          lines.push(` - ${skill.name}: ${skill.description}`);
        }
        return text(lines.join("\n"));
      },
    },
    async execute(args) {
      const query = String(args.query || "").trim();
      if (!query) throw new Error("dsh-xskill: query is required");
      const skillRoot = await skillRootPromise;
      const skills = await discoverSkills(skillRoot);
      return searchSkills(skills, query, args.limit).map(summarizeSkill);
    },
  });
}

function detectXskillCli() {
  const which = spawnSync(process.platform === "win32" ? "where" : "which", ["xskill"], {
    encoding: "utf8",
    timeout: 4000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (which.error && which.error.code === "ENOENT") return "";
  if (which.status !== 0) return "";
  return (which.stdout || "").split(/\r?\n/).find(Boolean) || "";
}
