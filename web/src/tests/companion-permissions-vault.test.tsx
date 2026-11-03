import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CredentialCard } from "@/companion/CredentialCard";
import { PermissionLevels } from "@/companion/PermissionLevels";
import { parseCard } from "@/companion/replyCards";

const requestMutation = vi.fn();
const session = { client: { requestMutation }, token: "t" };
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => session }));

const kinds = [
  { key: "browse", label: "打开网页", detail: "看网页内容", level: "allow", levels: ["deny", "ask", "allow"] },
  { key: "payment", label: "付款", detail: "任何花钱的操作", level: "ask", levels: ["deny", "ask"] },
];
vi.mock("@/companion/companion-api", async (original) => ({
  ...(await original<typeof import("@/companion/companion-api")>()),
  fetchPermissions: vi.fn(async () => ({ kinds })),
}));

beforeEach(() => requestMutation.mockReset());
afterEach(cleanup);

describe("设置 → 权限", () => {
  it("shows each kind's level in a dropdown, paying without 允许, and changes one", async () => {
    requestMutation.mockResolvedValue({ kinds: [{ ...kinds[0], level: "ask" }, kinds[1]] });
    render(<PermissionLevels />);
    fireEvent.click(await screen.findByRole("button", { name: "付款：询问我" }));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["禁止", "询问我"]);
    fireEvent.pointerDown(document.body); // a tap elsewhere puts the menu away
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "打开网页：允许" }));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["禁止", "询问我", "允许"]);
    await act(async () => fireEvent.click(screen.getByRole("option", { name: "询问我" })));
    await vi.waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.permissions.set", { kind: "browse", level: "ask" }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    // The page shows the level the server saved.
    expect(await screen.findByRole("button", { name: "打开网页：询问我" })).toBeInTheDocument();
    expect(screen.queryByText(/不用再问/)).not.toBeInTheDocument();
  });
});

describe("credential card", () => {
  it("parses only well-formed cards", () => {
    expect(parseCard({ type: "credential", kind: "api_key", service: "GitHub", env: "GITHUB_TOKEN" })).toMatchObject({ kind: "api_key", env: "GITHUB_TOKEN" });
    expect(parseCard({ type: "credential", kind: "api_key", service: "GitHub", env: "lower" })).toBeNull();
    expect(parseCard({ type: "credential", kind: "login", service: "12306", origin: "https://12306.cn" })).toMatchObject({ kind: "login" });
  });

  it("saves a login straight to the vault and tells the companion without the password", async () => {
    requestMutation.mockResolvedValue({ logins: [], keys: [] });
    const send = vi.fn();
    render(<CredentialCard card={{ type: "credential", kind: "login", service: "GitHub", origin: "github.com" }} send={send} />);
    fireEvent.change(screen.getByRole("textbox", { name: "账号" }), { target: { value: "me@example.com" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "s3cret" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "加密保存" })));
    await vi.waitFor(() => expect(send).toHaveBeenCalled());
    expect(requestMutation).toHaveBeenCalledWith("companion.vault.change", expect.objectContaining({ action: "add_login", origin: "github.com", password: "s3cret" }));
    expect(send.mock.calls[0][0]).not.toContain("s3cret");
    expect(await screen.findByText("已加密保存")).toBeInTheDocument();
  });
});
