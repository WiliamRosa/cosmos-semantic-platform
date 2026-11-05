// Static hosts (GitHub Pages) for the demo: Next.js prefetches route segments as
// `__next.<route>.<segment>.txt` while the static export writes `__next.<route>/<segment>.txt`.
// `cosmos serve` maps the names (api._console_files); here a copy under the dotted name does.
import { copyFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const root = process.argv[2] ?? "out";
let copied = 0;

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (!statSync(path).isDirectory()) continue;
    if (name.startsWith("__next.")) {
      for (const file of readdirSync(path)) {
        if (file.endsWith(".txt")) {
          copyFileSync(join(path, file), join(dir, `${name}.${file}`));
          copied += 1;
        }
      }
    }
    walk(path);
  }
}

walk(root);
console.log(`${copied} segment files flattened in ${root}`);
