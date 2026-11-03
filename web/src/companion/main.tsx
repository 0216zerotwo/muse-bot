import ReactDOM from "react-dom/client";

import "../globals.css";
import "./companion.css";
import { initializeI18n } from "../i18n";
import { LOCALE_STORAGE_KEY } from "../i18n/config";
import { CompanionApp } from "./CompanionApp";

// Companion mode is a Chinese-first personal app: default the shared thread UI
// to Simplified Chinese unless a language was chosen explicitly.
try {
  if (!window.localStorage.getItem(LOCALE_STORAGE_KEY)) {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, "zh-CN");
  }
} catch {
  // Storage can be unavailable (private mode); the browser language then applies.
}

// Same shim as the workbench entry: LAN access over plain HTTP is not a secure
// context, so crypto.randomUUID is missing and client-side message IDs break.
if (typeof globalThis.crypto !== "undefined" && !("randomUUID" in globalThis.crypto)) {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: () =>
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      }),
    configurable: true,
  });
}

const root = document.getElementById("root");
if (!root) throw new Error("root element missing");

void initializeI18n().then(() => {
  ReactDOM.createRoot(root).render(<CompanionApp />);
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {
      // Progressive enhancement only; the page works without a service worker.
    });
  });
}
