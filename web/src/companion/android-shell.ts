/**
 * Bridge injected by the Android app (android/app/src/main/java/.../MainActivity.java),
 * which shows this page full screen. Absent in browsers and the iPhone PWA.
 */
interface AndroidShell {
  /** Return to the app's setup screen to point it at another server. */
  changeServer(): void;
  /** This device's token, kept in the app's preferences ("" when not paired). Newer apps only. */
  deviceToken?(): string;
  /** Keep (or with "" forget) this device's token in the app's preferences. Newer apps only. */
  saveDevice?(token: string): void;
  /** Read a reply out with the phone's own voice (WebView has no speechSynthesis). 0.3 and later. */
  speak?(text: string, lang: string): void;
  stopSpeaking?(): void;
  speaking?(): boolean;
  /** Set the phone's notifications for reminders coming up again (a reply may have set one). */
  planReminders?(): void;
  /** 设置 → 通知: the phone's notification when a reminder is due. */
  remindersEnabled?(): boolean;
  setRemindersEnabled?(on: boolean): void;
  /** Text shared to the app from another app ("" when none), taken once. */
  takeShared?(): string;
  /** ＋ in the composer: the app's own sheet; what is picked comes as a companion:attach event. 0.4+. */
  attach?(): void;
  /** A file of the companion's into the system share sheet. */
  shareFile?(url: string, name: string): void;
  /** Open a file of the companion's in another Android app (VIEW intent). */
  openFile?(url: string, name: string): void;
  /** A PDF's first page, answered as a companion:pdf-preview event ({ id, image }). */
  pdfPreview?(url: string, id: string): void;
  /** Share plain text or exported Markdown via the Android system share sheet. */
  shareText?(text: string, title?: string): void;
  /** Trigger lightweight native haptic feedback ("light" | "medium" | "heavy"). */
  haptic?(kind?: "light" | "medium" | "heavy"): void;
}

/** What the app's ＋ sheet picked: the page fetches each from the app (never the server). */
export interface PickedFile {
  url: string;
  name: string;
  type: string;
}

export function androidShell(): AndroidShell | null {
  return (window as unknown as { PersonalBotAndroid?: AndroidShell }).PersonalBotAndroid ?? null;
}
