/**
 * Lightweight, zero-dependency, 100%-offline Mermaid-to-SVG renderer.
 * Supports flowchart/graph, sequenceDiagram, stateDiagram, pie, mindmap,
 * classDiagram, erDiagram, and gantt diagrams with CJK-aware text metrics,
 * light/dark theming, and strict XML escaping.
 */

export interface MermaidRenderResult {
  svg: string;
  diagramType: string;
}

interface Palette {
  bg: string;
  nodeFill: string;
  nodeStroke: string;
  nodeText: string;
  accentFill: string;
  accentStroke: string;
  edge: string;
  edgeLabelBg: string;
  muted: string;
  subgraphFill: string;
  subgraphStroke: string;
  slices: string[];
}

const LIGHT_PALETTE: Palette = {
  bg: "transparent",
  nodeFill: "#fffefd",
  nodeStroke: "#bd5a02",
  nodeText: "#1e1814",
  accentFill: "#fee1d1",
  accentStroke: "#e67f3b",
  edge: "#6e6058",
  edgeLabelBg: "#f8f3ef",
  muted: "#6e6058",
  subgraphFill: "rgba(230, 127, 59, 0.06)",
  subgraphStroke: "rgba(189, 90, 2, 0.4)",
  slices: ["#bd5a02", "#2b6bd6", "#21904f", "#8557d6", "#d94f70", "#d49b00", "#0e8a9c", "#6e6058"],
};

const DARK_PALETTE: Palette = {
  bg: "transparent",
  nodeFill: "#261f1b",
  nodeStroke: "#ed8642",
  nodeText: "#f3edea",
  accentFill: "#422615",
  accentStroke: "#fb9250",
  edge: "#b5a8a1",
  edgeLabelBg: "#1a1512",
  muted: "#b5a8a1",
  subgraphFill: "rgba(251, 146, 80, 0.08)",
  subgraphStroke: "rgba(237, 134, 66, 0.45)",
  slices: ["#ed8642", "#639bff", "#4cc38a", "#a78bfa", "#f472b6", "#facc15", "#22d3ee", "#b5a8a1"],
};

function escXml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Approximate pixel width for mixed Latin / CJK labels at 13px font size. */
function measureTextPx(text: string, charPx = 7.6): number {
  let units = 0;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    units += code > 0x2e7f ? 1.8 : 1;
  }
  return Math.max(24, Math.ceil(units * charPx));
}

function stripQuotes(raw: string): string {
  const s = raw.trim();
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
    return s.slice(1, -1);
  }
  return s;
}

function cleanLines(source: string): string[] {
  return source
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.replace(/%%.*$/, "").trimEnd())
    .filter((line) => line.trim().length > 0);
}

/* ---------- Layered Graph Engine (Flowchart / State / Class / ER) ---------- */

type NodeShape = "rect" | "round" | "diamond" | "circle" | "cylinder" | "start" | "end";
type EdgeStyle = "solid" | "dashed" | "thick";

interface GraphNode {
  id: string;
  label: string;
  shape: NodeShape;
  sublabel?: string;
}

interface GraphEdge {
  from: string;
  to: string;
  label?: string;
  style: EdgeStyle;
  arrow: boolean;
}

function parseNodeToken(rawToken: string): { id: string; label?: string; shape?: NodeShape } {
  const token = rawToken.trim();
  if (token === "[*]") return { id: "[*]", label: "", shape: "start" };

  // Match id + shape bracket syntax: A[label], A(label), A([label]), A{label}, A((label)), A[(label)], A[[label]]
  const patterns: Array<[RegExp, NodeShape]> = [
    [/^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\(\[([\s\S]+?)\]\)$/, "round"],
    [/^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\(\(([\s\S]+?)\)\)$/, "circle"],
    [/^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\[\(([\s\S]+?)\)\]$/, "cylinder"],
    [/^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\[\[([\s\S]+?)\]\]$/, "rect"],
    [/^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\[([\s\S]+?)\]$/, "rect"],
    [/^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\(([\s\S]+?)\)$/, "round"],
    [/^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\{([\s\S]+?)\}$/, "diamond"],
  ];
  for (const [re, shape] of patterns) {
    const match = re.exec(token);
    if (match) {
      return { id: match[1], label: stripQuotes(match[2]).replace(/<br\s*\/?>/gi, " "), shape };
    }
  }
  const bare = /^([A-Za-z0-9_.\u4e00-\u9fff-]+)$/.exec(token);
  if (bare) return { id: bare[1] };
  return { id: token.replace(/\s+/g, "_").slice(0, 32), label: stripQuotes(token), shape: "rect" };
}

