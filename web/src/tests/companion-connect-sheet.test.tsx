import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectSheet } from "@/companion/ConnectSheet";
import type { McpPresetInfo, McpPresetsPayload } from "@/lib/types";

const { requestMutation } = vi.hoisted(() => ({ requestMutation: vi.fn() }));

vi.mock("@/providers/ClientProvider", () => {
  const client = { requestMutation };
  return { useClient: () => ({ client }) };
});

const calendar = {
  name: "icloud-calendar",
  display_name: "iCloud 日历",
  description: "查看、添加和修改日程。",
  note: "",
  docs_url: "",
  required_fields: [
    { name: "icloud_username", label: "Apple 账户", secret: false, required: true, configured: false },
    { name: "icloud_password", label: "App 专用密码", secret: true, required: true, configured: false },
    { name: "caldav_server_url", label: "CalDAV 服务器", secret: false, required: false, configured: false },
  ],
} as unknown as McpPresetInfo;

function payload(extra: Partial<McpPresetsPayload> = {}): McpPresetsPayload {
  return { presets: [], installed_count: 1, ...extra };
}

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

afterEach(() => {
  cleanup();
  requestMutation.mockReset();
});

describe("ConnectSheet", () => {
  it("enables the service with the entered credentials once the required ones are filled", async () => {
    requestMutation.mockResolvedValue(payload({ last_action: { ok: true, message: "ok" } }));
    const onClose = vi.fn();
    render(<ConnectSheet preset={calendar} onClose={onClose} onChanged={() => {}} />);

    const submit = screen.getByRole("button", { name: "连接" });
    fill("Apple 账户", " me@icloud.com ");
    expect(submit).toBeDisabled();
    fill("App 专用密码", "abcd-efgh-ijkl-mnop");
    fireEvent.click(submit);

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(requestMutation).toHaveBeenCalledWith(
      "settings.mcp.enable",
      { name: "icloud-calendar", icloud_username: "me@icloud.com", icloud_password: "abcd-efgh-ijkl-mnop" },
      expect.any(Number),
    );
  });

  it("stays open and explains why when the service could not start", async () => {
    requestMutation.mockResolvedValue(payload({ hot_reload: { ok: false, message: "认证失败" } }));
    const onClose = vi.fn();
    render(<ConnectSheet preset={calendar} onClose={onClose} onChanged={() => {}} />);

    fill("Apple 账户", "me@icloud.com");
    fill("App 专用密码", "wrong");
    fireEvent.click(screen.getByRole("button", { name: "连接" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("认证失败");
    expect(onClose).not.toHaveBeenCalled();
  });
});
