import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApprovalCard } from "@/companion/ApprovalCard";
import type { ApprovalRecord } from "@/companion/companion-api";

afterEach(cleanup);

function record(partial: Partial<ApprovalRecord>): ApprovalRecord {
  return {
    id: "apv",
    session_key: "websocket:c",
    call_id: "call",
    tool_name: "mcp_email_send_email",
    arguments: {},
    status: "pending",
    created_at_ms: 0,
    decided_at_ms: null,
    result: null,
    ...partial,
  };
}

describe("ApprovalCard", () => {
  it("offers this once, each standing yes for its target, and no, as stacked buttons", async () => {
    const onDecide = vi.fn(async () => {});
    render(
      <ApprovalCard
        approval={record({ action: "send_email", arguments: { recipients: ["a@x.com"] }, scopes: ["once", "24h", "always"] })}
        onDecide={onDecide}
      />,
    );
    const twentyFourBtn = screen.getByRole("button", { name: "24 小时内「发给 a@x.com」都允许" });
    expect(screen.getByRole("button", { name: "发送，仅这次" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "「发给 a@x.com」一直允许" })).toBeInTheDocument();
    fireEvent.click(twentyFourBtn);
    await waitFor(() => {
      expect(onDecide).toHaveBeenCalledWith(expect.objectContaining({ id: "apv" }), "approve", "24h");
      expect(twentyFourBtn).not.toBeDisabled();
    });
  });

  it("says a step in the browser in words, without the script or the address", () => {
    render(
      <ApprovalCard
        approval={record({
          tool_name: "browser_exec",
          action: "browser_submit",
          arguments: { code: "goto_url('https://www.apple.com.cn/shop/buy-mac')", element: "立即购买" },
          context: { browser: { host: "apple.com.cn", page: "购买 MacBook Air", target: "立即购买", paying: true,
            summary: "要在 apple.com.cn「购买 MacBook Air」页面上点「立即购买」。点了可能就下单或付款了，看清楚是不是你要的再确认。" } },
        })}
        onDecide={vi.fn(async () => {})}
      />,
    );
    expect(screen.getByText(/页面上点「立即购买」/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "付款或下单，等你确认" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认付款" })).toBeInTheDocument();
    expect(screen.queryByText("立即购买")).not.toBeInTheDocument();
    expect(screen.queryByText(/goto_url/)).not.toBeInTheDocument();
  });

  it("names the site private data would go to, and shows what would be sent", () => {
    render(
      <ApprovalCard
        approval={record({
          tool_name: "mcp_browser_browser_type",
          action: "send_private",
          scopes: ["once", "24h", "always"],
          context: { egress: { host: "forms.example.com", what: "张三 13800000000" } },
        })}
        onDecide={vi.fn(async () => {})}
      />,
    );
    expect(screen.getByText("forms.example.com")).toBeInTheDocument();
    expect(screen.getByText("张三 13800000000")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "「发到 forms.example.com」一直允许" })).toBeInTheDocument();
  });

  it("normalizes `to` comma-separated recipients and numeric `email_ids`", () => {
    const { unmount } = render(
      <ApprovalCard
        approval={record({
          tool_name: "mcp__email__send_email",
          action: "send_email",
          arguments: { to: "alice@example.com, bob@example.com", subject: "周报" },
          scopes: ["once", "24h"],
        })}
        onDecide={vi.fn(async () => {})}
      />,
    );
    expect(screen.getByText("alice@example.com、bob@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "24 小时内「发给 alice@example.com、bob@example.com」都允许" })).toBeInTheDocument();
    unmount();

    render(
      <ApprovalCard
        approval={record({
          tool_name: "mcp__email__delete_emails",
          action: "delete_emails",
          arguments: { email_ids: [101, 102, 103] },
        })}
        onDecide={vi.fn(async () => {})}
      />,
    );
    expect(screen.getByText("3 封")).toBeInTheDocument();
  });
});
