import { copyFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(root, "node_modules", "opus-recorder", "dist");
const target = join(root, "public", "assets");
await mkdir(target, { recursive: true });

for (const name of [
  "encoderWorker.min.js",
  "encoderWorker.min.wasm",
  "decoderWorker.min.js",
  "decoderWorker.min.wasm",
]) {
  await copyFile(join(source, name), join(target, name));
}
