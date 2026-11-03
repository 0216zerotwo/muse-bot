import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "@/companion/Composer";
import { readsAfterVoice, setReadsAfterVoice } from "@/companion/MessageActions";

vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ token: "t", client: {} }) }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function composer() {
  return render(
    <Composer value="" onChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} streaming={false} connected transcribeAudio={vi.fn()} onFocusChange={vi.fn()} />,
  );
}

describe("＋ in the Android app", () => {
  it("opens the app's own sheet, and what it picked arrives as attachments", async () => {
    const attach = vi.fn();
    vi.stubGlobal("PersonalBotAndroid", { changeServer: vi.fn(), attach });
    const fetcher = vi.fn(async () => new Response(new Blob(["%PDF-1.4"], { type: "application/pdf" })));
    vi.stubGlobal("fetch", fetcher);
    composer();
    fireEvent.click(screen.getByRole("button", { name: "添加文件" }));
    expect(attach).toHaveBeenCalled();

    act(() => {
      window.dispatchEvent(new CustomEvent("companion:attach", {
        detail: [{ url: "/__android/picked/abc", name: "周末计划.pdf", type: "application/pdf" }],
      }));
    });
    expect(fetcher).toHaveBeenCalledWith("/__android/picked/abc");
    await waitFor(() => expect(screen.getByText("周末计划.pdf")).toBeInTheDocument());
  });

  it("is the file input in a browser", () => {
    composer();
    const input = document.querySelector<HTMLInputElement>("input[type=file]");
    const click = vi.spyOn(input as HTMLInputElement, "click");
    fireEvent.click(screen.getByRole("button", { name: "添加文件" }));
    expect(click).toHaveBeenCalled();
  });
});

describe("语音输入后朗读回复", () => {
  it("is on unless turned off, and a typed message is not one said aloud", () => {
    expect(readsAfterVoice()).toBe(true);
    setReadsAfterVoice(false);
    expect(readsAfterVoice()).toBe(false);
    setReadsAfterVoice(true);
    const onSend = vi.fn();
    render(<Composer value="明天下雨吗" onChange={vi.fn()} onSend={onSend} onStop={vi.fn()} streaming={false} connected transcribeAudio={vi.fn()} onFocusChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /发送/ }));
    expect(onSend).toHaveBeenCalledWith("明天下雨吗", [], false);
  });
});
