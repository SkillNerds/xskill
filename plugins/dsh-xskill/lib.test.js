import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  discoverSkills,
  isSkillName,
  parseFrontmatter,
  parseSkillDirFromConfig,
  resolveSkillRoot,
  searchSkills,
  skillFromFrontmatter,
} from "./lib.js";

test("isSkillName accepts kebab-case only", () => {
  assert.equal(isSkillName("using-xskill"), true);
  assert.equal(isSkillName("invoice-check"), true);
  assert.equal(isSkillName("UsingXskill"), false);
  assert.equal(isSkillName("has_underscore"), false);
  assert.equal(isSkillName(""), false);
});

test("parseFrontmatter reads name, description, and body", () => {
  const parsed = parseFrontmatter(
    "---\nname: invoice-check\ndescription: Check invoices\n---\n\n# Hello\n",
  );
  assert.deepEqual(parsed.data, {
    name: "invoice-check",
    description: "Check invoices",
  });
  assert.match(parsed.body, /# Hello/);
});

test("parseFrontmatter keeps a literal block description", () => {
  const parsed = parseFrontmatter(
    "---\nname: invoice-check\ndescription: |\n  line one\n  line two\n---\nbody\n",
  );
  assert.equal(parsed.data.description, "line one\nline two");
});

test("skillFromFrontmatter rejects invalid names", () => {
  const parsed = skillFromFrontmatter(
    "---\nname: NotKebab\ndescription: x\n---\nbody\n",
  );
  assert.equal(parsed, undefined);
});

test("parseSkillDirFromConfig reads a scalar skill_dir", () => {
  const text = "# comment\nskill_dir: ~/.xskill/alt\nllm:\n  model: x\n";
  assert.equal(parseSkillDirFromConfig(text), "~/.xskill/alt");
});

test("resolveSkillRoot prefers explicit, then env, then home", () => {
  assert.equal(
    resolveSkillRoot("/tmp/lib", {}, "/home/u"),
    resolve("/tmp/lib"),
  );
  assert.equal(
    resolveSkillRoot("", { XSKILL_HOME: "/tmp/xs" }, "/home/u"),
    resolve(join("/tmp/xs", "skill")),
  );
  assert.equal(
    resolveSkillRoot("", {}, "/home/u"),
    resolve(join("/home/u", ".xskill", "skill")),
  );
});

test("discoverSkills lists directory packages and skips junk", async () => {
  const root = await mkdtemp(join(tmpdir(), "dsh-xskill-"));
  await mkdir(join(root, "invoice-check"));
  await writeFile(
    join(root, "invoice-check", "SKILL.md"),
    "---\nname: invoice-check\ndescription: Check invoices\n---\n\nUse the checker.\n",
  );
  await mkdir(join(root, ".canary"));
  await mkdir(join(root, "NotValid"));
  await writeFile(join(root, "NotValid", "SKILL.md"), "---\nname: NotValid\ndescription: x\n---\n");
  await writeFile(join(root, "README.md"), "ignore me\n");

  const skills = await discoverSkills(root);
  assert.equal(skills.length, 1);
  assert.equal(skills[0].name, "invoice-check");
  assert.match(skills[0].content, /Use the checker/);
});

test("searchSkills matches name and body", async () => {
  const skills = [
    { name: "invoice-check", description: "AP invoices", content: "VAT rules" },
    { name: "git-rebase", description: "history rewrite", content: "onto main" },
  ];
  const hits = searchSkills(skills, "vat", 10);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].name, "invoice-check");
});
