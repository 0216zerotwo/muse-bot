import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CompanionState } from "@/companion/companion-api";
import { Onboarding } from "@/companion/Onboarding";

const { requestMutation } = vi.hoisted(() => ({ requestMutation: vi.fn() }));

vi.mock("@/providers/ClientProvider", () => {
  const client = { requestMutation };
  return { useClient: () => ({ client, token: "t" }) };
});

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchMcpPresets: vi.fn(async () => ({ presets: [], installed_count: 0 })),
}));

const companion: CompanionState = {
  name: "伙伴",
  avatar: "mochi:cream",
  chat_id: "c1",
  tone: "warm",
  user_name: null,
  style_note: null,
  onboarded: false,
  timezone: null,
  updated_at: null,
};

function respond() {
  requestMutation.mockImplementation(async (action: string, payload: { changes: Record<string, unknown> }) =>
    action === "companion.routines.update"
      ? { brief_time: null, checkins: false, ...payload.changes }
      : { ...companion, ...payload.changes },
  );
}

const calls = () => requestMutation.mock.calls.map(([action, payload]) => [action, payload.changes]);

afterEach(() => {
  cleanup();
  requestMutation.mockReset();
});

describe("Onboarding", () => {
  it("skipping only marks setup as done", async () => {
    respond();
    const onDone = vi.fn();
    render(<Onboarding companion={companion} onDone={onDone} />);

    fireEvent.click(screen.getByRole("button", { name: "跳过" }));

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    const [[action, changes]] = calls();
    expect(action).toBe("companion.update");
    expect(changes).toMatchObject({ onboarded: true });
    expect(changes).not.toHaveProperty("name");
  });

  it("saves the persona and the chosen routines on 完成", async () => {
    respond();
    const onDone = vi.fn();
    render(<Onboarding companion={companion} onDone={onDone} />);
    const next = () => fireEvent.click(screen.getByRole("button", { name: /开始|下一步|完成/ }));

    next();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "小糯" } });
    next();
    fireEvent.click(screen.getByRole("radio", { name: /简洁干脆/ }));
    next();
    fireEvent.click(screen.getByRole("switch", { name: "主动关心" }));
    next();
    await screen.findByText("完成");
    next();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(calls()).toContainEqual(["companion.routines.update", { brief_time: "08:00", checkins: false }]);
    expect(calls().at(-1)).toEqual([
      "companion.update",
      expect.objectContaining({ name: "小糯", tone: "concise", onboarded: true, avatar: "mochi:cream" }),
    ]);
  });
});
