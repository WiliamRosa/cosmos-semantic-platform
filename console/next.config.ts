import type { NextConfig } from "next";

// A static export served by `cosmos serve` at /console (ADR 0012): no Node server in
// production, one `pip install` as before.
const config: NextConfig = {
  output: "export",
  basePath: "/console",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default config;