function upsertNode(map: Map<string, GraphNode>, token: string): string {
  const parsed = parseNodeToken(token);
  const existing = map.get(parsed.id);
  if (!existing) {
    map.set(parsed.id, {
      id: parsed.id,
      label: parsed.label ?? parsed.id,
      shape: parsed.shape ?? "rect",
    });
  } else {
    if (parsed.label && existing.label === existing.id) existing.label = parsed.label;
    if (parsed.shape && existing.shape === "rect") existing.shape = parsed.shape;
  }
  return parsed.id;
}

function renderLayeredGraphSvg(
  nodesMap: Map<string, GraphNode>,
  edges: GraphEdge[],
  direction: "TD" | "TB" | "BT" | "LR" | "RL",
  palette: Palette,
  markerPrefix: string,
): string {
  const nodes = Array.from(nodesMap.values());
  if (nodes.length === 0) throw new Error("未解析到有效节点");

  const horizontal = direction === "LR" || direction === "RL";
  const reverse = direction === "BT" || direction === "RL";

  // Compute topological layers safely (cycle-tolerant).
  const inDeg = new Map<string, number>();
  const outMap = new Map<string, string[]>();
  for (const n of nodes) {
    inDeg.set(n.id, 0);
    outMap.set(n.id, []);
  }
  for (const e of edges) {
    if (e.from === e.to) continue;
    outMap.get(e.from)?.push(e.to);
    inDeg.set(e.to, (inDeg.get(e.to) ?? 0) + 1);
  }

  const rank = new Map<string, number>();
  const queue: string[] = [];
  for (const n of nodes) {
    if ((inDeg.get(n.id) ?? 0) === 0) {
      queue.push(n.id);
      rank.set(n.id, 0);
    }
  }
  if (queue.length === 0) {
    queue.push(nodes[0].id);
    rank.set(nodes[0].id, 0);
  }

  const visited = new Set<string>();
  let guard = 0;
  const maxSteps = Math.max(64, nodes.length * (edges.length + 2));
  while (queue.length > 0 && guard < maxSteps) {
    guard += 1;
    const curr = queue.shift()!;
    visited.add(curr);
    const currRank = rank.get(curr) ?? 0;
    for (const next of outMap.get(curr) ?? []) {
      const candidate = currRank + 1;
      const prev = rank.get(next);
      if (prev === undefined || (candidate > prev && candidate < nodes.length)) {
        rank.set(next, candidate);
        queue.push(next);
      }
    }
  }
  for (const n of nodes) {
    if (!rank.has(n.id)) rank.set(n.id, visited.size > 0 ? 1 : 0);
  }

  const maxRank = Math.max(0, ...Array.from(rank.values()));
  const layers: GraphNode[][] = Array.from({ length: maxRank + 1 }, () => []);
  for (const n of nodes) {
    const r = rank.get(n.id) ?? 0;
    const idx = reverse ? maxRank - r : r;
    layers[idx].push(n);
  }

  // Size each node based on text length and shape.
  const sizes = new Map<string, { w: number; h: number }>();
  for (const n of nodes) {
    if (n.shape === "start" || n.shape === "end") {
      sizes.set(n.id, { w: 24, h: 24 });
    } else if (n.shape === "circle") {
      const d = Math.max(54, Math.min(110, measureTextPx(n.label) + 26));
      sizes.set(n.id, { w: d, h: d });
    } else if (n.shape === "diamond") {
      const w = Math.max(96, Math.min(200, measureTextPx(n.label) + 48));
      sizes.set(n.id, { w, h: Math.max(58, Math.round(w * 0.56)) });
    } else {
      const textW = Math.max(measureTextPx(n.label), n.sublabel ? measureTextPx(n.sublabel, 6.8) : 0);
      const w = Math.max(88, Math.min(240, textW + 32));
      const h = n.sublabel ? 56 : 42;
      sizes.set(n.id, { w, h });
    }
  }

  const PAD = 28;
  const CROSS_GAP = 32;
  const LAYER_GAP = 64;

  const layerSpans = layers.map((layer) => {
    const cross = layer.reduce((sum, n, i) => {
      const s = sizes.get(n.id)!;
      return sum + (horizontal ? s.h : s.w) + (i > 0 ? CROSS_GAP : 0);
    }, 0);
    const main = Math.max(0, ...layer.map((n) => (horizontal ? sizes.get(n.id)!.w : sizes.get(n.id)!.h)));
    return { cross, main };
  });

  const maxCross = Math.max(120, ...layerSpans.map((l) => l.cross));
  const totalMain = layerSpans.reduce((sum, l, i) => sum + l.main + (i > 0 ? LAYER_GAP : 0), 0);
  const width = Math.ceil((horizontal ? totalMain : maxCross) + PAD * 2);
  const height = Math.ceil((horizontal ? maxCross : totalMain) + PAD * 2);

  const pos = new Map<string, { x: number; y: number; w: number; h: number }>();
  let mainCursor = PAD;
  layers.forEach((layer, li) => {
    const span = layerSpans[li];
    let crossCursor = PAD + (maxCross - span.cross) / 2;
    for (const n of layer) {
      const s = sizes.get(n.id)!;
      if (horizontal) {
        const x = mainCursor + (span.main - s.w) / 2;
        const y = crossCursor;
        pos.set(n.id, { x, y, w: s.w, h: s.h });
        crossCursor += s.h + CROSS_GAP;
      } else {
        const x = crossCursor;
        const y = mainCursor + (span.main - s.h) / 2;
        pos.set(n.id, { x, y, w: s.w, h: s.h });
        crossCursor += s.w + CROSS_GAP;
      }
    }
    mainCursor += span.main + LAYER_GAP;
  });

  const parts: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="Mermaid diagram">`,
    `<defs>`,
    `<marker id="${markerPrefix}-arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">`,
    `<path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="${palette.edge}"/>`,
    `</marker>`,
    `</defs>`,
  ];

  // Draw edges
  for (const edge of edges) {
    const from = pos.get(edge.from);
    const to = pos.get(edge.to);
    if (!from || !to) continue;

    const dash = edge.style === "dashed" ? ' stroke-dasharray="5 4"' : "";
    const sw = edge.style === "thick" ? "2.6" : "1.6";
    const marker = edge.arrow ? ` marker-end="url(#${markerPrefix}-arrow)"` : "";

    let pathD: string;
    let labelX: number;
    let labelY: number;

    if (edge.from === edge.to) {
      const sx = from.x + from.w;
      const sy = from.y + from.h * 0.3;
      const ex = from.x + from.w;
      const ey = from.y + from.h * 0.7;
      pathD = `M ${sx} ${sy} C ${sx + 36} ${sy - 16}, ${ex + 36} ${ey + 16}, ${ex} ${ey}`;
      labelX = sx + 32;
      labelY = (sy + ey) / 2;
    } else if (horizontal) {
      const forward = to.x >= from.x;
      const sx = forward ? from.x + from.w : from.x;
      const sy = from.y + from.h / 2;
      const ex = forward ? to.x : to.x + to.w;
      const ey = to.y + to.h / 2;
      const dx = Math.max(28, Math.abs(ex - sx) * 0.45);
      const c1x = forward ? sx + dx : sx - dx;
      const c2x = forward ? ex - dx : ex + dx;
      pathD = `M ${sx} ${sy} C ${c1x} ${sy}, ${c2x} ${ey}, ${ex} ${ey}`;
      labelX = (sx + ex) / 2;
      labelY = (sy + ey) / 2 - 8;
    } else {
      const forward = to.y >= from.y;
      const sx = from.x + from.w / 2;
      const sy = forward ? from.y + from.h : from.y;
      const ex = to.x + to.w / 2;
      const ey = forward ? to.y : to.y + to.h;
      const dy = Math.max(24, Math.abs(ey - sy) * 0.45);
      const c1y = forward ? sy + dy : sy - dy;
      const c2y = forward ? ey - dy : ey + dy;
      pathD = `M ${sx} ${sy} C ${sx} ${c1y}, ${ex} ${c2y}, ${ex} ${ey}`;
      labelX = (sx + ex) / 2;
      labelY = (sy + ey) / 2;
    }

    parts.push(`<path d="${pathD}" fill="none" stroke="${palette.edge}" stroke-width="${sw}"${dash}${marker}/>`);
    if (edge.label) {
      const lw = measureTextPx(edge.label, 6.8) + 14;
      parts.push(
        `<rect x="${labelX - lw / 2}" y="${labelY - 10}" width="${lw}" height="20" rx="10" fill="${palette.edgeLabelBg}" stroke="${palette.subgraphStroke}" stroke-width="0.8"/>`,
        `<text x="${labelX}" y="${labelY + 4}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="11.5" fill="${palette.muted}">${escXml(edge.label)}</text>`,
      );
    }
  }

  // Draw nodes
  for (const n of nodes) {
    const box = pos.get(n.id)!;
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;

    if (n.shape === "start") {
      parts.push(`<circle cx="${cx}" cy="${cy}" r="10" fill="${palette.nodeStroke}"/>`);
      continue;
    }
    if (n.shape === "end") {
      parts.push(
        `<circle cx="${cx}" cy="${cy}" r="11" fill="none" stroke="${palette.nodeStroke}" stroke-width="2"/>`,
        `<circle cx="${cx}" cy="${cy}" r="6" fill="${palette.nodeStroke}"/>`,
      );
      continue;
    }
    if (n.shape === "diamond") {
      const pts = `${cx},${box.y} ${box.x + box.w},${cy} ${cx},${box.y + box.h} ${box.x},${cy}`;
      parts.push(
        `<polygon points="${pts}" fill="${palette.accentFill}" stroke="${palette.nodeStroke}" stroke-width="1.6"/>`,
      );
    } else if (n.shape === "circle") {
      parts.push(
        `<circle cx="${cx}" cy="${cy}" r="${box.w / 2}" fill="${palette.nodeFill}" stroke="${palette.nodeStroke}" stroke-width="1.6"/>`,
      );
    } else if (n.shape === "cylinder") {
      parts.push(
        `<rect x="${box.x}" y="${box.y + 6}" width="${box.w}" height="${box.h - 12}" rx="6" fill="${palette.nodeFill}" stroke="${palette.nodeStroke}" stroke-width="1.6"/>`,
        `<ellipse cx="${cx}" cy="${box.y + 8}" rx="${box.w / 2}" ry="6" fill="${palette.accentFill}" stroke="${palette.nodeStroke}" stroke-width="1.4"/>`,
      );
    } else {
      const rx = n.shape === "round" ? Math.round(box.h / 2) : 10;
      parts.push(
        `<rect x="${box.x}" y="${box.y}" width="${box.w}" height="${box.h}" rx="${rx}" fill="${palette.nodeFill}" stroke="${palette.nodeStroke}" stroke-width="1.6"/>`,
      );
    }

    if (n.sublabel) {
      parts.push(
        `<text x="${cx}" y="${box.y + 21}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="13" font-weight="600" fill="${palette.nodeText}">${escXml(n.label)}</text>`,
        `<line x1="${box.x + 8}" y1="${box.y + 29}" x2="${box.x + box.w - 8}" y2="${box.y + 29}" stroke="${palette.subgraphStroke}" stroke-width="0.9"/>`,
        `<text x="${cx}" y="${box.y + 45}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="11.5" fill="${palette.muted}">${escXml(n.sublabel)}</text>`,
      );
    } else {
      parts.push(
        `<text x="${cx}" y="${cy + 4.5}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="13" font-weight="500" fill="${palette.nodeText}">${escXml(n.label)}</text>`,
      );
    }
  }

  parts.push("</svg>");
  return parts.join("");
}

