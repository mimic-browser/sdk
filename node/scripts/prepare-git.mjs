import { spawnSync } from "node:child_process";
import { copyFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { checkGitPackage } from "./git-package.mjs";

// npm installs build dependencies before preparing a Git dependency. This hook
// compiles source only; runtime acquisition remains an explicit SDK operation.
const root = fileURLToPath(new URL("../../", import.meta.url));
await checkGitPackage();
const require = createRequire(import.meta.url);
const result = spawnSync(
  process.execPath,
  [
    require.resolve("typescript/bin/tsc"),
    "--project",
    resolve(root, "node/tsconfig.json"),
    "--outDir",
    resolve(root, "dist"),
  ],
  { cwd: root, stdio: "inherit" },
);
if (result.error) throw result.error;
if (result.status !== 0)
  throw new Error(
    `Node SDK compilation failed (${result.status ?? result.signal})`,
  );
await mkdir(resolve(root, "dist"), { recursive: true });
await copyFile(
  resolve(root, "node/runtime-lock.json"),
  resolve(root, "dist/runtime-lock.json"),
);
