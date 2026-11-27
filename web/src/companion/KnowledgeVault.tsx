import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from "react";
import {
  AlertCircle,
  BookOpen,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  FolderOpen,
  Loader2,
  MessageSquareQuote,
  Pencil,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { copyTextToClipboard } from "@/lib/clipboard";
import { useClient } from "@/providers/ClientProvider";

import {
  deleteKnowledgeDocument,
  fetchKnowledge,
  readKnowledgeDocument,
  updateKnowledgeDocument,
  uploadKnowledgeFile,
  type KnowledgeDocument,
  type KnowledgeDocumentDetail,
  type KnowledgePayload,
  type KnowledgeSearchHit,
} from "./companion-api";
import { fileBadge, fileSize } from "./fileKinds";
import { MuseSheet } from "./muse/Sheet";
import { CompanionPet } from "./Pet";
import { dayLabel } from "./presentation";

const ACCEPTED_FILE_TYPES =
  ".pdf,.docx,.xlsx,.pptx,.odt,.md,.markdown,.mdx,.txt,.csv,.tsv,.json,.jsonl,.yaml,.yml,.html,.htm,.xhtml,.xml,.svg,.mmd,.mermaid,.rst,.log,.ini,.toml,.conf,.cfg,.py,.ts,.tsx,.js,.jsx,.mjs,.cjs,.swift,.java,.kt,.go,.rs,.sh,.bash,.zsh,.css,.scss,.sql,.c,.h,.cpp,.hpp,.rb,.php";

const SEARCH_DEBOUNCE_MS = 220;

export interface HighlightSegment {
  text: string;
  matched: boolean;
}

/**
 * Split `text` into plain / highlighted segments using backend `[start, end]` character index ranges,
 * safely clamping out-of-bounds values and merging overlapping spans.
 */
export function splitHighlightedText(text: string, matches: [number, number][] = []): HighlightSegment[] {
  if (!text) return [];
  const len = text.length;
  const normalized: [number, number][] = [];
  for (const pair of matches) {
    if (!Array.isArray(pair) || pair.length < 2) continue;
    const start = Math.max(0, Math.min(len, Math.floor(Number(pair[0]))));
    const end = Math.max(0, Math.min(len, Math.floor(Number(pair[1]))));
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
      normalized.push([start, end]);
    }
  }
  if (normalized.length === 0) return [{ text, matched: false }];

  normalized.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: [number, number][] = [normalized[0]];
  for (let i = 1; i < normalized.length; i += 1) {
    const prev = merged[merged.length - 1];
    const curr = normalized[i];
    if (curr[0] <= prev[1]) {
      prev[1] = Math.max(prev[1], curr[1]);
    } else {
      merged.push([curr[0], curr[1]]);
    }
  }

  const segments: HighlightSegment[] = [];
  let cursor = 0;
  for (const [start, end] of merged) {
    if (start > cursor) {
      segments.push({ text: text.slice(cursor, start), matched: false });
    }
    segments.push({ text: text.slice(start, end), matched: true });
    cursor = end;
  }
  if (cursor < len) {
    segments.push({ text: text.slice(cursor), matched: false });
  }
  return segments;
}

function originLabel(origin: string): string {
  if (origin === "creation" || origin.startsWith("creation:")) return "作品导入";
  if (origin === "local") return "本地导入";
  return "手动上传";
}

function formatKnowledgeQuote(title: string, heading: string, content: string): string {
  const header = heading ? `[知识库 · ${title} / ${heading}]` : `[知识库 · ${title}]`;
  return `${header}\n${content.trim().slice(0, 600)}`;
}

/**
 * Personal Knowledge Vault: FTS5 hybrid search with match highlights, collection capsules,
 * click/drag-and-drop upload, full chunk reader, quote-to-ask handoff, inline editing, and deletion.
 */
