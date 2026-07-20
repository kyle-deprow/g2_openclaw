#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, readFile, readdir, rename, stat, unlink, writeFile } from "node:fs/promises";
import { statSync } from "node:fs";
import path from "node:path";
import process from "node:process";

const REQUIRED_OPENCLAW_VERSION = "2026.7.1-2";
const PATCH_MARKER = "g2_openclaw:codex-auto-compaction-no-fallback:v1";
const PACKAGE_ROOT_OVERRIDE = process.env.OPENCLAW_PACKAGE_ROOT?.trim();
const VULNERABLE_BRANCH =
  /if\s*\(\s*isIntentionalNativeAutoCompactionSkip\s*\(\s*result\s*\)\s*\)\s*return\s*\{\s*compacted\s*:\s*false\s*,\s*fallbackToContextEngine\s*:\s*true\s*,\s*failureReason\s*:\s*CODEX_APP_SERVER_OWNS_AUTO_COMPACTION_REASON\s*\}\s*;?/g;
const PATCHED_BRANCH = `if(isIntentionalNativeAutoCompactionSkip(result))return{compacted:false};/* ${PATCH_MARKER} */`;

function fail(message) {
  console.error(`ERROR: ${message}`);
  process.exitCode = 1;
}

function countMatches(source, pattern) {
  pattern.lastIndex = 0;
  const count = (source.match(pattern) ?? []).length;
  pattern.lastIndex = 0;
  return count;
}

function countOccurrences(source, needle) {
  return source.split(needle).length - 1;
}

function resolveOpenClawBinary() {
  const candidates = [];
  if (process.env.OPENCLAW_BIN?.trim()) candidates.push(process.env.OPENCLAW_BIN.trim());
  candidates.push(`${process.env.HOME}/.local/share/pnpm/openclaw`);
  candidates.push(`${process.env.HOME}/.local/bin/openclaw`);
  for (const entry of (process.env.PATH ?? "").split(":")) {
    if (entry) candidates.push(path.join(entry, "openclaw"));
  }
  const seen = new Set();
  for (const candidate of candidates) {
    if (seen.has(candidate)) continue;
    seen.add(candidate);
    try {
      statSync(candidate);
      return path.resolve(candidate);
    } catch {
      // Continue through the explicit candidate list; absence is handled below.
    }
  }
  throw new Error("OpenClaw executable was not found in OPENCLAW_BIN, the managed pnpm paths, or PATH");
}

