import type { NextConfig } from "next";

// A static export served by `cosmos serve` at /console (ADR 0012): no Node server in
// production, one `pip install` as before.
// The public demo (GitHub Pages) is built with another base path and NEXT_PUBLIC_COSMOS_DEMO=1.
const config: NextConfig = {
  output: "export",
  basePath: process.env.NEXT_PUBLIC_BASE_PATH ?? "/console",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default config;