export function KnowledgeVault({
  active,
  onQuoteToAsk,
}: {
  active: boolean;
  onQuoteToAsk?: (quote: string, prompt?: string) => void;
}) {
  const { client, token } = useClient();
  const [payload, setPayload] = useState<KnowledgePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [collection, setCollection] = useState<string>("");
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);

  const [uploadCollection, setUploadCollection] = useState("");
  const [dragging, setDragging] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editCollection, setEditCollection] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [expandedHits, setExpandedHits] = useState<ReadonlySet<string>>(new Set());
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const [readerDoc, setReaderDoc] = useState<KnowledgeDocument | null>(null);
  const [readerDetail, setReaderDetail] = useState<KnowledgeDocumentDetail | null>(null);
  const [readerLoading, setReaderLoading] = useState(false);
  const [readerError, setReaderError] = useState<string | null>(null);

  const openDocumentReader = useCallback(
    async (doc: KnowledgeDocument) => {
      setReaderDoc(doc);
      setReaderDetail(null);
      setReaderLoading(true);
      setReaderError(null);
      try {
        const detail = await readKnowledgeDocument(client, doc.id);
        setReaderDetail(detail);
      } catch (err: unknown) {
        setReaderError(err instanceof Error ? err.message : String(err));
      } finally {
        setReaderLoading(false);
      }
    },
    [client],
  );

  const copySnippet = useCallback((key: string, text: string) => {
    void copyTextToClipboard(text).then((ok) => {
      if (!ok) return;
      setCopiedKey(key);
      window.setTimeout(() => setCopiedKey((curr) => (curr === key ? null : curr)), 1_500);
    });
  }, []);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const runFetch = useCallback(
    (q: string, col: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const isSearch = Boolean(q.trim());
      if (isSearch) setSearching(true);
      else setLoading((prev) => prev || payload === null);

      fetchKnowledge(token, {
        query: q.trim() || undefined,
        collection: col || undefined,
        signal: controller.signal,
      })
        .then((next) => {
          if (controller.signal.aborted) return;
          setPayload(next);
          setError(null);
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return;
          setError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          if (controller.signal.aborted) return;
          setLoading(false);
          setSearching(false);
        });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [token],
  );

  // Load on activation or collection switch; debounce when typing search query.
  useEffect(() => {
    if (!active) return;
    const delay = query.trim() ? SEARCH_DEBOUNCE_MS : 0;
    const timer = window.setTimeout(() => runFetch(query, collection), delay);
    return () => window.clearTimeout(timer);
  }, [active, collection, query, runFetch]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const totalDocsAll = useMemo(() => {
    if (!payload) return 0;
    if (payload.collection_counts) {
      return Object.values(payload.collection_counts).reduce((sum, n) => sum + n, 0);
    }
    return payload.stats.doc_count;
  }, [payload]);

  const handleFilesUpload = useCallback(
    async (fileList: FileList | File[]) => {
      const files = Array.from(fileList);
      if (files.length === 0 || uploading) return;
      setUploading(true);
      setUploadError(null);
      const targetCol = uploadCollection.trim() || collection || undefined;

      let lastSummary = "";
      try {
        for (let i = 0; i < files.length; i += 1) {
          const file = files[i];
          setUploadStatus(
            files.length > 1
              ? `正在上传并切分索引 (${i + 1}/${files.length})：${file.name}…`
              : `正在上传并切分索引：${file.name}…`,
          );
          const result = await uploadKnowledgeFile(token, file, { collection: targetCol });
          const { added } = result;
          // Refresh filtered view if a different collection filter is active.
          if (collection && added.collection !== collection) {
            runFetch(query, collection);
          } else {
            setPayload(result);
          }
          lastSummary = added.deduplicated
            ? `已存在相同内容，已更新「${added.title}」`
            : `已导入「${added.title}」（${added.chunk_count} 个分块）`;
        }
        setUploadStatus(files.length > 1 ? `已完成 ${files.length} 个文档入库 · ${lastSummary}` : lastSummary);
      } catch (err: unknown) {
        setUploadStatus(null);
        setUploadError(err instanceof Error ? err.message : String(err));
      } finally {
        setUploading(false);
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [collection, query, runFetch, token, uploadCollection, uploading],
  );

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(false);
      if (event.dataTransfer?.files?.length) {
        void handleFilesUpload(event.dataTransfer.files);
      }
    },
    [handleFilesUpload],
  );

  const startEdit = (doc: KnowledgeDocument) => {
    setEditingId(doc.id);
    setEditTitle(doc.title);
    setEditCollection(doc.collection);
    setEditError(null);
    setConfirmDeleteId(null);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditError(null);
  };

  const submitEdit = async (event: FormEvent, docId: string) => {
    event.preventDefault();
    if (savingEdit) return;
    setSavingEdit(true);
    setEditError(null);
    try {
      const next = await updateKnowledgeDocument(
        client,
        docId,
        { title: editTitle.trim(), collection: editCollection.trim() },
        collection || undefined,
      );
      setPayload(next);
      setEditingId(null);
    } catch (err: unknown) {
      setEditError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingEdit(false);
    }
  };

  const handleDelete = async (docId: string) => {
    if (deletingId) return;
    setDeletingId(docId);
    setError(null);
    try {
      const next = await deleteKnowledgeDocument(client, docId, collection || undefined);
      setPayload(next);
      setConfirmDeleteId(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingId(null);
    }
  };

  const toggleHitExpand = (hitKey: string) => {
    setExpandedHits((prev) => {
      const next = new Set(prev);
      if (next.has(hitKey)) next.delete(hitKey);
      else next.add(hitKey);
      return next;
    });
  };

  const hits = payload?.hits;
  const trimmedQuery = query.trim();

  return (
    <div className="flex flex-col gap-4 px-4 pb-4" data-testid="knowledge-vault">
      {/* Stats Strip */}
      <div className="grid grid-cols-3 gap-2" aria-label="知识库统计">
        <StatCard label="文档数" value={String(payload?.stats.doc_count ?? 0)} />
        <StatCard label="知识分块" value={String(payload?.stats.chunk_count ?? 0)} />
        <StatCard label="总大小" value={fileSize(payload?.stats.total_bytes ?? 0)} />
      </div>

      {/* Live FTS5 Search Box */}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          runFetch(query, collection);
        }}
        className="relative flex items-center"
      >
        <Search className="c-muted pointer-events-none absolute left-3.5 h-4 w-4" aria-hidden="true" />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="检索知识库"
          placeholder="实时检索知识片段、标题或关键词…"
          className="c-card min-h-11 w-full rounded-full pl-10 pr-10 text-[14px] outline-none placeholder:opacity-60 focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]"
        />
        {searching ? (
          <Loader2
            className="c-muted pointer-events-none absolute right-3.5 h-4 w-4 animate-spin motion-reduce:animate-none"
            aria-hidden="true"
          />
        ) : query ? (
          <button
            type="button"
            onClick={() => setQuery("")}
            aria-label="清空检索"
            className="c-muted absolute right-1.5 grid h-8 w-8 place-items-center rounded-full hover:opacity-80"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        ) : null}
      </form>

      {/* Collection Filter Capsules */}
      {payload && payload.collections.length > 0 && (
        <div className="flex gap-2 overflow-x-auto pb-0.5" role="tablist" aria-label="按分组筛选">
          <button
            type="button"
            role="tab"
            aria-selected={collection === ""}
            onClick={() => setCollection("")}
            className="c-pressable min-h-9 shrink-0 rounded-full px-3.5 text-[13px] font-medium"
            style={
              collection === ""
                ? { background: "var(--c-text)", color: "var(--c-bg)" }
                : { background: "var(--c-segment)", color: "var(--c-text)" }
            }
          >
            全部分组
            {totalDocsAll > 0 && <span className="ml-1 opacity-60">{totalDocsAll}</span>}
          </button>
          {payload.collections.map((col) => {
            const selected = collection === col;
            const cnt = payload.collection_counts?.[col];
            return (
              <button
                key={col}
                type="button"
                role="tab"
                aria-selected={selected}
                onClick={() => setCollection(col)}
                className="c-pressable inline-flex min-h-9 shrink-0 items-center gap-1 rounded-full px-3.5 text-[13px] font-medium"
                style={
                  selected
                    ? { background: "var(--c-text)", color: "var(--c-bg)" }
                    : { background: "var(--c-segment)", color: "var(--c-text)" }
                }
              >
                <FolderOpen className="h-3.5 w-3.5 opacity-75" aria-hidden="true" />
                {col}
                {typeof cnt === "number" && cnt > 0 && <span className="ml-0.5 opacity-60">{cnt}</span>}
              </button>
            );
          })}
        </div>
      )}

      {/* Upload Dropzone */}
      <div
        data-testid="knowledge-dropzone"
        onDragEnter={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(e) => {
          e.preventDefault();
          if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
          setDragging(false);
        }}
        onDrop={handleDrop}
        className="c-card flex flex-col gap-2.5 rounded-2xl border-dashed p-3.5 transition-colors"
        style={{
          borderWidth: "1.5px",
          borderStyle: "dashed",
          borderColor: dragging ? "var(--c-primary)" : "var(--c-border)",
          background: dragging ? "var(--c-accent-soft)" : undefined,
        }}
      >
        <div className="flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
            className="c-primary inline-flex min-h-10 items-center gap-1.5 rounded-full px-4 text-[13.5px] font-semibold disabled:opacity-50"
          >
            {uploading ? (
              <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <Upload className="h-4 w-4" aria-hidden="true" />
            )}
            {uploading ? "上传中…" : "上传文档入库"}
          </button>

          <input
            type="text"
            value={uploadCollection}
            onChange={(e) => setUploadCollection(e.target.value)}
            list="kb-collection-suggestions"
            aria-label="目标分组"
            placeholder={collection ? `分组：${collection}` : "分组（默认）"}
            className="min-h-10 min-w-[120px] flex-1 rounded-full px-3.5 text-[13px] outline-none placeholder:opacity-60"
            style={{ background: "var(--c-fill)" }}
          />
          <datalist id="kb-collection-suggestions">
            {(payload?.collections ?? ["默认"]).map((col) => (
              <option key={col} value={col} />
            ))}
          </datalist>

          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPTED_FILE_TYPES}
            aria-label="选择要上传的知识库文档"
            className="hidden"
            onChange={(e) => {
              if (e.target.files?.length) void handleFilesUpload(e.target.files);
            }}
          />
        </div>
        <p className="c-muted text-[12px] leading-snug">
          {dragging
            ? "松开即可将文件导入知识库"
            : "支持拖拽或点击上传 Markdown、PDF、Word、Excel、PPT、HTML 及常见代码文本（单文件上限 20 MB）"}
        </p>

        {uploadStatus && (
          <div className="flex items-center gap-2 rounded-xl px-3 py-2 text-[12.5px]" style={{ background: "var(--c-fill)" }} role="status">
            {uploading ? (
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <Check className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--c-online)" }} aria-hidden="true" />
            )}
            <span className="min-w-0 flex-1">{uploadStatus}</span>
          </div>
        )}
        {uploadError && (
          <div className="c-danger flex items-center gap-2 rounded-xl bg-destructive/10 px-3 py-2 text-[12.5px]" role="alert">
            <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1">{uploadError}</span>
          </div>
        )}
      </div>

      {/* Fetch / Mutation Error */}
      {error && (
        <div className="c-card flex items-center gap-3 rounded-2xl px-4 py-3" role="alert">
          <AlertCircle className="c-danger h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="c-danger min-w-0 flex-1 text-[13px]">{error}</span>
          <button
            type="button"
            onClick={() => runFetch(query, collection)}
            className="c-pressable rounded-full px-3 py-1 text-[12.5px] font-semibold"
            style={{ background: "var(--c-fill)" }}
          >
            重试
          </button>
        </div>
      )}

      {/* Search Hits Section */}
      {trimmedQuery && (
        <section aria-label="检索命中片段" className="flex flex-col gap-2">
          <div className="flex items-center justify-between px-1">
            <h3 className="c-muted text-[13px] font-medium">
              命中片段{hits ? `（${hits.length}）` : ""}
            </h3>
          </div>
          {searching && !hits ? (
            <div className="c-card flex items-center justify-center gap-2 rounded-2xl py-6 text-[13px] c-muted" role="status">
              <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              正在检索知识库…
            </div>
          ) : !hits || hits.length === 0 ? (
            <div className="c-card rounded-2xl px-4 py-6 text-center text-[13.5px] c-muted">
              没有找到与「{trimmedQuery}」相关的知识片段
            </div>
          ) : (
            <ul className="flex flex-col gap-2">
              {hits.map((hit) => {
                const hitKey = `${hit.doc_id}:${hit.chunk_index}`;
                const expanded = expandedHits.has(hitKey);
                return (
                  <KnowledgeHitCard
                    key={hitKey}
                    hit={hit}
                    expanded={expanded}
                    copied={copiedKey === `hit:${hitKey}`}
                    onToggleExpand={() => toggleHitExpand(hitKey)}
                    onCopy={() => copySnippet(`hit:${hitKey}`, hit.content || hit.snippet)}
                    onQuoteToAsk={
                      onQuoteToAsk
                        ? () =>
                            onQuoteToAsk(
                              formatKnowledgeQuote(hit.title, hit.heading, hit.content || hit.snippet),
                              `结合「${hit.title}」的这段内容，`,
                            )
                        : undefined
                    }
                  />
                );
              })}
            </ul>
          )}
        </section>
      )}

      {/* Document List */}
      <section aria-label="知识库文档列表" className="flex flex-col gap-2">
        <h3 className="c-muted px-1 text-[13px] font-medium">
          {collection ? `分组「${collection}」` : "全部文档"}
        </h3>

        {loading && !payload ? (
          <div className="c-card flex items-center justify-center gap-2 rounded-2xl py-8 text-[13.5px] c-muted" role="status">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            正在读取知识库…
          </div>
        ) : !payload || payload.documents.length === 0 ? (
          <div className="flex flex-col items-center px-6 py-8 text-center">
            <CompanionPet size={56} />
            <p className="mt-3 text-[15.5px] font-medium">知识库还没有文档</p>
            <p className="c-muted mt-1 max-w-xs text-[13px] leading-relaxed">
              上传常用文档、笔记或代码，或者从「作品归档」一键导入，聊天时伙伴会自动检索参考。
            </p>
          </div>
        ) : (
          <ul className="c-card overflow-hidden rounded-2xl">
            {payload.documents.map((doc) => {
              const isEditing = editingId === doc.id;
              const isConfirmingDelete = confirmDeleteId === doc.id;
              const isDeleting = deletingId === doc.id;
              return (
                <li
                  key={doc.id}
                  className="border-b px-3.5 py-3 last:border-b-0"
                  style={{ borderColor: "var(--c-border)" }}
                >
                  {isEditing ? (
                    <form onSubmit={(e) => void submitEdit(e, doc.id)} className="flex flex-col gap-2">
                      <div className="flex flex-col gap-2 sm:flex-row">
                        <label className="flex min-w-0 flex-[1.5] flex-col gap-1">
                          <span className="c-muted text-[11.5px]">文档标题</span>
                          <input
                            type="text"
                            value={editTitle}
                            onChange={(e) => setEditTitle(e.target.value)}
                            aria-label="编辑文档标题"
                            required
                            className="min-h-9 rounded-xl px-3 text-[13.5px] outline-none"
                            style={{ background: "var(--c-fill)" }}
                          />
                        </label>
                        <label className="flex min-w-0 flex-1 flex-col gap-1">
                          <span className="c-muted text-[11.5px]">分组</span>
                          <input
                            type="text"
                            value={editCollection}
                            onChange={(e) => setEditCollection(e.target.value)}
                            list="kb-collection-suggestions"
                            aria-label="编辑文档分组"
                            required
                            className="min-h-9 rounded-xl px-3 text-[13.5px] outline-none"
                            style={{ background: "var(--c-fill)" }}
                          />
                        </label>
                      </div>
                      {editError && (
                        <p className="c-danger text-[12px]" role="alert">
                          {editError}
                        </p>
                      )}
                      <div className="flex items-center justify-end gap-2">
                        <button
                          type="button"
                          onClick={cancelEdit}
                          className="c-pressable min-h-8 rounded-full px-3 text-[12.5px]"
                          style={{ background: "var(--c-fill)" }}
                        >
                          取消
                        </button>
                        <button
                          type="submit"
                          disabled={savingEdit || !editTitle.trim() || !editCollection.trim()}
                          className="c-primary min-h-8 rounded-full px-3.5 text-[12.5px] font-semibold disabled:opacity-50"
                        >
                          {savingEdit ? "保存中…" : "保存"}
                        </button>
                      </div>
                    </form>
                  ) : (
                    <div className="flex items-start gap-3">
                      <button
                        type="button"
                        onClick={() => void openDocumentReader(doc)}
                        aria-label={`阅读「${doc.title}」全部章节分块`}
                        className="c-pressable flex min-w-0 flex-1 items-start gap-3 rounded-xl text-left"
                      >
                        <span
                          className="mt-0.5 grid h-10 w-10 shrink-0 place-items-center rounded-xl font-mono text-[10px] font-bold"
                          style={{ background: "var(--c-accent-soft)", color: "var(--c-primary)" }}
                          aria-hidden="true"
                        >
                          {fileBadge(doc.filename)}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="truncate text-[14.5px] font-medium">{doc.title}</span>
                            <span
                              className="rounded-full px-2 py-0.5 text-[11px] font-medium"
                              style={{ background: "var(--c-fill)", color: "var(--c-text-muted)" }}
                            >
                              {doc.collection}
                            </span>
                          </div>
                          <p className="c-muted mt-0.5 truncate text-[12px]">
                            {doc.filename} · {doc.chunk_count} 分块 · {fileSize(doc.size_bytes)} · {originLabel(doc.origin)} ·{" "}
                            {dayLabel(Math.round(doc.updated_at * 1000))}
                          </p>
                        </div>
                      </button>

                      {isConfirmingDelete ? (
                        <div className="flex shrink-0 items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => setConfirmDeleteId(null)}
                            className="c-pressable min-h-8 rounded-full px-2.5 text-[12px]"
                            style={{ background: "var(--c-fill)" }}
                          >
                            取消
                          </button>
                          <button
                            type="button"
                            disabled={isDeleting}
                            onClick={() => void handleDelete(doc.id)}
                            className="min-h-8 rounded-full px-3 text-[12px] font-semibold text-white disabled:opacity-50"
                            style={{ background: "var(--c-danger-fill)" }}
                          >
                            {isDeleting ? "删除中…" : "确认删除"}
                          </button>
                        </div>
                      ) : (
                        <div className="flex shrink-0 items-center gap-1">
                          <button
                            type="button"
                            onClick={() => startEdit(doc)}
                            aria-label={`编辑「${doc.title}」`}
                            title="编辑标题与分组"
                            className="c-pressable c-muted grid h-9 w-9 place-items-center rounded-full hover:text-om-fg"
                          >
                            <Pencil className="h-4 w-4" aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setConfirmDeleteId(doc.id);
                              setEditingId(null);
                            }}
                            aria-label={`删除「${doc.title}」`}
                            title="删除文档"
                            className="c-pressable c-muted grid h-9 w-9 place-items-center rounded-full hover:c-danger"
                          >
                            <Trash2 className="h-4 w-4" aria-hidden="true" />
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <MuseSheet
        open={readerDoc !== null}
        onClose={() => setReaderDoc(null)}
        title={readerDoc?.title ?? "知识库文档"}
      >
        {readerDoc && (
          <div className="flex flex-col gap-3 pb-3" data-testid="knowledge-reader-sheet">
            <div className="c-muted flex flex-wrap items-center gap-1.5 text-[12px]">
              <span className="rounded-full px-2 py-0.5 font-medium" style={{ background: "var(--c-fill)" }}>
                {readerDoc.collection}
              </span>
              <span>{readerDoc.filename}</span>
              <span>· {readerDetail?.chunks.length ?? readerDoc.chunk_count} 个分块</span>
              <span>· {fileSize(readerDoc.size_bytes)}</span>
            </div>

            {readerLoading ? (
              <div className="flex items-center justify-center gap-2 py-10 text-[13.5px] c-muted" role="status">
                <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                正在加载全文分块…
              </div>
            ) : readerError ? (
              <div className="c-card flex items-center gap-2 rounded-2xl p-3.5 text-[13px] c-danger" role="alert">
                <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
                <span className="min-w-0 flex-1">读取分块失败：{readerError}</span>
                <button
                  type="button"
                  onClick={() => void openDocumentReader(readerDoc)}
                  className="c-pressable rounded-full px-3 py-1 text-[12px] font-semibold"
                  style={{ background: "var(--c-fill)", color: "var(--c-text)" }}
                >
                  重试
                </button>
              </div>
            ) : !readerDetail || readerDetail.chunks.length === 0 ? (
              <p className="c-muted py-8 text-center text-[13.5px]">该文档暂无可读分块</p>
            ) : (
              <ul className="flex flex-col gap-2.5">
                {readerDetail.chunks.map((chunk) => {
                  const key = `chunk:${readerDoc.id}:${chunk.chunk_index}`;
                  const isCopied = copiedKey === key;
                  return (
                    <li
                      key={chunk.chunk_index}
                      data-testid="knowledge-reader-chunk"
                      className="c-card flex flex-col gap-2 rounded-2xl p-3.5"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="min-w-0 truncate text-[13px] font-semibold">
                          {chunk.heading || `第 ${chunk.chunk_index + 1} 段`}
                        </span>
                        <span className="c-muted shrink-0 text-[11px] tabular-nums">
                          分块 #{chunk.chunk_index + 1}
                        </span>
                      </div>
                      <p className="whitespace-pre-wrap break-words text-[13.5px] leading-relaxed">
                        {chunk.content}
                      </p>
                      <div className="flex flex-wrap items-center gap-2 pt-0.5">
                        <button
                          type="button"
                          onClick={() => copySnippet(key, chunk.content)}
                          className="c-pressable inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12px] font-medium"
                          style={{ background: "var(--c-fill)" }}
                        >
                          {isCopied ? (
                            <Check className="h-3.5 w-3.5" aria-hidden="true" />
                          ) : (
                            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
                          )}
                          {isCopied ? "已复制" : "复制片段"}
                        </button>
                        {onQuoteToAsk && (
                          <button
                            type="button"
                            onClick={() => {
                              const quote = formatKnowledgeQuote(readerDoc.title, chunk.heading, chunk.content);
                              setReaderDoc(null);
                              onQuoteToAsk(quote, `结合「${readerDoc.title}」的这段内容，`);
                            }}
                            className="c-primary inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12px] font-semibold"
                          >
                            <MessageSquareQuote className="h-3.5 w-3.5" aria-hidden="true" />
                            引用去提问
                          </button>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </MuseSheet>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="c-card flex flex-col items-center justify-center rounded-2xl px-3 py-2.5 text-center">
      <span className="text-[17px] font-bold tabular-nums leading-tight">{value}</span>
      <span className="c-muted mt-0.5 text-[11.5px]">{label}</span>
    </div>
  );
}

function KnowledgeHitCard({
  hit,
  expanded,
  copied,
  onToggleExpand,
  onCopy,
  onQuoteToAsk,
}: {
  hit: KnowledgeSearchHit;
  expanded: boolean;
  copied: boolean;
  onToggleExpand: () => void;
  onCopy: () => void;
  onQuoteToAsk?: () => void;
}) {
  const segments = useMemo(() => splitHighlightedText(hit.snippet, hit.matches), [hit.snippet, hit.matches]);
  const hasMore = hit.content.length > hit.snippet.length;

  return (
    <li className="c-card flex flex-col gap-1.5 rounded-2xl px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-1.5 text-[12.5px]">
        <BookOpen className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
        <span className="font-semibold">{hit.title}</span>
        <span
          className="rounded-full px-2 py-0.5 text-[11px]"
          style={{ background: "var(--c-fill)", color: "var(--c-text-muted)" }}
        >
          {hit.collection}
        </span>
        {hit.heading && <span className="c-muted truncate text-[11.5px]">· {hit.heading}</span>}
        <span className="c-muted ml-auto text-[11px] tabular-nums">片段 #{hit.chunk_index + 1}</span>
      </div>

      <p className="text-[13.5px] leading-relaxed" data-testid="knowledge-hit-snippet">
        {segments.map((seg, idx) =>
          seg.matched ? (
            <mark
              key={idx}
              className="rounded px-0.5 font-semibold"
              style={{ background: "var(--c-accent-soft)", color: "var(--c-primary)" }}
            >
              {seg.text}
            </mark>
          ) : (
            <span key={idx}>{seg.text}</span>
          ),
        )}
      </p>

      {hasMore && (
        <div>
          <button
            type="button"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            className="c-muted inline-flex items-center gap-1 text-[12px] font-medium hover:underline"
          >
            {expanded ? (
              <>
                <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
                收起原文
              </>
            ) : (
              <>
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
                展开完整分块
              </>
            )}
          </button>
          {expanded && (
            <pre
              className="mt-1.5 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-xl p-2.5 font-sans text-[12.5px] leading-relaxed"
              style={{ background: "var(--c-fill)" }}
            >
              {hit.content}
            </pre>
          )}
        </div>
      )}

      <div className="mt-0.5 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onCopy}
          className="c-pressable inline-flex min-h-8 items-center gap-1 rounded-full px-2.5 text-[12px] font-medium"
          style={{ background: "var(--c-fill)" }}
        >
          {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
          {copied ? "已复制" : "复制片段"}
        </button>
        {onQuoteToAsk && (
          <button
            type="button"
            onClick={onQuoteToAsk}
            className="c-pressable inline-flex min-h-8 items-center gap-1 rounded-full px-2.5 text-[12px] font-semibold"
            style={{ background: "var(--c-accent-soft)", color: "var(--c-primary)" }}
          >
            <MessageSquareQuote className="h-3.5 w-3.5" aria-hidden="true" />
            引用去提问
          </button>
        )}
      </div>
    </li>
  );
}
