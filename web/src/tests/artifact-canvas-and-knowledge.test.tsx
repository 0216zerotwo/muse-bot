import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteKnowledgeDocument,
  fetchKnowledge,
  importCreationToKnowledge,
  saveCreationArtifact,
  searchKnowledge,
  updateKnowledgeDocument,
  uploadKnowledgeFile,
  type CreationsPayload,
  type KnowledgeDocument,
  type KnowledgePayload,
} from "@/companion/companion-api";
import { CreationsView, formatSize } from "@/companion/Creations";
import {
  artifactFormatForLanguage,
  artifactHtmlDocument,
  downloadFilenameForCode,
  downloadTextFile,
  fileExtensionForLanguage,
  fileKind,
  isKnowledgeImportable,
  isPreviewableDocument,
  sanitizeSvgMarkup,
  svgPageDocument,
} from "@/companion/fileKinds";
import { KnowledgeVault, splitHighlightedText } from "@/companion/KnowledgeVault";
import {
  ArtifactProvider,
  inferArtifactTitle,
  inferArtifactType,
  isMermaidLanguage,
  isPreviewableArtifactLanguage,
  useArtifact,
} from "@/companion/muse/ArtifactContext";
import { ArtifactPane } from "@/companion/muse/ArtifactPane";
import { Viewer } from "@/companion/Viewer";
import { CodeBlock } from "@/components/CodeBlock";
import MermaidDiagram from "@/components/MermaidDiagram";
import { renderMermaidToSvg } from "@/lib/mermaid-svg";
import type { NanobotClient } from "@/lib/nanobot-client";

const { requestMutation } = vi.hoisted(() => ({
  requestMutation: vi.fn(),
}));

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: { requestMutation }, token: "test-token" }),
}));

const sampleDoc1: KnowledgeDocument = {
  id: "doc-1",
  title: "架构设计笔记",
  filename: "architecture.md",
  collection: "研发资料",
  mime: "text/markdown",
  size_bytes: 4096,
  char_count: 1800,
  chunk_count: 3,
  content_sha256: "abc123",
  origin: "upload",
  created_at: Math.floor(Date.now() / 1000) - 600,
  updated_at: Math.floor(Date.now() / 1000) - 60,
};

const sampleDoc2: KnowledgeDocument = {
  id: "doc-2",
  title: "Q4 会议纪要",
  filename: "meeting.pdf",
  collection: "默认",
  mime: "application/pdf",
  size_bytes: 120 * 1024,
  char_count: 5400,
  chunk_count: 7,
  content_sha256: "def456",
  origin: "creation",
  created_at: Math.floor(Date.now() / 1000) - 3600,
  updated_at: Math.floor(Date.now() / 1000) - 300,
};

const sampleDoc3: KnowledgeDocument = {
  id: "doc-3",
  title: "本地笔记",
  filename: "local.txt",
  collection: "默认",
  mime: "text/plain",
  size_bytes: 512,
  char_count: 200,
  chunk_count: 1,
  content_sha256: "ghi789",
  origin: "local",
  created_at: Math.floor(Date.now() / 1000) - 7200,
  updated_at: Math.floor(Date.now() / 1000) - 700,
};

const sampleKnowledgePayload: KnowledgePayload = {
  documents: [sampleDoc1, sampleDoc2, sampleDoc3],
  collections: ["默认", "研发资料"],
  collection_counts: { 默认: 2, 研发资料: 1 },
  stats: { doc_count: 3, chunk_count: 11, total_bytes: 4096 + 120 * 1024 + 512 },
};