/* ---------- Diagram Specific Parsers ---------- */

function parseFlowchart(lines: string[], palette: Palette, markerPrefix: string): string {
  const dirMatch = /^(?:flowchart|graph)\s+(TD|TB|BT|LR|RL)\b/i.exec(lines[0].trim());
  const direction = (dirMatch?.[1]?.toUpperCase() as "TD" | "TB" | "BT" | "LR" | "RL") || "TD";
  const nodes = new Map<string, GraphNode>();
  const edges: GraphEdge[] = [];

  const edgeRe = /^(.+?)\s*(--\s+([^->]+?)\s+-->|-\.\s*([^.]+?)\s*\.->|==\s+([^=]+?)\s+==>|-->\|([^|]+)\||-\.->\|([^|]+)\||==>\|([^|]+)\||-->|-\.->|==>|---)\s*(.+)$/;

  for (let i = 1; i < lines.length; i += 1) {
    const raw = lines[i].trim();
    if (!raw || /^(?:subgraph\b|end\b|classDef\b|class\b|style\b|linkStyle\b|click\b|direction\b)/i.test(raw)) {
      continue;
    }
    const statements = raw.split(";").map((s) => s.trim()).filter(Boolean);
    for (const stmt of statements) {
      // Handle chained edges like A --> B --> C iteratively from left to right.
      let remaining = stmt;
      let matchedAny = false;
      while (remaining) {
        const m = edgeRe.exec(remaining);
        if (!m) break;
        matchedAny = true;
        const leftRaw = m[1].trim();
        const op = m[2];
        const label = (m[3] || m[4] || m[5] || m[6] || m[7] || m[8] || "").trim() || undefined;
        const rightRest = m[9].trim();

        // Check if rightRest itself starts another edge
        const nextEdge = edgeRe.exec(rightRest);
        const rightToken = nextEdge ? nextEdge[1].trim() : rightRest;

        const style: EdgeStyle = op.includes("-.") ? "dashed" : op.includes("==") ? "thick" : "solid";
        const arrow = op.includes(">");

        const fromId = upsertNode(nodes, leftRaw);
        const toId = upsertNode(nodes, rightToken);
        edges.push({ from: fromId, to: toId, label, style, arrow });

        if (!nextEdge) break;
        remaining = rightRest;
      }
      if (!matchedAny) {
        upsertNode(nodes, stmt);
      }
    }
  }

  return renderLayeredGraphSvg(nodes, edges, direction, palette, markerPrefix);
}

