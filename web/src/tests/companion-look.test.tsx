import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CompanionState, CustomAvatar } from "@/companion/companion-api";
import { EmojiIcon } from "@/companion/EmojiIcon";
import { iconFor } from "@/companion/icons";
import { NameLookRow } from "@/companion/muse/Identity";
import { PetSourceView, resolvePet } from "@/companion/Pet";

vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: {}, token: "t" }) }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the app's drawn icons", () => {
  it("draws the emoji it has a drawing for, and leaves the others as emoji", () => {
    expect(iconFor("🧭")).toBe("/icons/compass.webp");
    expect(iconFor("✍️")).toBe(iconFor("✍"));
    expect(iconFor("🦄")).toBeNull();
    const { container, rerender } = render(<EmojiIcon emoji="📰" size={40} />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/icons/news.webp");
    rerender(<EmojiIcon emoji="🦄" size={40} />);
    expect(container.textContent).toBe("🦄");
  });
});

describe("a look drawn from a description", () => {
  const drawn: CustomAvatar = {
    id: "abc123abc123", kind: "image", name: "小企鹅", created_at: "", image_url: "/a/image.webp",
    sheet_url: null, actions_url: null, character: true, working_url: "/a/working.webp", happy_url: "/a/happy.webp",
  };

  it("is shown whole, busy while it works and happy when pleased", () => {
    const source = resolvePet("image:abc123abc123", [drawn]);
    const src = (animation: "idle" | "typing" | "jumping") => {
      const { container, unmount } = render(<PetSourceView source={source} animation={animation} size={64} />);
      const img = container.querySelector("img");
      const out = { src: img?.getAttribute("src"), round: img?.className.includes("rounded-full") };
      unmount();
      return out;
    };
    expect(src("idle")).toEqual({ src: "/a/image.webp", round: false });
    expect(src("typing").src).toBe("/a/working.webp");
    expect(src("jumping").src).toBe("/a/happy.webp");
  });
});

describe("a look in motion", () => {
  const moving: CustomAvatar = {
    id: "abc123abc124", kind: "image", name: "小鲸鱼", created_at: "", image_url: "/m/image.webp",
    sheet_url: null, actions_url: null, character: true, working_url: "/m/working.webp", happy_url: "/m/happy.webp",
    motions: { idle: "/m/motion_idle.webp", thinking: "/m/motion_thinking.webp", working: "/m/motion_working.webp", waiting: "/m/motion_waiting.webp" },
  };

  it("shows the still until the clip has downloaded, then the clip; the others load meanwhile", async () => {
    const requested: string[] = [];
    const loads: Array<() => void> = [];
    class FakeImage {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(url: string) {
        requested.push(url);
        loads.push(() => this.onload?.());
      }
    }
    vi.stubGlobal("Image", FakeImage);
    try {
      const source = resolvePet("image:abc123abc199", [{ ...moving, id: "abc123abc199", motions: { idle: "/s/idle.webp", working: "/s/working.webp" } }]);
      const { container } = render(<PetSourceView source={source} animation="idle" size={64} />);
      expect(container.querySelector("img")?.getAttribute("src")).toBe("/m/image.webp");
      await act(async () => loads.shift()?.());
      expect(container.querySelector("img")?.getAttribute("src")).toBe("/s/idle.webp");
      expect(requested).toEqual(["/s/idle.webp", "/s/working.webp"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("plays the clip for what it is doing, with no extra sway", async () => {
    // Every clip here has downloaded already.
    vi.stubGlobal("Image", class {
      onload: (() => void) | null = null;
      set src(_: string) {
        queueMicrotask(() => this.onload?.());
      }
    });
    const source = resolvePet("image:abc123abc124", [moving]);
    const shown = async (animation: "idle" | "review" | "typing" | "waiting" | "jumping") => {
      const { container, unmount } = render(<PetSourceView source={source} animation={animation} size={64} />);
      await act(async () => undefined);
      const out = { src: container.querySelector("img")?.getAttribute("src"), sways: !!container.querySelector("[class*='c-pet-']") };
      unmount();
      return out;
    };
    expect(await shown("idle")).toEqual({ src: "/m/motion_idle.webp", sways: false });
    expect((await shown("review")).src).toBe("/m/motion_thinking.webp");
    expect((await shown("typing")).src).toBe("/m/motion_working.webp");
    expect((await shown("waiting")).src).toBe("/m/motion_waiting.webp");
    expect((await shown("jumping")).src).toBe("/m/happy.webp"); // no clip for a cheer: the happy still
  });
});

describe("换形象", () => {
  it("hands over to the chat instead of opening a picker", () => {
    const onChangeLook = vi.fn();
    render(<NameLookRow companion={{ name: "Adam" } as CompanionState} onUpdate={vi.fn()} onChangeLook={onChangeLook} />);
    fireEvent.click(screen.getByRole("button", { name: "换形象" }));
    expect(onChangeLook).toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("opening the app", () => {
  afterEach(() => localStorage.clear());

  it("shows nothing for a drawn look still loading, never the default cat", () => {
    const { container } = render(<PetSourceView source={resolvePet("image:abc123abc199", null)} size={64} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("canvas, [style*='spritesheet']")).toBeNull();
  });

  it("shows the look this phone remembers until the list arrives", () => {
    localStorage.setItem("companion.look", JSON.stringify({ id: "abc123abc198", kind: "image", name: "小鲸", created_at: "", image_url: "/r/image.webp", sheet_url: null, actions_url: null, character: true }));
    const { container } = render(<PetSourceView source={resolvePet("image:abc123abc198", null)} size={64} />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/r/image.webp");
  });

  it("falls back to the default only for a look that is gone", () => {
    expect(resolvePet("image:gone00000000", []).type).toBe("sprite");
  });
});