function stubDesktopMedia(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: query.includes("min-width: 1024px") ? matches : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

function stubDownloadMocks() {
  const createObjectURL = vi.fn(() => "blob:mock-url");
  const revokeObjectURL = vi.fn();
  const origCreate = URL.createObjectURL;
  const origRevoke = URL.revokeObjectURL;
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
  const downloadedNames: string[] = [];
  const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    downloadedNames.push(this.download);
  });
  return {
    createObjectURL,
    revokeObjectURL,
    downloadedNames,
    clickSpy,
    restore() {
      URL.createObjectURL = origCreate;
      URL.revokeObjectURL = origRevoke;
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn(async () => undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("fileKinds format upgrades and sandbox sanitization", () => {
  it("recognizes svg, mermaid, html, and common code/config files as previewable", () => {
    expect(fileKind("diagram.mmd")).toBe("mermaid");
    expect(fileKind("flow.mermaid")).toBe("mermaid");
    expect(fileKind("icon.svg")).toBe("svg");
    expect(fileKind("landing.html")).toBe("web");
    for (const name of [
      "app.py", "index.ts", "App.tsx", "main.js", "Card.jsx", "data.json",
      "config.yaml", "docker.yml", "theme.css", "deploy.sh", "schema.sql",
      "Cargo.toml", "main.rs", "server.go", "Main.java", "View.swift",
    ]) {
      expect(fileKind(name)).toBe("code");
      expect(isPreviewableDocument(name)).toBe(true);
    }
    expect(isPreviewableDocument("diagram.mmd")).toBe(true);
    expect(isPreviewableDocument("vector.svg")).toBe(true);
    expect(isPreviewableDocument("page.html")).toBe(true);
    expect(isPreviewableDocument("book.pdf")).toBe(false);
    expect(isPreviewableDocument("archive.zip")).toBe(false);

    expect(isKnowledgeImportable("report.pdf")).toBe(true);
    expect(isKnowledgeImportable("slides.pptx")).toBe(true);
    expect(isKnowledgeImportable("notes.md")).toBe(true);
    expect(isKnowledgeImportable("README")).toBe(true);
    expect(isKnowledgeImportable("photo.png")).toBe(false);
    expect(isKnowledgeImportable("archive.zip")).toBe(false);
  });

  it("maps code languages to download extensions and backend-safe artifact formats", () => {
    expect(fileExtensionForLanguage("python")).toBe("py");
    expect(fileExtensionForLanguage("typescript")).toBe("ts");
    expect(fileExtensionForLanguage("html")).toBe("html");
    expect(fileExtensionForLanguage("sql")).toBe("sql");
    expect(fileExtensionForLanguage("mermaid")).toBe("mmd");
    expect(fileExtensionForLanguage("customx")).toBe("customx");
    expect(fileExtensionForLanguage("invalid lang!")).toBe("txt");
    expect(fileExtensionForLanguage("")).toBe("txt");

    expect(artifactFormatForLanguage("html")).toBe("html");
    expect(artifactFormatForLanguage("mmd")).toBe("mermaid");
    expect(artifactFormatForLanguage("python")).toBe("py");
    expect(artifactFormatForLanguage("sql")).toBe("txt");
    expect(artifactFormatForLanguage("rust")).toBe("txt");

    expect(downloadFilenameForCode("python")).toBe("snippet.py");
    expect(downloadFilenameForCode("html", "仪表盘: v1")).toBe("仪表盘-v1.html");
    expect(downloadFilenameForCode("svg")).toBe("graphic.svg");
    expect(downloadFilenameForCode("mermaid")).toBe("diagram.mmd");
    expect(downloadFilenameForCode("markdown", "notes.md")).toBe("notes.md");
    expect(downloadFilenameForCode("markdown")).toBe("document.md");
  });

  it("builds strict CSP HTML documents with theme defaults and sanitizes SVG payloads", () => {
    const darkHtml = artifactHtmlDocument(
      '<html><head><base href="https://evil.test/"><meta http-equiv="refresh" content="0;url=https://evil.test"></head><body><h1>Hi</h1></body></html>',
      true,
    );
    expect(darkHtml).toMatch(/^<!doctype html><html><head><meta http-equiv="Content-Security-Policy"/);
    expect(darkHtml).toContain("connect-src 'none'");
    expect(darkHtml).toContain("color-scheme:dark");
    expect(darkHtml).not.toMatch(/http-equiv=["']?refresh/i);
    expect(darkHtml).not.toMatch(/<base\b/i);

    const lightHtml = artifactHtmlDocument("<p>light</p>", false);
    expect(lightHtml).toContain("color-scheme:light");

    const dirtySvg =
      '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script>' +
      '<foreignObject><body>bad</body></foreignObject>' +
      '<style>@import url(https://evil.test/x.css); rect { fill: red; background: expression(1); }</style>' +
      '<a href="javascript:alert(3)"><circle cx="10" cy="10" r="8" onclick="evil()" style="background:url(https://evil.test/x)"/></a>' +
      '<use href="#safe-id"/><image href="https://evil.test/track.png"/><image href="data:image/png;base64,AA=="/></svg>';
    const cleanSvg = sanitizeSvgMarkup(dirtySvg);
    expect(cleanSvg).not.toContain("<script");
    expect(cleanSvg).not.toContain("foreignObject");
    expect(cleanSvg).not.toContain("onload");
    expect(cleanSvg).not.toContain("onclick");
    expect(cleanSvg).not.toContain("javascript:");
    expect(cleanSvg).not.toContain("@import");
    expect(cleanSvg).not.toContain("https://evil.test");
    expect(cleanSvg).toContain("#safe-id");
    expect(cleanSvg).toContain("data:image/png");
    expect(cleanSvg).toContain("<circle");

    expect(sanitizeSvgMarkup("   ")).toBe("");
    expect(sanitizeSvgMarkup("<svg><rect width='10' height='10'/></svg>")).toContain("xmlns=");

    const svgDoc = svgPageDocument(dirtySvg, true);
    expect(svgDoc).toContain("default-src 'none'");
    expect(svgDoc).not.toContain("<script");
    expect(svgDoc).toContain("<circle");
  });

  it("triggers client-side downloads cleanly via Blob URLs", () => {
    const dl = stubDownloadMocks();
    try {
      expect(downloadTextFile("SELECT 1;", "query.sql")).toBe(true);
      expect(dl.createObjectURL).toHaveBeenCalledTimes(1);
      expect(dl.downloadedNames).toEqual(["query.sql"]);
      expect(dl.revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
    } finally {
      dl.restore();
    }
  });
});

describe("ArtifactContext, ArtifactPane, and MermaidDiagram", () => {
  it("infers artifact types and sensible Chinese titles from source content", () => {
    expect(isPreviewableArtifactLanguage("HTML")).toBe(true);
    expect(isPreviewableArtifactLanguage("svg")).toBe(true);
    expect(isPreviewableArtifactLanguage("mmd")).toBe(true);
    expect(isPreviewableArtifactLanguage("md")).toBe(true);
    expect(isPreviewableArtifactLanguage("python")).toBe(false);
    expect(isMermaidLanguage("mermaid")).toBe(true);
    expect(isMermaidLanguage("mmd")).toBe(true);
    expect(isMermaidLanguage("html")).toBe(false);

    expect(inferArtifactType("htm")).toBe("html");
    expect(inferArtifactType("svg")).toBe("svg");
    expect(inferArtifactType("mmd")).toBe("mermaid");
    expect(inferArtifactType("markdown")).toBe("markdown");
    expect(inferArtifactType("python")).toBe("code");

    expect(inferArtifactTitle("<html><head><title>销售看板</title></head></html>", "html")).toBe("销售看板");
    expect(inferArtifactTitle("<h1>快速原型</h1>", "html")).toBe("快速原型");
    expect(inferArtifactTitle("<p>hi</p>", "html")).toBe("网页预览");
    expect(inferArtifactTitle("<svg><title>系统拓扑</title></svg>", "svg")).toBe("系统拓扑");
    expect(inferArtifactTitle("<svg><circle r='5'/></svg>", "svg")).toBe("SVG 图形");
    expect(inferArtifactTitle("%% comment\nsequenceDiagram\nA->>B: hi", "mermaid")).toBe("时序图");
    expect(inferArtifactTitle("flowchart TD\nA-->B", "mermaid")).toBe("流程图");
    expect(inferArtifactTitle("classDiagram\nA <|-- B", "mermaid")).toBe("类图");
    expect(inferArtifactTitle("erDiagram\nA ||--o{ B : has", "mermaid")).toBe("ER 关系图");
    expect(inferArtifactTitle("gantt\ntitle Plan", "mermaid")).toBe("甘特图");
    expect(inferArtifactTitle("pie\n\"A\": 10", "mermaid")).toBe("饼图");
    expect(inferArtifactTitle("mindmap\n  root", "mermaid")).toBe("思维导图");
    expect(inferArtifactTitle("stateDiagram-v2\n[*] --> Idle", "mermaid")).toBe("状态图");
    expect(inferArtifactTitle("journey\ntitle Day", "mermaid")).toBe("Mermaid 图表");
    expect(inferArtifactTitle("# 项目周报\n内容", "markdown")).toBe("项目周报");
    expect(inferArtifactTitle("普通段落", "markdown")).toBe("Markdown 文档");
    expect(inferArtifactTitle("print(1)", "code", "py")).toBe("PY 代码");
    expect(inferArtifactTitle("plain", "code", "")).toBe("代码画布");
  });

  it("opens desktop side-pane from CodeBlock preview, toggles source, downloads, copies, saves to creations, and refines", async () => {
    stubDesktopMedia(true);
    const dl = stubDownloadMocks();
    const onRefine = vi.fn();
    requestMutation.mockResolvedValueOnce({
      items: [],
      counts: { image: 0, video: 0, document: 1, audio: 0 },
      saved_id: "文档/2026-11-26 交互卡片.html",
    });

    try {
      render(
        <ArtifactProvider onRefine={onRefine}>
          <CodeBlock language="html" code="<html><head><title>交互卡片</title></head><body><h1>Hello</h1></body></html>" />
          <ArtifactPane />
        </ArtifactProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: "在画布中预览" }));
      const pane = await screen.findByTestId("artifact-pane-desktop");
      expect(pane).toHaveAttribute("role", "complementary");
      expect(within(pane).getByText("交互卡片")).toBeInTheDocument();

      const iframe = within(pane).getByTitle("HTML 预览：交互卡片") as HTMLIFrameElement;
      expect(iframe).toHaveAttribute("sandbox", "allow-scripts");
      expect(iframe.getAttribute("srcdoc")).toContain("Content-Security-Policy");

      // Toggle to Edit tab, toggle wrap, and back to Preview tab
      fireEvent.click(within(pane).getByRole("tab", { name: "编辑" }));
      expect(within(pane).getByText("交互卡片.html")).toBeInTheDocument();
      const wrapBtn = within(pane).getByRole("button", { name: "自动换行" });
      expect(wrapBtn).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(wrapBtn);
      expect(wrapBtn).toHaveAttribute("aria-pressed", "false");
      fireEvent.click(within(pane).getByRole("tab", { name: "预览" }));
      expect(within(pane).getByTitle("HTML 预览：交互卡片")).toBeInTheDocument();

      // Copy source
      fireEvent.click(within(pane).getByRole("button", { name: "复制源码" }));
      await waitFor(() => expect(within(pane).getByRole("button", { name: "已复制" })).toBeInTheDocument());

      // Download file
      fireEvent.click(within(pane).getByRole("button", { name: "下载" }));
      expect(dl.downloadedNames).toEqual(["交互卡片.html"]);

      // Save to creations
      fireEvent.click(within(pane).getByRole("button", { name: "存入资源库" }));
      await waitFor(() =>
        expect(requestMutation).toHaveBeenCalledWith("companion.creations.save", {
          title: "交互卡片",
          content: "<html><head><title>交互卡片</title></head><body><h1>Hello</h1></body></html>",
          format: "html",
        }),
      );
      expect(await within(pane).findByRole("button", { name: "已存入资源库" })).toBeInTheDocument();

      // Refine in chat
      fireEvent.click(within(pane).getByRole("button", { name: "让伙伴继续完善" }));
      expect(onRefine).toHaveBeenCalledWith("请帮我继续完善「交互卡片」：");

      // Close pane
      fireEvent.click(within(pane).getByRole("button", { name: "关闭画布" }));
      expect(screen.queryByTestId("artifact-pane-desktop")).not.toBeInTheDocument();
    } finally {
      dl.restore();
    }
  });

  it("renders mobile full-screen sheet for SVG/Markdown/Mermaid/Code, downloads each format, and surfaces save errors", async () => {
    stubDesktopMedia(false);
    const dl = stubDownloadMocks();
    const onRefine = vi.fn();
    requestMutation.mockRejectedValueOnce(new Error("作品内容超过限制"));

    function Trigger() {
      const ctx = useArtifact();
      return (
        <div>
          <button
            type="button"
            onClick={() =>
              ctx?.openArtifact({
                language: "svg",
                code: '<svg xmlns="http://www.w3.org/2000/svg"><title>徽标</title><circle cx="12" cy="12" r="10"/></svg>',
              })
            }
          >
            open-svg
          </button>
          <button
            type="button"
            onClick={() => ctx?.openArtifact({ language: "mermaid", code: "flowchart LR\nA-->B" })}
          >
            open-mmd
          </button>
          <button
            type="button"
            onClick={() => ctx?.openArtifact({ language: "md", code: "# 计划\n- 第一步" })}
          >
            open-md
          </button>
          <button
            type="button"
            onClick={() => ctx?.openArtifact({ language: "py", code: "print('ok')" })}
          >
            open-code
          </button>
        </div>
      );
    }

    try {
      render(
        <ArtifactProvider onRefine={onRefine}>
          <Trigger />
          <ArtifactPane />
        </ArtifactProvider>,
      );

      // SVG in mobile sheet + download
      fireEvent.click(screen.getByRole("button", { name: "open-svg" }));
      const sheet = await screen.findByTestId("artifact-pane-sheet");
      expect(sheet).toHaveAttribute("role", "dialog");
      const svgFrame = within(sheet).getByTitle("SVG 预览：徽标");
      expect(svgFrame).toHaveAttribute("sandbox", "");
      fireEvent.click(within(sheet).getByRole("button", { name: "下载" }));

      // Save failure alert
      fireEvent.click(within(sheet).getByRole("button", { name: "存入资源库" }));
      expect(await within(sheet).findByRole("alert")).toHaveTextContent("作品内容超过限制");

      // Mermaid preview inside pane
      fireEvent.click(screen.getByRole("button", { name: "open-mmd" }));
      expect(await within(sheet).findByTestId("mermaid-svg-stage")).toBeInTheDocument();

      // Code artifact (source-only)
      fireEvent.click(screen.getByRole("button", { name: "open-code" }));
      expect(within(sheet).queryByRole("tablist", { name: "画布视图切换" })).toBeNull();
      fireEvent.click(within(sheet).getByRole("button", { name: "下载" }));

      // Markdown preview inside pane + download
      fireEvent.click(screen.getByRole("button", { name: "open-md" }));
      await waitFor(() => expect(within(sheet).getByText("计划")).toBeInTheDocument());
      fireEvent.click(within(sheet).getByRole("button", { name: "下载" }));
      expect(dl.downloadedNames).toEqual(["徽标.svg", "PY-代码.py", "计划.md"]);

      // Mobile refine closes sheet
      fireEvent.click(within(sheet).getByRole("button", { name: "让伙伴继续完善" }));
      expect(onRefine).toHaveBeenCalledWith("请帮我继续完善「计划」：");
      expect(screen.queryByTestId("artifact-pane-sheet")).not.toBeInTheDocument();
    } finally {
      dl.restore();
    }
  });

  it("renders MermaidDiagram offline for flowchart/sequence/state/pie/mindmap/class/er/gantt with zoom, XSS escaping, and error fallback", async () => {
    const { rerender } = render(<MermaidDiagram code={"flowchart TD\nA[开始] -->|确认| B{通过?}\nB -.-> C((结束))\nB ==> D[(数据库)]\nD --> D"} interactive />);

    const stage = await screen.findByTestId("mermaid-svg-stage");
    expect(stage.querySelector("svg")).not.toBeNull();
    expect(stage.textContent).toContain("开始");
    expect(stage.textContent).toContain("通过?");
    expect(stage).toHaveAttribute("data-fit", "true");

    fireEvent.click(screen.getByRole("button", { name: "放大图表" }));
    expect(stage).toHaveAttribute("data-fit", "false");
    expect(stage).toHaveAttribute("data-zoom", "1.25");

    fireEvent.click(screen.getByRole("button", { name: "缩小图表" }));
    expect(stage).toHaveAttribute("data-zoom", "1");

    fireEvent.click(screen.getByRole("button", { name: "适应窗口" }));
    expect(stage).toHaveAttribute("data-fit", "true");

    // Non-interactive mode hides toolbar & supports horizontal LR chained edges
    rerender(<MermaidDiagram code={"graph LR\nX([输入]) --> Y[处理] --> Z[输出]"} interactive={false} />);
    expect(screen.queryByRole("toolbar", { name: "图表缩放" })).toBeNull();
    expect(await screen.findByTestId("mermaid-svg-stage")).toHaveTextContent("处理");

    // Unsupported diagram header error fallback
    rerender(<MermaidDiagram code="unknownDiagram\nA-->B" interactive />);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂不支持该 Mermaid 图表类型");

    // Blank source
    rerender(<MermaidDiagram code="   " interactive />);
    expect(await screen.findByRole("alert")).toHaveTextContent("图表源码为空");

    // Direct engine checks across all diagram families + XSS escaping + dark theme
    const seq = renderMermaidToSvg(
      "sequenceDiagram\nparticipant U as 用户\nparticipant S as 服务\nU->>S: 请求\nS-->>U: 响应\nS->>S: 自检\nNote over U,S: 完成",
      { dark: true, id: "seq1" },
    );
    expect(seq.diagramType).toBe("sequence");
    expect(seq.svg).toContain("用户");
    expect(seq.svg).toContain("完成");

    const state = renderMermaidToSvg('stateDiagram-v2\nstate "运行中" as Run\n[*] --> Idle\nIdle --> Run : 启动\nRun --> [*]');
    expect(state.diagramType).toBe("state");
    expect(state.svg).toContain("运行中");

    const pie = renderMermaidToSvg('pie title 占比\n"代码" : 60\n"文档" : 40');
    expect(pie.diagramType).toBe("pie");
    expect(pie.svg).toContain("代码 (60%)");
    expect(renderMermaidToSvg('pie\n"唯一" : 100').svg).toContain("<circle");

    const mm = renderMermaidToSvg("mindmap\n  root((核心))\n    分支A\n      叶子1\n    分支B");
    expect(mm.diagramType).toBe("mindmap");
    expect(mm.svg).toContain("核心");

    const cls = renderMermaidToSvg("classDiagram\nclass User {\n  +id: int\n  +name: str\n}\nUser --> Order : places");
    expect(cls.diagramType).toBe("classDiagram");
    expect(cls.svg).toContain("User");

    const er = renderMermaidToSvg("erDiagram\nCUSTOMER ||--o{ ORDER : places");
    expect(er.diagramType).toBe("erDiagram");
    expect(er.svg).toContain("CUSTOMER");

    const gantt = renderMermaidToSvg("gantt\ntitle 排期\nsection 第一阶段\n设计 :a1, 2026-01-01, 5d\n开发 :a2, after a1, 10d");
    expect(gantt.diagramType).toBe("gantt");
    expect(gantt.svg).toContain("排期");

    const xss = renderMermaidToSvg('flowchart TD\nA["<script>alert(1)</script>"] --> B[Safe]');
    expect(xss.svg).not.toContain("<script>");
    expect(xss.svg).toContain("&lt;script&gt;");
  });
});

describe("CodeBlock toolbar upgrades", () => {
  it("supports inline lazy Mermaid preview, custom onPreview, ANSI output, line numbers, copy, wrap toggle, and inferred-extension download", async () => {
    const dl = stubDownloadMocks();
    const onPreview = vi.fn();

    try {
      const { rerender } = render(
        <CodeBlock language="mermaid" code={"graph TD\nA-->B"} highlight={false} showLineNumbers />,
      );
      // Without ArtifactProvider or onPreview, clicking 预览 on mermaid opens inline diagram
      fireEvent.click(screen.getByRole("button", { name: "在画布中预览" }));
      expect(await screen.findByTestId("mermaid-svg-stage")).toBeInTheDocument();

      // Toggle collapse inline diagram
      fireEvent.click(screen.getByRole("button", { name: "收起内联图表" }));
      expect(screen.queryByTestId("mermaid-svg-stage")).toBeNull();

      // Re-open via inline toggle
      fireEvent.click(screen.getByRole("button", { name: "展开内联图表" }));
      expect(await screen.findByTestId("mermaid-svg-stage")).toBeInTheDocument();

      // Toggle wrap off and on
      const wrapBtn = screen.getByRole("button", { name: "关闭自动换行" });
      expect(wrapBtn).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(wrapBtn);
      expect(screen.getByRole("button", { name: "开启自动换行" })).toHaveAttribute("aria-pressed", "false");

      // Copy code
      fireEvent.click(screen.getByRole("button", { name: /copy/i }));
      await waitFor(() => expect(screen.getByText("已复制")).toBeInTheDocument());

      // Download mermaid -> .mmd
      fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
      expect(dl.downloadedNames).toEqual(["diagram.mmd"]);

      // Custom onPreview callback for HTML
      rerender(<CodeBlock language="html" code="<p>Hi</p>" highlight={false} onPreview={onPreview} />);
      fireEvent.click(screen.getByRole("button", { name: "在画布中预览" }));
      expect(onPreview).toHaveBeenCalledWith({ language: "html", code: "<p>Hi</p>" });

      // Python block: no preview button, downloads as .py
      rerender(<CodeBlock language="python" code="print('hello')" highlight={false} />);
      expect(screen.queryByRole("button", { name: "在画布中预览" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "下载文件" }));

      // SQL block -> .sql
      rerender(<CodeBlock language="sql" code="SELECT * FROM kb_documents;" highlight={false} />);
      fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
      expect(dl.downloadedNames).toEqual(["diagram.mmd", "snippet.py", "snippet.sql"]);

      // ANSI output rendering & chrome="none"
      rerender(<CodeBlock language="ansi" code={"\u001b[32mPASS\u001b[0m\nline2"} showLineNumbers chrome="none" />);
      expect(screen.getByTestId("ansi-code")).toHaveTextContent("PASS");
      expect(screen.queryByRole("toolbar", { name: "代码块工具栏" })).toBeNull();
    } finally {
      dl.restore();
    }
  });
});

describe("Viewer full-format document previews", () => {
  it("previews HTML, SVG, Mermaid, Markdown, CSV, code, video/audio, multi-item navigation, and unsupported binary fallback", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("demo.html")) return new Response("<h1>页面标题</h1>", { status: 200 });
      if (url.endsWith("logo.svg")) return new Response('<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>', { status: 200 });
      if (url.endsWith("flow.mmd")) return new Response("flowchart TD\nStart-->End", { status: 200 });
      if (url.endsWith("script.py")) return new Response("def greet():\n    return 'hi'", { status: 200 });
      if (url.endsWith("data.csv")) return new Response("姓名,分数\n小明,98\n小红,95", { status: 200 });
      if (url.endsWith("doc.md")) return new Response("# 标题\n正文段落", { status: 200 });
      return new Response("ok", { status: 200 });
    });

    // 1. HTML with preview / source toggle
    const { rerender } = render(
      <Viewer
        items={[{ key: "h1", kind: "document", url: "/f/demo.html", name: "demo.html", title: "示例网页" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    const htmlFrame = await screen.findByTitle("预览：示例网页");
    expect(htmlFrame).toHaveAttribute("sandbox", "allow-scripts");
    fireEvent.click(screen.getByRole("tab", { name: "源码" }));
    expect(screen.getByTestId("viewer-code-body")).toHaveTextContent("<h1>页面标题</h1>");
    fireEvent.click(screen.getByRole("button", { name: "自动换行" }));
    expect(screen.getByTestId("viewer-code-body")).toHaveAttribute("data-wrapped", "false");
    fireEvent.click(screen.getByRole("tab", { name: "预览" }));
    expect(screen.getByTitle("预览：示例网页")).toBeInTheDocument();

    // 2. SVG document
    rerender(
      <Viewer
        items={[{ key: "s1", kind: "document", url: "/f/logo.svg", name: "logo.svg", title: "品牌图" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    const svgFrame = await screen.findByTitle("预览：品牌图");
    expect(svgFrame).toHaveAttribute("sandbox", "");

    // 3. Mermaid document
    rerender(
      <Viewer
        items={[{ key: "m1", kind: "document", url: "/f/flow.mmd", name: "flow.mmd", title: "流程图" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    expect(await screen.findByTestId("mermaid-svg-stage")).toBeInTheDocument();

    // 4. Markdown document with preview & source toggle
    rerender(
      <Viewer
        items={[{ key: "md1", kind: "document", url: "/f/doc.md", name: "doc.md", title: "文档" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    expect(await screen.findByText(/正文段落/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "源码" }));
    expect(screen.getByTestId("viewer-code-body")).toHaveTextContent("# 标题");

    // 5. Python code document with monospace body & copy
    rerender(
      <Viewer
        items={[{ key: "p1", kind: "document", url: "/f/script.py", name: "script.py", title: "脚本" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    const codeBody = await screen.findByTestId("viewer-code-body");
    expect(codeBody).toHaveTextContent("def greet():");
    expect(screen.queryByText(/暂不支持页内直接预览/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "已复制" })).toBeInTheDocument());

    // 6. CSV table document
    rerender(
      <Viewer
        items={[{ key: "c1", kind: "document", url: "/f/data.csv", name: "data.csv", title: "成绩单" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    expect(await screen.findByText("小明")).toBeInTheDocument();
    expect(screen.getByText("98")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "源码" }));
    expect(screen.getByTestId("viewer-code-body")).toHaveTextContent("姓名,分数");

    // 7. Multi-item navigation: video error recovery -> audio -> unsupported zip
    rerender(
      <Viewer
        items={[
          { key: "v1", kind: "video", url: "/f/clip.mp4", name: "clip.mp4", title: "短片" },
          { key: "a1", kind: "audio", url: "/f/voice.mp3", name: "voice.mp3", title: "语音" },
          { key: "z1", kind: "document", url: "/f/pack.zip", name: "pack.zip", title: "压缩包" },
        ]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    const videoEl = document.querySelector("video")!;
    fireEvent.error(videoEl);
    expect(screen.getByRole("alert")).toHaveTextContent("视频无法播放");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(document.querySelector("video")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "下一个" }));
    expect(document.querySelector("audio")).not.toBeNull();

    fireEvent.keyDown(document, { key: "ArrowRight" });
    expect(screen.getByText(/当前格式暂不支持页内直接预览/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "上一个" }));
    expect(document.querySelector("audio")).not.toBeNull();
  });
});

describe("KnowledgeVault & CreationsView integration", () => {
  it("merges and clamps match highlight intervals cleanly in splitHighlightedText", () => {
    expect(splitHighlightedText("", [[0, 2]])).toEqual([]);
    expect(splitHighlightedText("Hello FTS5 world", [])).toEqual([{ text: "Hello FTS5 world", matched: false }]);
    expect(splitHighlightedText("SQLite FTS5 hybrid search", [[7, 11], [0, 6], [4, 9], [-5, 2], [20, 99]])).toEqual([
      { text: "SQLite FTS5", matched: true },
      { text: " hybrid s", matched: false },
      { text: "earch", matched: true },
    ]);
  });

  it("renders KnowledgeVault stats, filters collections, highlights FTS5 search hits, uploads, edits, and deletes", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/api/webui/companion/knowledge/upload") && init?.method === "POST") {
        const body = init.body as FormData;
        const fname = String(body.get("filename") ?? "guide.md");
        if (fname === "bad.exe") {
          return new Response(JSON.stringify({ error: "不支持的文件格式（.exe）" }), { status: 400 });
        }
        const dedup = fname === "dup.md";
        const addedDoc: KnowledgeDocument = {
          ...sampleDoc1,
          id: dedup ? "doc-1" : "doc-4",
          title: dedup ? "架构设计笔记" : "新指南",
          filename: fname,
          collection: "工作资料",
          chunk_count: 4,
          deduplicated: dedup,
        };
        return new Response(
          JSON.stringify({
            documents: dedup ? [addedDoc, sampleDoc2] : [addedDoc, sampleDoc1, sampleDoc2],
            collections: ["默认", "研发资料", "工作资料"],
            collection_counts: { 默认: 1, 研发资料: 1, 工作资料: 1 },
            stats: { doc_count: dedup ? 2 : 3, chunk_count: 14, total_bytes: 130_000 },
            added: addedDoc,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("q=SQLite")) {
        return new Response(
          JSON.stringify({
            ...sampleKnowledgePayload,
            hits: [
              {
                doc_id: "doc-1",
                title: "架构设计笔记",
                filename: "architecture.md",
                collection: "研发资料",
                chunk_index: 0,
                heading: "检索引擎",
                snippet: "基于 SQLite FTS5 的本地知识检索",
                content: "基于 SQLite FTS5 的本地知识检索，支持 trigram 与中文分词混合召回，完全离线运行。",
                score: 9.5,
                matches: [[3, 9], [10, 14]],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("q=NO_MATCH")) {
        return new Response(JSON.stringify({ ...sampleKnowledgePayload, hits: [] }), { status: 200 });
      }
      if (url.includes("collection=")) {
        return new Response(
          JSON.stringify({
            documents: [sampleDoc1],
            collections: ["默认", "研发资料"],
            collection_counts: { 默认: 2, 研发资料: 1 },
            stats: { doc_count: 1, chunk_count: 3, total_bytes: 4096 },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify(sampleKnowledgePayload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(<KnowledgeVault active />);

    // Initial stats & document list
    expect(await screen.findByText("架构设计笔记")).toBeInTheDocument();
    expect(screen.getByText("Q4 会议纪要")).toBeInTheDocument();
    expect(screen.getByText("本地笔记")).toBeInTheDocument();
    expect(screen.getByLabelText("知识库统计")).toHaveTextContent("11");

    // Collection filter capsule
    fireEvent.click(screen.getByRole("tab", { name: /研发资料/ }));
    await waitFor(() => expect(screen.queryByText("Q4 会议纪要")).toBeNull());
    fireEvent.click(screen.getByRole("tab", { name: /全部分组/ }));
    expect(await screen.findByText("Q4 会议纪要")).toBeInTheDocument();

    // Live FTS5 search with match highlights and chunk expand/collapse
    const searchBox = screen.getByRole("searchbox", { name: "检索知识库" });
    fireEvent.change(searchBox, { target: { value: "SQLite" } });
    fireEvent.submit(searchBox.closest("form")!);

    const snippet = await screen.findByTestId("knowledge-hit-snippet");
    const marks = snippet.querySelectorAll("mark");
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveTextContent("SQLite");
    expect(marks[1]).toHaveTextContent("FTS5");

    fireEvent.click(screen.getByRole("button", { name: "展开完整分块" }));
    expect(screen.getByText(/完全离线运行/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "收起原文" }));
    expect(screen.queryByText(/完全离线运行/)).toBeNull();

    // Empty search hit state
    fireEvent.change(searchBox, { target: { value: "NO_MATCH" } });
    fireEvent.submit(searchBox.closest("form")!);
    expect(await screen.findByText(/没有找到与「NO_MATCH」相关的知识片段/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清空检索" }));

    // Upload via drag-and-drop (enter -> leave -> over -> drop)
    fireEvent.change(screen.getByLabelText("目标分组"), { target: { value: "工作资料" } });
    const dropzone = screen.getByTestId("knowledge-dropzone");
    fireEvent.dragEnter(dropzone);
    expect(screen.getByText("松开即可将文件导入知识库")).toBeInTheDocument();
    fireEvent.dragLeave(dropzone);
    fireEvent.dragOver(dropzone);
    const file = new File(["# 新指南\n内容"], "guide.md", { type: "text/markdown" });
    fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });

    expect(await screen.findByText(/已导入「新指南」（4 个分块）/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/webui/companion/knowledge/upload",
      expect.objectContaining({ method: "POST" }),
    );

    // Upload via file input: deduplicated + error paths
    const fileInput = screen.getByLabelText("选择要上传的知识库文档") as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [new File(["same"], "dup.md")] } });
    expect(await screen.findByText(/已存在相同内容，已更新「架构设计笔记」/)).toBeInTheDocument();

    fireEvent.change(fileInput, { target: { files: [new File(["bin"], "bad.exe")] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("不支持的文件格式（.exe）");

    // Inline edit: cancel, error, and success
    fireEvent.click(screen.getByRole("button", { name: "编辑「架构设计笔记」" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByLabelText("编辑文档标题")).toBeNull();

    requestMutation.mockRejectedValueOnce(new Error("分组中已存在相同内容的文档")).mockResolvedValueOnce({
      documents: [{ ...sampleDoc1, title: "架构总览 v2", collection: "核心文档" }, sampleDoc2],
      collections: ["默认", "核心文档"],
      collection_counts: { 默认: 1, 核心文档: 1 },
      stats: sampleKnowledgePayload.stats,
      document: { ...sampleDoc1, title: "架构总览 v2", collection: "核心文档" },
    });
    fireEvent.click(screen.getByRole("button", { name: "编辑「架构设计笔记」" }));
    fireEvent.change(screen.getByLabelText("编辑文档标题"), { target: { value: "架构总览 v2" } });
    fireEvent.change(screen.getByLabelText("编辑文档分组"), { target: { value: "核心文档" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("分组中已存在相同内容的文档")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
        action: "update",
        id: "doc-1",
        title: "架构总览 v2",
        collection: "核心文档",
      }),
    );
    expect(await screen.findByText("架构总览 v2")).toBeInTheDocument();

    // Two-step delete confirmation: cancel first, then confirm
    fireEvent.click(screen.getByRole("button", { name: "删除「架构总览 v2」" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("button", { name: "确认删除" })).toBeNull();

    requestMutation.mockResolvedValueOnce({
      documents: [sampleDoc2],
      collections: ["默认"],
      collection_counts: { 默认: 1 },
      stats: { doc_count: 1, chunk_count: 7, total_bytes: 120 * 1024 },
    });
    fireEvent.click(screen.getByRole("button", { name: "删除「架构总览 v2」" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
        action: "delete",
        id: "doc-1",
      }),
    );
    expect(screen.queryByText("架构总览 v2")).toBeNull();
  });

  it("handles KnowledgeVault fetch errors, retry, and empty state cleanly", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "数据库暂不可用" }), { status: 503 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            documents: [],
            collections: [],
            collection_counts: {},
            stats: { doc_count: 0, chunk_count: 0, total_bytes: 0 },
          }),
          { status: 200 },
        ),
      );

    render(<KnowledgeVault active />);
    expect(await screen.findByRole("alert")).toHaveTextContent("数据库暂不可用");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("知识库还没有文档")).toBeInTheDocument();
  });

  it("integrates 作品归档 | 知识库 switcher and one-click 加入知识库 button inside CreationsView", async () => {
    expect(formatSize(500)).toBe("500 B");
    expect(formatSize(2048)).toBe("2 KB");
    expect(formatSize(3 * 1024 * 1024)).toBe("3.0 MB");

    const creationsPayload: CreationsPayload = {
      items: [
        {
          id: "文档/方案.md",
          kind: "document",
          name: "方案.md",
          title: "产品方案",
          origin: "canvas",
          prompt: null,
          size: 1024,
          created_at: new Date().toISOString(),
          url: "/f/文档/方案.md",
        },
      ],
      counts: { image: 0, video: 0, document: 1, audio: 0 },
    };

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/webui/companion/creations")) {
        return new Response(JSON.stringify(creationsPayload), { status: 200 });
      }
      return new Response(JSON.stringify(sampleKnowledgePayload), { status: 200 });
    });

    requestMutation
      .mockRejectedValueOnce(new Error("导入失败"))
      .mockResolvedValueOnce({
        ...sampleKnowledgePayload,
        added: { ...sampleDoc1, title: "产品方案", chunk_count: 2, deduplicated: false },
      });

    render(<CreationsView active />);
    expect(screen.getByRole("tab", { name: "作品归档" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("产品方案")).toBeInTheDocument();

    // First import fails -> alert shown
    fireEvent.click(screen.getByRole("button", { name: "将「产品方案」加入知识库" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("导入知识库失败：导入失败");

    // Retry succeeds -> notice + 去查看
    fireEvent.click(screen.getByRole("button", { name: "将「产品方案」加入知识库" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
        action: "import_creation",
        creation_id: "文档/方案.md",
      }),
    );
    expect(await screen.findByText(/已将「产品方案」加入知识库（2 个分块）/)).toBeInTheDocument();

    // Jump straight to 知识库 tab via 去查看, then back to 作品归档
    fireEvent.click(screen.getByRole("button", { name: "去查看" }));
    expect(screen.getByRole("tab", { name: "知识库" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("knowledge-vault")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "作品归档" }));
    expect(screen.getByRole("tab", { name: "作品归档" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("companion-api artifact and knowledge client functions", () => {
  it("calls WS mutations and HTTP endpoints with exact backend contract fields and surfaces API errors", async () => {
    const fakeClient = { requestMutation } as unknown as NanobotClient;
    requestMutation.mockResolvedValue({ ok: true });

    await saveCreationArtifact(fakeClient, { title: "Demo", content: "<h1>Hi</h1>", format: "html", prompt: "make a page" });
    expect(requestMutation).toHaveBeenCalledWith("companion.creations.save", {
      title: "Demo",
      content: "<h1>Hi</h1>",
      format: "html",
      prompt: "make a page",
    });

    await updateKnowledgeDocument(fakeClient, "d1", { title: "新名", collection: "研发" }, "研发");
    expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
      action: "update",
      id: "d1",
      title: "新名",
      collection: "研发",
      list_collection: "研发",
    });

    await deleteKnowledgeDocument(fakeClient, "d1", "默认");
    expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
      action: "delete",
      id: "d1",
      list_collection: "默认",
    });

    await importCreationToKnowledge(fakeClient, "文档/a.md", { collection: "技术", listCollection: "技术" });
    expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
      action: "import_creation",
      creation_id: "文档/a.md",
      collection: "技术",
      list_collection: "技术",
    });

    await searchKnowledge(fakeClient, "FTS5", { collection: "默认", limit: 8 });
    expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
      action: "search",
      query: "FTS5",
      collection: "默认",
      limit: 8,
    });

    // HTTP error parsing
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ error: "文件大小超过 20 MB 上限" }), { status: 413 }),
    );
    await expect(uploadKnowledgeFile("tok", new File(["x"], "huge.pdf"))).rejects.toThrow("文件大小超过 20 MB 上限");

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "unauthorized" }), { status: 401 }),
    );
    await expect(fetchKnowledge("bad-tok", { query: "abc", collection: "默认" })).rejects.toThrow("unauthorized");

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("plain error", { status: 500 }));
    await expect(fetchKnowledge("tok")).rejects.toThrow("plain error");
  });
});