function parseSequenceDiagram(lines: string[], palette: Palette, markerPrefix: string): string {
  const actors: Array<{ id: string; label: string }> = [];
  const actorIndex = new Map<string, number>();
  const ensureActor = (rawId: string, label?: string) => {
    const id = rawId.trim();
    const existing = actorIndex.get(id);
    if (existing !== undefined) {
      if (label) actors[existing].label = label;
      return existing;
    }
    const idx = actors.length;
    actors.push({ id, label: label || id });
    actorIndex.set(id, idx);
    return idx;
  };

  type SeqEvent =
    | { kind: "msg"; from: number; to: number; text: string; dashed: boolean }
    | { kind: "note"; from: number; to: number; text: string };
  const events: SeqEvent[] = [];

  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line || /^(?:autonumber|activate|deactivate|loop|alt|else|opt|par|and|rect|end)\b/i.test(line)) {
      continue;
    }
    const decl = /^(?:participant|actor)\s+([A-Za-z0-9_.\u4e00-\u9fff-]+)(?:\s+as\s+(.+))?$/i.exec(line);
    if (decl) {
      ensureActor(decl[1], decl[2]?.trim());
      continue;
    }
    const note = /^Note\s+(?:over|left of|right of)\s+([^:]+):\s*(.+)$/i.exec(line);
    if (note) {
      const targets = note[1].split(",").map((s) => s.trim()).filter(Boolean);
      const a = ensureActor(targets[0]);
      const b = targets[1] ? ensureActor(targets[1]) : a;
      events.push({ kind: "note", from: Math.min(a, b), to: Math.max(a, b), text: note[2].trim() });
      continue;
    }
    const msg = /^([^->\s]+)\s*(-->>|->>|-->|->|--\)|-\))\s*([+-]?)([^:\s]+)\s*:\s*(.*)$/.exec(line);
    if (msg) {
      const from = ensureActor(msg[1]);
      const to = ensureActor(msg[4]);
      events.push({
        kind: "msg",
        from,
        to,
        text: msg[5].trim(),
        dashed: msg[2].startsWith("--"),
      });
    }
  }

  if (actors.length === 0) throw new Error("时序图未包含参与者或消息");

  const COL_W = 160;
  const PAD_X = 32;
  const TOP_Y = 20;
  const BOX_H = 38;
  const ROW_H = 48;
  const width = Math.max(280, actors.length * COL_W + PAD_X * 2);
  const lifelineTop = TOP_Y + BOX_H;
  const bodyHeight = Math.max(64, events.length * ROW_H + 24);
  const bottomBoxY = lifelineTop + bodyHeight;
  const height = bottomBoxY + BOX_H + 20;

  const colX = (index: number) => PAD_X + COL_W * index + COL_W / 2;

  const parts: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="Sequence diagram">`,
    `<defs>`,
    `<marker id="${markerPrefix}-seq" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">`,
    `<path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="${palette.edge}"/>`,
    `</marker>`,
    `</defs>`,
  ];

  // Lifelines and actor header/footer boxes
  actors.forEach((actor, idx) => {
    const cx = colX(idx);
    const bw = Math.min(COL_W - 18, Math.max(78, measureTextPx(actor.label) + 26));
    parts.push(
      `<line x1="${cx}" y1="${lifelineTop}" x2="${cx}" y2="${bottomBoxY}" stroke="${palette.subgraphStroke}" stroke-width="1.3" stroke-dasharray="5 4"/>`,
      `<rect x="${cx - bw / 2}" y="${TOP_Y}" width="${bw}" height="${BOX_H}" rx="10" fill="${palette.nodeFill}" stroke="${palette.nodeStroke}" stroke-width="1.5"/>`,
      `<text x="${cx}" y="${TOP_Y + 23}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="13" font-weight="600" fill="${palette.nodeText}">${escXml(actor.label)}</text>`,
      `<rect x="${cx - bw / 2}" y="${bottomBoxY}" width="${bw}" height="${BOX_H}" rx="10" fill="${palette.nodeFill}" stroke="${palette.nodeStroke}" stroke-width="1.5"/>`,
      `<text x="${cx}" y="${bottomBoxY + 23}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="13" font-weight="600" fill="${palette.nodeText}">${escXml(actor.label)}</text>`,
    );
  });

  // Messages and notes
  events.forEach((ev, idx) => {
    const y = lifelineTop + 28 + idx * ROW_H;
    if (ev.kind === "note") {
      const x1 = colX(ev.from);
      const x2 = colX(ev.to);
      const cx = (x1 + x2) / 2;
      const nw = Math.max(x2 - x1 + 44, measureTextPx(ev.text, 6.8) + 24);
      parts.push(
        `<rect x="${cx - nw / 2}" y="${y - 14}" width="${nw}" height="28" rx="8" fill="${palette.accentFill}" stroke="${palette.accentStroke}" stroke-width="1"/>`,
        `<text x="${cx}" y="${y + 4}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="12" fill="${palette.nodeText}">${escXml(ev.text)}</text>`,
      );
      return;
    }

    const x1 = colX(ev.from);
    const x2 = colX(ev.to);
    const dash = ev.dashed ? ' stroke-dasharray="5 4"' : "";
    if (ev.from === ev.to) {
      parts.push(
        `<path d="M ${x1} ${y - 6} H ${x1 + 42} V ${y + 12} H ${x1 + 2}" fill="none" stroke="${palette.edge}" stroke-width="1.5"${dash} marker-end="url(#${markerPrefix}-seq)"/>`,
        `<text x="${x1 + 48}" y="${y + 6}" text-anchor="start" font-family="system-ui, -apple-system, sans-serif" font-size="12" fill="${palette.nodeText}">${escXml(ev.text)}</text>`,
      );
    } else {
      const targetX = x2 > x1 ? x2 - 4 : x2 + 4;
      parts.push(
        `<line x1="${x1}" y1="${y}" x2="${targetX}" y2="${y}" stroke="${palette.edge}" stroke-width="1.6"${dash} marker-end="url(#${markerPrefix}-seq)"/>`,
        `<text x="${(x1 + x2) / 2}" y="${y - 7}" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="12" fill="${palette.nodeText}">${escXml(ev.text)}</text>`,
      );
    }
  });

  parts.push("</svg>");
  return parts.join("");
}

