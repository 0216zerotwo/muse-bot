import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ManageConnectionSheet } from "@/companion/ManageConnectionSheet";
import type { McpPresetInfo, McpPresetsPayload } from "@/lib/types";

const { requestMutation } = vi.hoisted(() => ({ requestMutation: vi.fn() }));

vi.mock("@/providers/ClientProvider", () => {
  const client = { requestMutation };
  return { useClient: () => ({ client }) };
});

const TOOLS = [
  "mcp_icloud-calendar_list_calendars",
  "mcp_icloud-calendar_list_events",
  "mcp_icloud-calendar_create_event",
  "mcp_icloud-calendar_delete_event",
];

const calendar = {
  name: "icloud-calendar",
  display_name: "iCloud 日历",
  description: "查看、添加和修改日程。",
  enabled: true,
  enabled_tools: ["*"],
  tool_names: TOOLS,
  required_fields: [],
} as unknown as McpPresetInfo;

const ok: McpPresetsPayload = { presets: [], installed_count: 1, last_action: { ok: true, message: "ok" } };

afterEach(() => {
  cleanup();
  requestMutation.mockReset();
});

describe("ManageConnectionSheet", () => {
  it("lists what it can do and limits it to looking when asked", async () => {
    requestMutation.mockResolvedValue(ok);
    render(<ManageConnectionSheet preset={calendar} onClose={() => {}} onChanged={() => {}} onEditCredentials={() => {}} />);

    expect(await screen.findByText("查看日历、查看日程")).toBeInTheDocument();
    expect(screen.getByText("添加日程、删除日程")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: "只允许查看" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith(
        "settings.mcp.tools",
        { name: "icloud-calendar", enabled_tools: TOOLS.slice(0, 2) },
        expect.any(Number),
      ),
    );
  });

  it("asks once more before disconnecting", async () => {
    requestMutation.mockResolvedValue(ok);
    const onClose = vi.fn();
    render(<ManageConnectionSheet preset={calendar} onClose={onClose} onChanged={() => {}} onEditCredentials={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "断开连接" }));
    expect(requestMutation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /确定断开/ }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(requestMutation).toHaveBeenCalledWith("settings.mcp.remove", { name: "icloud-calendar" }, expect.any(Number));
  });
});
