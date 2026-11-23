import "@testing-library/jest-dom/vitest";
import { beforeAll, beforeEach } from "vitest";

import i18n, { initializeI18n } from "@/i18n";

// Ensure standards mode (<!doctype html> + CSS1Compat) so KaTeX never logs quirks-mode warnings.
if (typeof document !== "undefined") {
  if (!document.doctype) {
    document.insertBefore(
      document.implementation.createDocumentType("html", "", ""),
      document.documentElement,
    );
  }
  if (document.compatMode !== "CSS1Compat") {
    Object.defineProperty(document, "compatMode", {
      value: "CSS1Compat",
      configurable: true,
    });
  }
}

// Prevent happy-dom from opening real TCP sockets (127.0.0.1:3000 ECONNREFUSED)
// for unmocked fetches or background <img>/<iframe> resource loads.
const happyDOM = (
  typeof window !== "undefined"
    ? (window as unknown as {
        happyDOM?: {
          settings: {
            disableJavaScriptFileLoading: boolean;
            disableCSSFileLoading: boolean;
            navigation: { disableChildFrameNavigation: boolean };
            fetch: {
              interceptor: {
                beforeAsyncRequest?: () => Promise<Response>;
              } | null;
            };
          };
        };
      }).happyDOM
    : undefined
);

if (happyDOM) {
  happyDOM.settings.disableJavaScriptFileLoading = true;
  happyDOM.settings.disableCSSFileLoading = true;
  happyDOM.settings.navigation.disableChildFrameNavigation = true;
  happyDOM.settings.fetch.interceptor = {
    async beforeAsyncRequest() {
      const ResponseCtor = (window as unknown as { Response: typeof Response }).Response ?? Response;
      return new ResponseCtor(JSON.stringify({ detail: "unmocked_test_fetch" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      });
    },
  };
}

// The DOM test environment does not implement pointer capture used by Radix Select.
if (!HTMLElement.prototype.hasPointerCapture) {
  HTMLElement.prototype.hasPointerCapture = () => false;
  HTMLElement.prototype.setPointerCapture = () => {};
  HTMLElement.prototype.releasePointerCapture = () => {};
}
if (!HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = () => {};
}

// happy-dom does not load fonts; text measurements can run immediately.
Object.defineProperty(document, "fonts", {
  value: { ready: Promise.resolve() },
  configurable: true,
});

function createTestStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.get(String(key)) ?? null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(String(key));
    },
    setItem(key: string, value: string) {
      store.set(String(key), String(value));
    },
  };
}

if (typeof window !== "undefined" && typeof localStorage.setItem !== "function") {
  const storage = createTestStorage();
  Object.defineProperty(window, "localStorage", {
    value: storage,
    configurable: true,
  });
  Object.defineProperty(globalThis, "localStorage", {
    value: storage,
    configurable: true,
    writable: true,
  });
}

// happy-dom doesn't ship with ``crypto.randomUUID``; shim a tiny v4-ish helper.
if (!("randomUUID" in globalThis.crypto)) {
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

beforeAll(async () => {
  localStorage.setItem("nanobot.locale", "en");
  await initializeI18n();
});

beforeEach(async () => {
  if (i18n.language !== "en") {
    await i18n.changeLanguage("en");
  }
  document.documentElement.lang = "en";
  document.title = "nanobot";
  localStorage.setItem("nanobot.locale", "en");
});