async function resolvePackageRoot(binary, visited = new Set()) {
  const resolvedBinary = path.resolve(binary);
  if (visited.has(resolvedBinary)) {
    throw new Error(`OpenClaw launcher chain contains a cycle at ${resolvedBinary}`);
  }
  visited.add(resolvedBinary);
  if (PACKAGE_ROOT_OVERRIDE) return path.resolve(PACKAGE_ROOT_OVERRIDE);

  const binaryText = await readFile(resolvedBinary, "utf8").catch(() => "");
  const absoluteMatches = [
    ...binaryText.matchAll(
      /(?:^|[\s"'=])(\/[^\n"'\s]+\/node_modules\/openclaw)(?:\/openclaw\.mjs|\/dist\/index\.js)/gm,
    ),
  ]
    .map((match) => match[1])
    .filter((match) => match.startsWith("/"))
    .map((match) => path.resolve(match));
  const relativeMatch = binaryText.match(/\$basedir\/(global\/[^\n\"]+\/node_modules\/openclaw)\/openclaw\.mjs/);
  const matches = relativeMatch
    ? [...absoluteMatches, path.resolve(path.dirname(resolvedBinary), relativeMatch[1])]
    : absoluteMatches;
  const uniqueMatches = [...new Set(matches)];
  if (uniqueMatches.length === 1) return uniqueMatches[0];

  const delegatedLauncher = binaryText.match(/(?:^|\n)\s*exec\s+(\/[^\s"']+)\s+"\$@"/m)?.[1];
  if (delegatedLauncher) return resolvePackageRoot(delegatedLauncher, visited);

  let current = resolvedBinary;
  for (;;) {
    if (path.basename(current) === "openclaw") {
      try {
        await stat(path.join(current, "package.json"));
        return current;
      } catch {
        // Keep walking toward the filesystem root.
      }
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  throw new Error(`Could not resolve the OpenClaw package root from ${resolvedBinary}`);
}

function readCliVersion(binary) {
  const output = execFileSync(binary, ["--version"], { encoding: "utf8" }).split("\n", 1)[0];
  const match = output.match(/(?:^|\s)(\d+\.\d+\.\d+(?:[-+][^\s]+)?)(?:\s|$)/);
  if (!match) throw new Error(`Could not parse the OpenClaw CLI version from: ${output || "<empty>"}`);
  return match[1];
}

async function atomicWrite(filePath, content, mode) {
  const tempPath = `${filePath}.g2-openclaw-${process.pid}.tmp`;
  await writeFile(tempPath, content, { encoding: "utf8", mode });
  await chmod(tempPath, mode);
  try {
    await rename(tempPath, filePath);
  } catch (error) {
    await unlink(tempPath).catch(() => {});
    throw error;
  }
}

async function main() {
  const binary = resolveOpenClawBinary();
  const cliVersion = readCliVersion(binary);
  if (cliVersion !== REQUIRED_OPENCLAW_VERSION) {
    throw new Error(
      `OpenClaw CLI ${cliVersion} is unsupported; this guarded runtime patch requires exactly ${REQUIRED_OPENCLAW_VERSION}`,
    );
  }

  const packageRoot = await resolvePackageRoot(binary);
  const packageJson = JSON.parse(await readFile(path.join(packageRoot, "package.json"), "utf8"));
  if (packageJson.version !== REQUIRED_OPENCLAW_VERSION) {
    throw new Error(
      `resolved OpenClaw package version ${packageJson.version} does not match CLI ${REQUIRED_OPENCLAW_VERSION}`,
    );
  }

  const distDir = path.join(packageRoot, "dist");
  const compactionFiles = (await readdir(distDir))
    .filter((name) => /^cli-compaction-[^/]+\.js$/.test(name))
    .map((name) => path.join(distDir, name));
  if (compactionFiles.length !== 1) {
    throw new Error(`expected exactly one OpenClaw CLI compaction bundle, found ${compactionFiles.length}`);
  }
  const compactionFile = compactionFiles[0];
  const original = await readFile(compactionFile, "utf8");
  const patchedCount = countOccurrences(original, PATCHED_BRANCH);
  const vulnerableCount = countMatches(original, VULNERABLE_BRANCH);
  if (patchedCount === 1 && vulnerableCount === 0) {
    console.log(
      JSON.stringify({
        status: "verified",
        openclaw: REQUIRED_OPENCLAW_VERSION,
        packageRoot,
        compactionFile,
        patch: PATCH_MARKER,
        sha256: createHash("sha256").update(original).digest("hex"),
      }),
    );
    return;
  }
  if (vulnerableCount !== 1 || patchedCount !== 0) {
    throw new Error(
      `OpenClaw CLI compaction bundle does not contain exactly one recognized Codex fallback branch; refusing to modify ${compactionFile}`,
    );
  }

  const replacement = `if(isIntentionalNativeAutoCompactionSkip(result))return{compacted:false};/* ${PATCH_MARKER} */`;
  const patched = original.replace(VULNERABLE_BRANCH, replacement);
  const mode = (await stat(compactionFile)).mode & 0o777;
  await atomicWrite(compactionFile, patched, mode);
  const verified = await readFile(compactionFile, "utf8");
  if (countOccurrences(verified, PATCHED_BRANCH) !== 1 || countMatches(verified, VULNERABLE_BRANCH) !== 0) {
    throw new Error(`post-write verification failed for ${compactionFile}`);
  }
  console.log(
    JSON.stringify({
      status: "patched",
      openclaw: REQUIRED_OPENCLAW_VERSION,
      packageRoot,
      compactionFile,
      patch: PATCH_MARKER,
      sha256: createHash("sha256").update(patched).digest("hex"),
    }),
  );
}

try {
  await main();
} catch (error) {
  fail(error instanceof Error ? error.message : String(error));
}