function parseStateDiagram(lines: string[], palette: Palette, markerPrefix: string): string {
  const nodes = new Map<string, GraphNode>();
  const edges: GraphEdge[] = [];
  let startCount = 0;
  let endCount = 0;

  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line || line === "{" || line === "}") continue;
    const stateAlias = /^state\s+"([^"]+)"\s+as\s+([A-Za-z0-9_.\u4e00-\u9fff-]+)$/i.exec(line);
    if (stateAlias) {
      nodes.set(stateAlias[2], { id: stateAlias[2], label: stateAlias[1], shape: "round" });
      continue;
    }
    const trans = /^(\[\*\]|[A-Za-z0-9_.\u4e00-\u9fff-]+)\s*-->\s*(\[\*\]|[A-Za-z0-9_.\u4e00-\u9fff-]+)(?:\s*:\s*(.+))?$/.exec(line);
    if (trans) {
      let fromId = trans[1];
      let toId = trans[2];
      if (fromId === "[*]") {
        fromId = `__start_${++startCount}`;
        nodes.set(fromId, { id: fromId, label: "", shape: "start" });
      } else if (!nodes.has(fromId)) {
        nodes.set(fromId, { id: fromId, label: fromId, shape: "round" });
      }
      if (toId === "[*]") {
        toId = `__end_${++endCount}`;
        nodes.set(toId, { id: toId, label: "", shape: "end" });
      } else if (!nodes.has(toId)) {
        nodes.set(toId, { id: toId, label: toId, shape: "round" });
      }
      edges.push({ from: fromId, to: toId, label: trans[3]?.trim(), style: "solid", arrow: true });
    }
  }
  return renderLayeredGraphSvg(nodes, edges, "TD", palette, markerPrefix);
}

