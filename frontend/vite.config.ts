import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

export default defineConfig(({ mode }) => ({
  plugins: [svelte()],
  // DOM component tests need Svelte's browser lifecycle, not the SSR entry point.
  ...(mode === "test" ? { resolve: { conditions: ["browser"] } } : {}),
  build: {
    lib: {
      entry: "src/main.ts",
      formats: ["es"],
      fileName: "flamingo-chat",
    },
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
}));
