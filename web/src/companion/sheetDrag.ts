/*
 * Pull a bottom sheet down to close it, as on iOS: hold its top (the handle or the title bar) and
 * pull, or pull its content when that is already scrolled to the top. Let go past a short distance
 * (or with a flick) and it slides away and closes; otherwise it springs back.
 *
 * Touch is followed everywhere in the sheet (on a phone the whole sheet is the grip); a mouse only
 * from the top bar, marked ``data-sheet-grip``. Anything inside ``data-no-sheet-drag`` (a map, a
 * browser view, a horizontal strip) keeps its own gestures.
 *
 * ``direction: "left"`` is the same for a panel that slides in from the left (the side menu):
 * swiped back left, it closes.
 */
import { useEffect, useRef, type RefObject } from "react";

const CLOSE_PX = 110;
const FLICK = 0.6; // px per ms

/** Some element between ``target`` and the sheet that is scrolled down: the pull scrolls it instead. */
function scrolledAbove(target: Element | null, panel: HTMLElement): boolean {
  for (let el = target; el && el !== panel; el = el.parentElement) {
    if (el.scrollTop > 0 && el.scrollHeight > el.clientHeight) return true;
  }
  return false;
}

function blocked(target: EventTarget | null): boolean {
  const el = target instanceof Element ? target : null;
  return !!el?.closest("[data-no-sheet-drag], input[type=range], textarea, select");
}

export function useSheetDrag(panel: RefObject<HTMLElement | null>, onClose: () => void, enabled = true,
                             direction: "down" | "left" = "down") {
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const node = panel.current;
    if (!enabled || !node) return;
    let startY = 0;
    let startX = 0;
    let startT = 0;
    let dy = 0;
    // null: not decided yet; true: this gesture moves the sheet; false: it is someone else's.
    let dragging: boolean | null = false;

    const left = direction === "left";
    // The finger's position along the closing direction (down, or leftwards).
    const along = (t: { clientX: number; clientY: number }) => (left ? -t.clientX : t.clientY);
    const across = (t: { clientX: number; clientY: number }) => (left ? t.clientY : t.clientX);
    const shift = (px: number | string) => (left ? `translateX(-${px})` : `translateY(${px})`);
    const move = (at: number) => {
      dy = Math.max(0, at - startY);
      node.style.animation = "none"; // an entry animation's fill would hold the transform
      node.style.transition = "none";
      node.style.transform = shift(`${dy}px`);
    };
    const release = () => {
      const fast = dy / Math.max(1, performance.now() - startT) > FLICK && dy > 30;
      node.style.transition = "transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1)";
      if (dy > (left ? 80 : CLOSE_PX) || fast) {
        node.style.transform = shift("110%");
        window.setTimeout(() => {
          close.current();
          // A panel that stays mounted (the side menu) slides by its own style from here on.
          node.style.transition = "";
          node.style.transform = "";
        }, 200);
      } else {
        node.style.transform = "";
      }
      dragging = false;
      dy = 0;
    };

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1 || blocked(e.target)) {
        dragging = false;
        return;
      }
      const target = e.target instanceof Element ? e.target : null;
      dragging = !left && scrolledAbove(target, node) ? false : null;
      startY = along(e.touches[0]);
      startX = across(e.touches[0]);
      startT = performance.now();
      dy = 0;
    };
    const onTouchMove = (e: TouchEvent) => {
      if (dragging === false) return;
      const y = along(e.touches[0]);
      if (dragging === null) {
        const down = y - startY;
        const side = Math.abs(across(e.touches[0]) - startX);
        if (Math.abs(down) < 8 && side < 8) return;
        // Only a pull straight down, from content at its top, is the sheet's.
        dragging = down > 0 && down > side;
        if (!dragging) return;
        startY = y;
        startT = performance.now();
      }
      e.preventDefault(); // the page mustn't scroll (or bounce) under the finger as well
      move(y);
    };
    const onTouchEnd = () => {
      if (dragging) release();
      dragging = false;
    };

    // A mouse pulls from the grip only (a click elsewhere selects text, presses buttons…).
    const onMouseDown = (e: MouseEvent) => {
      if (e.button !== 0 || !(e.target instanceof Element) || !e.target.closest("[data-sheet-grip]")) return;
      if (e.target.closest("button, a, input")) return;
      startY = along(e);
      startT = performance.now();
      dy = 0;
      const onMove = (m: MouseEvent) => move(along(m));
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        if (dy > 0) release();
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    };

    node.addEventListener("touchstart", onTouchStart, { passive: true });
    node.addEventListener("touchmove", onTouchMove, { passive: false });
    node.addEventListener("touchend", onTouchEnd);
    node.addEventListener("touchcancel", onTouchEnd);
    node.addEventListener("mousedown", onMouseDown);
    return () => {
      node.removeEventListener("touchstart", onTouchStart);
      node.removeEventListener("touchmove", onTouchMove);
      node.removeEventListener("touchend", onTouchEnd);
      node.removeEventListener("touchcancel", onTouchEnd);
      node.removeEventListener("mousedown", onMouseDown);
    };
  }, [panel, enabled, direction]);
}