function parsePieChart(lines: string[], palette: Palette): string {
  let title = "";
  const items: Array<{ label: string; value: number }> = [];

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    const titleMatch = /^(?:pie(?:\s+showData)?\s+)?title\s+(.+)$/i.exec(line);
    if (titleMatch) {
      title = titleMatch[1].trim();
      continue;
    }
    const entry = /^"([^"]+)"\s*:\s*([0-9]+(?:\.[0-9]+)?)$/.exec(line);
    if (entry) {
      const value = Number(entry[2]);
      if (Number.isFinite(value) && value > 0) items.push({ label: entry[1], value });
    }
  }

  if (items.length === 0) throw new Error("饼图未包含有效数据项");
  const total = items.reduce((sum, item) => sum + item.value, 0);
  const r = 82;
  const cx = 112;
  const cy = title ? 124 : 104;
  const legendX = 224;
  const maxLegendW = Math.max(120, ...items.map((it) => measureTextPx(`${it.label} (100%)`, 7.2) + 28));
  const width = Math.ceil(legendX + maxLegendW + 20);
  const height = Math.max(cy + r + 24, (title ? 52 : 28) + items.length * 26 + 20);

  const parts: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="Pie chart">`,
  ];
  if (title) {
    parts.push(
      `<text x="${width / 2}" y="26" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="14" font-weight="600" fill="${palette.nodeText}">${escXml(title)}</text>`,
    );
  }

  let angle = -Math.PI / 2;
  items.forEach((item, idx) => {
    const color = palette.slices[idx % palette.slices.length];
    const frac = item.value / total;
    const sweep = frac * Math.PI * 2;
    if (items.length === 1) {
      parts.push(`<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}"/>`);
    } else {
      const x1 = cx + r * Math.cos(angle);
      const y1 = cy + r * Math.sin(angle);
      const x2 = cx + r * Math.cos(angle + sweep);
      const y2 = cy + r * Math.sin(angle + sweep);
      const large = sweep > Math.PI ? 1 : 0;
      parts.push(
        `<path d="M ${cx} ${cy} L ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z" fill="${color}" stroke="${palette.edgeLabelBg}" stroke-width="1.5"/>`,
      );
    }
    angle += sweep;

    const ly = (title ? 58 : 34) + idx * 26;
    const pct = Math.round(frac * 100);
    parts.push(
      `<rect x="${legendX}" y="${ly - 10}" width="12" height="12" rx="3" fill="${color}"/>`,
      `<text x="${legendX + 18}" y="${ly}" font-family="system-ui, -apple-system, sans-serif" font-size="12.5" fill="${palette.nodeText}">${escXml(item.label)} (${pct}%)</text>`,
    );
  });

  parts.push("</svg>");
  return parts.join("");
}

