/*
 * The app's own drawings for the emoji it shows (helper faces, feed topics): drawn by the image
 * model in the default companion's style (tools/draw_icons.py), served from public/icons. An emoji
 * stays the stored value everywhere; where there is a drawing for it, the drawing is shown.
 */

const DRAWN: Record<string, string> = {
  "🤖": "robot",
  "🔎": "search",
  "🔍": "search",
  "💰": "money",
  "✍️": "write",
  "✍": "write",
  "🧭": "compass",
  "🛒": "cart",
  "🌐": "globe",
  "📚": "books",
  "🗂️": "folders",
  "🗂": "folders",
  "📁": "folders",
  "📈": "chart",
  "🍳": "cooking",
  "🏃": "run",
  "🎨": "palette",
  "🧑‍💻": "coder",
  "📮": "mailbox",
  "🗓️": "calendar",
  "🗓": "calendar",
  "📅": "calendar",
  "🙂": "smile",
  "😊": "smile",
  "💻": "laptop",
  "📰": "news",
  "🗞️": "news",
  "🗞": "news",
  "🌿": "leaf",
  "✈️": "plane",
  "✈": "plane",
  "🎬": "clapper",
  "⚽": "ball",
  "🏠": "home",
  "👥": "people",
  "🎯": "target",
  "📝": "memo",
};

/** The drawing for an emoji, or null when it has none (the emoji itself is shown). */
export function iconFor(emoji: string | null | undefined): string | null {
  const name = emoji ? DRAWN[emoji.trim()] : undefined;
  return name ? `/icons/${name}.webp` : null;
}

/** What the helper face picker offers, in order. */
export const FACE_CHOICES = ["🤖", "🔎", "💰", "✍️", "🧭", "🛒", "🌐", "📚", "🗂️", "📈", "🍳", "🏃", "🎨", "🧑‍💻", "📮", "🗓️"];
