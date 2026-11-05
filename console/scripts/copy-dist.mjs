// Copies the static export (out/) into the Python package, where `cosmos serve` mounts it at
// /console and the wheel ships it.
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "out");
const target = join(here, "..", "..", "src", "cosmos_semantic", "console_dist");

if (!existsSync(join(source, "index.html"))) {
  console.error(`no static export at ${source}; run \`next build\` first`);
  process.exit(1);
}
rmSync(target, { recursive: true, force: true });
cpSync(source, target, { recursive: true });
console.log(`console copied to ${target}`);