function parseMindmap(lines: string[], palette: Palette, markerPrefix: string): string {
  const nodes = new Map<string, GraphNode>();
  const edges: GraphEdge[] = [];
  const stack: Array<{ indent: number; id: string }> = [];
  let seq = 0;

  for (let i = 1; i < lines.length; i += 1) {
    const raw = lines[i];
    const trimmed = raw.trim();
    if (!trimmed) continue;
    const indent = raw.length - raw.trimStart().length;
    const id = `mm_${++seq}`;
    const cleaned = trimmed
      .replace(/^root/i, "")
      .replace(/^\(\((.+)\)\)$/, "$1")
      .replace(/^\((.+)\)$/, "$1")
      .replace(/^\[(.+)\]$/, "$1")
      .trim() || trimmed;
    nodes.set(id, { id, label: cleaned, shape: stack.length === 0 ? "round" : "rect" });

    while (stack.length > 0 && stack[stack.length - 1].indent >= indent) {
      stack.pop();
    }
    if (stack.length > 0) {
      edges.push({ from: stack[stack.length - 1].id, to: id, style: "solid", arrow: false });
    }
    stack.push({ indent, id });
  }
  return renderLayeredGraphSvg(nodes, edges, "LR", palette, markerPrefix);
}

function parseClassOrErDiagram(lines: string[], palette: Palette, markerPrefix: string): string {
  const nodes = new Map<string, GraphNode>();
  const edges: GraphEdge[] = [];
  const relRe = /^([A-Za-z0-9_.\u4e00-\u9fff-]+)\s+([|o}<*.-]+--[|o>{*.-]+|<\|--|--\|>|\*--|o--|-->|\.\.>|--)\s+([A-Za-z0-9_.\u4e00-\u9fff-]+)(?:\s*:\s*(.+))?$/;

  let currentBlock: string | null = null;
  const members = new Map<string, string[]>();

  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line) continue;
    if (line === "}") {
      currentBlock = null;
      continue;
    }
    const blockStart = /^(?:class\s+)?([A-Za-z0-9_.\u4e00-\u9fff-]+)\s*\{$/.exec(line);
    if (blockStart) {
      currentBlock = blockStart[1];
      if (!nodes.has(currentBlock)) nodes.set(currentBlock, { id: currentBlock, label: currentBlock, shape: "rect" });
      members.set(currentBlock, []);
      continue;
    }
    if (currentBlock) {
      members.get(currentBlock)?.push(line);
      continue;
    }
    const m = relRe.exec(line);
    if (m) {
      const from = m[1];
      const op = m[2];
      const to = m[3];
      if (!nodes.has(from)) nodes.set(from, { id: from, label: from, shape: "rect" });
      if (!nodes.has(to)) nodes.set(to, { id: to, label: to, shape: "rect" });
      edges.push({
        from,
        to,
        label: m[4]?.trim(),
        style: op.includes("..") ? "dashed" : "solid",
        arrow: op.includes(">") || op.includes("<"),
      });
    }
  }

  for (const [id, list] of members.entries()) {
    const node = nodes.get(id);
    if (node && list.length > 0) {
      node.sublabel = list.slice(0, 2).join(" · ").slice(0, 34);
    }
  }

  return renderLayeredGraphSvg(nodes, edges, "TD", palette, markerPrefix);
}

