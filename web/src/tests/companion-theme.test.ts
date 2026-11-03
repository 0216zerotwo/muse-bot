import { describe, expect, it } from "vitest";

import { contrast, DEFAULT_ACCENT, themeFromAccent } from "@/companion/theme";

describe("themeFromAccent", () => {
  it.each([DEFAULT_ACCENT, "#93b8ef", "#e6cdb0", "#2fbf71", "#d4537e", "#ffe600"])("keeps text readable for %s", (accent) => {
    const { light, dark } = themeFromAccent(accent);
    for (const tokens of [light, dark]) {
      expect(contrast(tokens["--c-primary"], tokens["--c-primary-text"])).toBeGreaterThanOrEqual(4.5);
      expect(contrast(tokens["--c-text"], tokens["--c-bg"])).toBeGreaterThanOrEqual(7);
      expect(contrast(tokens["--c-text-muted"], tokens["--c-bg"])).toBeGreaterThanOrEqual(4.5);
      expect(contrast(tokens["--c-bubble-user"], tokens["--c-bubble-user-text"])).toBeGreaterThanOrEqual(4.5);
      expect(contrast(tokens["--c-text"], tokens["--c-bubble-agent"])).toBeGreaterThanOrEqual(7);
    }
  });

  it("gives the orange cat an ivory page and amber buttons", () => {
    const { light } = themeFromAccent(DEFAULT_ACCENT);
    expect(light["--c-bg"]).toMatch(/^#f[5-9a-f]f[0-9a-f]e[0-9a-f]$/);
    expect(contrast(light["--c-primary"], "#ffffff")).toBeGreaterThanOrEqual(4.5);
  });
});
