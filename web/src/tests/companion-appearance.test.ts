import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { adoptAppearance, petSize, setAppearanceSaver, setPetSize, setThemeMode, themeMode } from "@/companion/appearance";

describe("外观 kept on the server", () => {
  const saved: object[] = [];
  beforeEach(() => {
    localStorage.clear();
    saved.length = 0;
    setAppearanceSaver((changes) => saved.push(changes));
  });
  afterEach(() => setAppearanceSaver(null));

  it("comes back from the server when this phone has lost it", () => {
    adoptAppearance({ theme_mode: "dark", pet_size: "large" });
    expect([themeMode(), petSize()]).toEqual(["dark", "large"]);
    expect(saved).toEqual([]);
  });

  it("sends up a choice made here before the server kept it", () => {
    localStorage.setItem("nanobot-webui.theme", "light");
    adoptAppearance({ theme_mode: "system", pet_size: "medium" });
    expect(themeMode()).toBe("light");
    expect(saved).toEqual([{ theme_mode: "light" }]);
  });

  it("saves each change", () => {
    setThemeMode("dark");
    setPetSize("small");
    expect(saved).toEqual([{ theme_mode: "dark" }, { pet_size: "small" }]);
    vi.restoreAllMocks();
  });
});