function parseGantt(lines: string[], palette: Palette): string {
  let title = "";
  const tasks: Array<{ section: string; label: string }> = [];
  let section = "";

  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line || /^(?:dateFormat|axisFormat|excludes|includes|tickInterval)\b/i.test(line)) continue;
    const t = /^title\s+(.+)$/i.exec(line);
    if (t) {
      title = t[1].trim();
      continue;
    }
    const sec = /^section\s+(.+)$/i.exec(line);
    if (sec) {
      section = sec[1].trim();
      continue;
    }
    const task = /^([^:]+):\s*(.+)$/.exec(line);
    if (task) {
      tasks.push({ section, label: task[1].trim() });
    }
  }

  if (tasks.length === 0) throw new Error("甘特图未包含任务条目");
  const labelW = Math.min(180, Math.max(96, ...tasks.map((t) => measureTextPx(t.label, 7) + 20)));
  const barAreaW = 260;
  const width = labelW + barAreaW + 32;
  const top = title ? 44 : 20;
  const rowH = 34;
  const height = top + tasks.length * rowH + 20;

  const parts: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="Gantt chart">`,
  ];
  if (title) {
    parts.push(
      `<text x="${width / 2}" y="24" text-anchor="middle" font-family="system-ui, -apple-system, sans-serif" font-size="14" font-weight="600" fill="${palette.nodeText}">${escXml(title)}</text>`,
    );
  }
  const step = Math.max(24, Math.floor((barAreaW - 40) / Math.max(1, tasks.length)));
  tasks.forEach((task, idx) => {
    const y = top + idx * rowH;
    const bx = labelW + 12 + Math.min(barAreaW - 76, idx * Math.round(step * 0.65));
    const bw = Math.min(barAreaW - (bx - labelW - 12), Math.max(64, step + 36));
    const color = palette.slices[idx % palette.slices.length];
    parts.push(
      `<text x="${labelW}" y="${y + 18}" text-anchor="end" font-family="system-ui, -apple-system, sans-serif" font-size="12" fill="${palette.nodeText}">${escXml(task.label)}</text>`,
      `<rect x="${bx}" y="${y + 4}" width="${bw}" height="20" rx="6" fill="${color}" opacity="0.88"/>`,
    );
  });
  parts.push("</svg>");
  return parts.join("");
}

/**
 * Compile Mermaid source into a clean, self-contained, XML-escaped SVG string.
 * Throws a readable Chinese error message on empty or unsupported/malformed diagrams.
 */
export function renderMermaidToSvg(source: string, options: { dark?: boolean; id?: string } = {}): MermaidRenderResult {
  const lines = cleanLines(source);
  if (lines.length === 0) {
    throw new Error("图表源码为空");
  }

  const palette = options.dark ? DARK_PALETTE : LIGHT_PALETTE;
  const markerPrefix = (options.id ?? "mmd").replace(/[^a-zA-Z0-9_-]/g, "") || "mmd";
  const header = lines[0].trim();

  if (/^(?:flowchart|graph)\b/i.test(header)) {
    return { svg: parseFlowchart(lines, palette, markerPrefix), diagramType: "flowchart" };
  }
  if (/^sequenceDiagram\b/i.test(header)) {
    return { svg: parseSequenceDiagram(lines, palette, markerPrefix), diagramType: "sequence" };
  }
  if (/^stateDiagram(?:-v2)?\b/i.test(header)) {
    return { svg: parseStateDiagram(lines, palette, markerPrefix), diagramType: "state" };
  }
  if (/^pie\b/i.test(header)) {
    return { svg: parsePieChart(lines, palette), diagramType: "pie" };
  }
  if (/^mindmap\b/i.test(header)) {
    return { svg: parseMindmap(lines, palette, markerPrefix), diagramType: "mindmap" };
  }
  if (/^(?:classDiagram|erDiagram)\b/i.test(header)) {
    return { svg: parseClassOrErDiagram(lines, palette, markerPrefix), diagramType: header.split(/\s/)[0] };
  }
  if (/^gantt\b/i.test(header)) {
    return { svg: parseGantt(lines, palette), diagramType: "gantt" };
  }

  throw new Error(`暂不支持该 Mermaid 图表类型或语法有误：${header.slice(0, 32)}`);
}
