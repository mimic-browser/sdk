import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import {
  cp,
  mkdtemp,
  readFile,
  rm,
  writeFile,
  mkdir,
  access,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { checkGitPackage } from "../scripts/git-package.mjs";

if (process.platform !== "linux")
  throw new Error("Git package qualification runs in WSL/Linux");
await checkGitPackage();
const root = fileURLToPath(new URL("../../", import.meta.url));
const pinnedRelease = JSON.parse(
  await readFile(join(root, "node/runtime-lock.json"), "utf8"),
).release;
const temporary = await mkdtemp(join(tmpdir(), "mimic-npm-git-"));
const source = join(temporary, "source");
const consumer = join(temporary, "consumer");
const runtimeDir = join(temporary, "runtime-cache");
const environment = { ...process.env, MIMIC_RUNTIME_DIR: runtimeDir };
delete environment.MIMIC_EXECUTABLE_PATH;
delete environment.MIMIC_RUNTIME_VERSION;
delete environment.MIMIC_DOWNLOAD;
delete environment.NODE_PATH;
const commands = [];
async function run(command, args, cwd, timeout = 180_000) {
  const child = spawn(command, args, {
    cwd,
    env: environment,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (bytes) => {
    output += bytes;
  });
  child.stderr.on("data", (bytes) => {
    output += bytes;
  });
  const timer = setTimeout(() => child.kill("SIGKILL"), timeout);
  const code = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  }).finally(() => clearTimeout(timer));
  commands.push({ command: [command, ...args], exitCode: code, output });
  assert.equal(code, 0, `${command} ${args.join(" ")}\n${output}`);
  return output.trim();
}
try {
  await mkdir(source);
  await mkdir(consumer);
  // Only source and immutable metadata enter the repository: no existing dist,
  // node_modules, Git state, runtime cache, or sibling workspace is available.
  for (const name of [
    "package.json",
    "package-lock.json",
    "LICENSE",
    "README.md",
    "node/package.json",
    "node/package-lock.json",
    "node/tsconfig.json",
    "node/runtime-lock.json",
    "node/src",
    "node/scripts",
  ]) {
    await mkdir(dirname(join(source, name)), { recursive: true });
    await cp(join(root, name), join(source, name), { recursive: true });
  }
  await assert.rejects(access(join(source, "dist")));
  await assert.rejects(access(join(source, "node_modules")));
  await assert.rejects(access(join(source, "node/node_modules")));
  await run("git", ["init", "--quiet"], source);
  await run("git", ["add", "."], source);
  await run(
    "git",
    [
      "-c",
      "user.name=Mimic package test",
      "-c",
      "user.email=sdk-test@example.invalid",
      "-c",
      "commit.gpgsign=false",
      "commit",
      "--quiet",
      "-m",
      "Temporary source fixture",
    ],
    source,
  );
  const commit = await run("git", ["rev-parse", "HEAD"], source);
  await writeFile(
    join(consumer, "package.json"),
    '{"private":true,"type":"module"}\n',
  );
  const specification = `git+${pathToFileURL(source).href}#${commit}`;
  await run(
    "npm",
    ["install", "--no-audit", "--no-fund", specification],
    consumer,
  );
  await assert.rejects(
    access(runtimeDir),
    "npm install must not acquire a runtime",
  );
  const installed = join(consumer, "node_modules/mimic-browser");
  await access(join(installed, "dist/index.js"));
  await access(join(installed, "dist/index.d.ts"));
  assert.equal(
    await readFile(join(installed, "dist/runtime-lock.json"), "utf8"),
    await readFile(join(root, "node/runtime-lock.json"), "utf8"),
  );
  await assert.rejects(
    access(join(installed, "node/src")),
    "packed Git dependency contains only built files",
  );
  await run(
    "node",
    [
      "--input-type=module",
      "-e",
      `
    import assert from 'node:assert/strict';
    import { createRequire } from 'node:module';
    import { RuntimeManager } from 'mimic-browser';
    const require = createRequire(import.meta.url);
    assert.equal(typeof RuntimeManager, 'function');
    for (const name of ['playwright-core', 'puppeteer-core']) {
      assert.throws(() => require.resolve(name), {code: 'MODULE_NOT_FOUND'});
    }
  `,
    ],
    consumer,
  );
  await assert.rejects(
    access(runtimeDir),
    "core-only install and import must remain inert",
  );
  await run(
    "npm",
    [
      "install",
      "--no-audit",
      "--no-fund",
      "playwright-core@1.63.0",
      "puppeteer-core@25.10.0",
    ],
    consumer,
  );
  await run(
    "node",
    [
      "--input-type=module",
      "-e",
      `
    import assert from 'node:assert/strict';
    import { RuntimeManager } from 'mimic-browser';
    import * as playwright from 'mimic-browser/playwright';
    import * as puppeteer from 'mimic-browser/puppeteer';
    assert.equal(typeof RuntimeManager, 'function');
    assert.equal(typeof playwright.launch, 'function');
    assert.equal(typeof puppeteer.launch, 'function');
  `,
    ],
    consumer,
  );
  await assert.rejects(
    access(runtimeDir),
    "importing SDK and adapters must not acquire a runtime",
  );
  await writeFile(
    join(consumer, "smoke.mjs"),
    `
    import assert from 'node:assert/strict';
    import { createServer } from 'node:http';
    import { RuntimeManager } from 'mimic-browser';
    import * as playwright from 'mimic-browser/playwright';
    import * as puppeteer from 'mimic-browser/puppeteer';
    const fixture = createServer((request, response) => response.end('<title>Git install</title><h1>working</h1>'));
    await new Promise(resolve => fixture.listen(0, '127.0.0.1', resolve));
    try {
      // Optional offline archive seeds the same verified cache. Neither adapter
      // receives an executable path; the default online path acquires on launch.
      if (process.env.MIMIC_TEST_ARCHIVE) await new RuntimeManager({allowDownload:false}).install({archivePath:process.env.MIMIC_TEST_ARCHIVE});
      for (const [name, adapter] of Object.entries({playwright, puppeteer})) {
        const session = await adapter.launch();
        const child = session.runtime.process;
        try {
          assert.equal((await session.mimic.getVersion()).version, ${JSON.stringify(pinnedRelease)});
          const context = await session.newContext();
          const page = await context.newPage();
          await page.goto('http://127.0.0.1:' + fixture.address().port);
          assert.equal(await page.title(), 'Git install');
          assert.equal(await page.evaluate(() => document.querySelector('h1').textContent), 'working');
        } finally { await session.close(); }
        assert.ok(child.exitCode !== null || child.signalCode !== null);
        console.log(name + ': native page + owned teardown PASS');
      }
    } finally { await new Promise(resolve => fixture.close(resolve)); }
  `,
  );
  await run("node", ["smoke.mjs"], consumer, 240_000);
  const receipt = {
    status: "pass",
    node: process.version,
    sourceCommit: commit,
    transport: "npm Git dependency via temporary local repository",
    packageName: "mimic-browser",
    frameworks:
      "optional; absent after SDK-only install, explicitly installed before adapter use",
    acquisition: process.env.MIMIC_TEST_ARCHIVE
      ? "verified offline archive before default launch"
      : "cold download on default launch",
    packageManifestSha256: createHash("sha256")
      .update(await readFile(join(installed, "package.json")))
      .digest("hex"),
    commands,
  };
  if (process.env.MIMIC_GIT_INSTALL_RECEIPT)
    await writeFile(
      process.env.MIMIC_GIT_INSTALL_RECEIPT,
      `${JSON.stringify(receipt, null, 2)}\n`,
    );
  console.log(
    `PASS Git source installation, side-effect-free import, Playwright/Puppeteer native flow, default runtime acquisition and teardown (${receipt.acquisition})`,
  );
} finally {
  await rm(temporary, { recursive: true, force: true });
}
