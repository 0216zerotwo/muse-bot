import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelsPayload } from "@/companion/companion-api";
import { modelsShort, routinesShort, SettingsScreen } from "@/companion/Settings";

vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation: vi.fn() }, token: "t" }) }));

vi.mock("@/companion/companion-api", async (original) => ({
  ...(await original<typeof import("@/companion/companion-api")>()),
  fetchCompanionState: vi.fn(async () => ({
    name: "伙伴",
    avatar: "mochi:cream",
    chat_id: "c1",
    tone: "warm" as const,
    user_name: null,
    style_note: null,
    onboarded: true,
    timezone: null,
    updated_at: null,
  })),
  fetchRoutines: vi.fn(async () => ({ brief_time: "08:00", checkins: true })),
  fetchMemory: vi.fn(async () => ({ sections: [] })),
  fetchSkills: vi.fn(async () => ({ learned: [], bundled: [] })),
  fetchModels: vi.fn(async () => {
    throw new Error("offline");
  }),
  fetchUsage: vi.fn(async () => {
    throw new Error("offline");
  }),
}));

vi.mock("@/companion/device", async (original) => ({
  ...(await original<typeof import("@/companion/device")>()),
  fetchDevices: vi.fn(async () => []),
}));

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchMcpPresets: vi.fn(async () => ({ presets: [], installed_count: 0 })),
}));

function open(start: "root" | "devices" = "root") {
  const onClose = vi.fn();
  render(<SettingsScreen start={start} onClose={onClose} onLogout={() => {}} onDraft={() => {}} onTeach={() => {}} onAppearance={() => {}} />);
  return onClose;
}

const title = () => screen.getByRole("heading", { level: 1 }).textContent;

afterEach(cleanup);

describe("SettingsScreen", () => {
  it("opens pages with a back button that returns to 设置, then to the chat", async () => {
    const onClose = open();
    expect(title()).toBe("设置");
    await act(async () => {});

    fireEvent.click(await screen.findByRole("button", { name: /设备/ }));
    expect(title()).toBe("设备");
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "返回设置" }));
    expect(title()).toBe("设置");
    await act(async () => {});

    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("opened on a page directly, back leads to 设置; Escape steps back too", async () => {
    const onClose = open("devices");
    expect(title()).toBe("设备");
    await act(async () => {});
    fireEvent.keyDown(document, { key: "Escape" });
    expect(title()).toBe("设置");
    await act(async () => {});
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});

describe("row values", () => {
  const models = (settings: Partial<ModelsPayload["settings"]>): ModelsPayload => ({
    presets: [],
    max_tool_iterations: 200,
    default: "flash",
    fallbacks: [],
    settings: { enabled: false, router: null, fixed: null, models: {}, ...settings },
    health: {},
    decisions: [],
    media: { image: { active: null, models: [] }, video: { active: null, models: [] } },
  });

  it("say the state in a word or two", () => {
    expect(routinesShort({ brief_time: null, checkins: false })).toBe("关闭");
    expect(routinesShort({ brief_time: null, checkins: true })).toBe("关心");
    expect([modelsShort(models({ enabled: true })), modelsShort(models({ fixed: "pro" })), modelsShort(models({}))]).toEqual([
      "自动",
      "pro",
      "flash",
    ]);
  });
});
