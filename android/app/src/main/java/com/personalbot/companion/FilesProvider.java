package com.personalbot.companion;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import android.webkit.MimeTypeMap;

import java.io.File;
import java.io.FileNotFoundException;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * The app's own files, lent to other apps one at a time: the camera writes a photo into
 * {@code captures/}, and a file shared or opened from the chat is handed out of {@code shared/}.
 * Both live in the app's cache; nothing here is reachable without a granted URI.
 */
public class FilesProvider extends ContentProvider {
    static final String AUTHORITY = "com.personalbot.companion.files";
    static final String CAPTURES = "captures";
    static final String SHARED = "shared";
    /** Older files are cleared whenever a new one is made. */
    private static final long KEEP_MS = 24L * 60 * 60 * 1000;

    private static final Map<String, String> EXTRA_EXT_TO_MIME;
    private static final Map<String, String> EXTRA_MIME_TO_EXT;

    static {
        Map<String, String> extToMime = new HashMap<>();
        extToMime.put("md", "text/markdown");
        extToMime.put("markdown", "text/markdown");
        extToMime.put("txt", "text/plain");
        extToMime.put("log", "text/plain");
        extToMime.put("csv", "text/csv");
        extToMime.put("tsv", "text/tab-separated-values");
        extToMime.put("json", "application/json");
        extToMime.put("jsonl", "application/json");
        extToMime.put("yaml", "text/yaml");
        extToMime.put("yml", "text/yaml");
        extToMime.put("toml", "text/plain");
        extToMime.put("py", "text/x-python");
        extToMime.put("js", "text/javascript");
        extToMime.put("mjs", "text/javascript");
        extToMime.put("ts", "text/plain");
        extToMime.put("tsx", "text/plain");
        extToMime.put("jsx", "text/plain");
        extToMime.put("sh", "text/x-shellscript");
        extToMime.put("html", "text/html");
        extToMime.put("htm", "text/html");
        extToMime.put("pdf", "application/pdf");
        extToMime.put("zip", "application/zip");
        extToMime.put("webp", "image/webp");
        extToMime.put("heic", "image/heic");
        extToMime.put("heif", "image/heif");
        extToMime.put("avif", "image/avif");
        extToMime.put("svg", "image/svg+xml");
        extToMime.put("png", "image/png");
        extToMime.put("jpg", "image/jpeg");
        extToMime.put("jpeg", "image/jpeg");
        extToMime.put("gif", "image/gif");
        extToMime.put("mp4", "video/mp4");
        extToMime.put("mov", "video/quicktime");
        extToMime.put("webm", "video/webm");
        extToMime.put("mkv", "video/x-matroska");
        extToMime.put("m4a", "audio/mp4");
        extToMime.put("mp3", "audio/mpeg");
        extToMime.put("wav", "audio/wav");
        extToMime.put("ogg", "audio/ogg");
        extToMime.put("opus", "audio/opus");
        extToMime.put("flac", "audio/flac");
        extToMime.put("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
        extToMime.put("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
        extToMime.put("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation");
        EXTRA_EXT_TO_MIME = Collections.unmodifiableMap(extToMime);

        Map<String, String> mimeToExt = new HashMap<>();
        mimeToExt.put("application/pdf", "pdf");
        mimeToExt.put("application/json", "json");
        mimeToExt.put("application/zip", "zip");
        mimeToExt.put("text/markdown", "md");
        mimeToExt.put("text/x-markdown", "md");
        mimeToExt.put("text/plain", "txt");
        mimeToExt.put("text/html", "html");
        mimeToExt.put("text/csv", "csv");
        mimeToExt.put("text/tab-separated-values", "tsv");
        mimeToExt.put("text/yaml", "yaml");
        mimeToExt.put("application/x-yaml", "yaml");
        mimeToExt.put("image/jpeg", "jpg");
        mimeToExt.put("image/png", "png");
        mimeToExt.put("image/gif", "gif");
        mimeToExt.put("image/webp", "webp");
        mimeToExt.put("image/heic", "heic");
        mimeToExt.put("image/heif", "heif");
        mimeToExt.put("image/avif", "avif");
        mimeToExt.put("image/svg+xml", "svg");
        mimeToExt.put("video/mp4", "mp4");
        mimeToExt.put("video/quicktime", "mov");
        mimeToExt.put("video/webm", "webm");
        mimeToExt.put("video/x-matroska", "mkv");
        mimeToExt.put("audio/mpeg", "mp3");
        mimeToExt.put("audio/mp4", "m4a");
        mimeToExt.put("audio/x-m4a", "m4a");
        mimeToExt.put("audio/wav", "wav");
        mimeToExt.put("audio/x-wav", "wav");
        mimeToExt.put("audio/ogg", "ogg");
        mimeToExt.put("audio/opus", "opus");
        mimeToExt.put("audio/flac", "flac");
        mimeToExt.put("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx");
        mimeToExt.put("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx");
        mimeToExt.put("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx");
        EXTRA_MIME_TO_EXT = Collections.unmodifiableMap(mimeToExt);
    }

    static File folder(Context context, String name) {
        File folder = new File(context.getCacheDir(), name);
        if (!folder.isDirectory()) folder.mkdirs();
        return folder;
    }

    /** A new file in {@code folder} (old ones cleared), and its {@code content://} address. */
    static File newFile(Context context, String folder, String name) {
        File dir = folder(context, folder);
        long now = System.currentTimeMillis();
        File[] old = dir.listFiles();
        if (old != null) {
            for (File file : old) {
                if (now - file.lastModified() > KEEP_MS) file.delete();
            }
        }
        return new File(dir, safeName(name));
    }

    static Uri uriFor(String folder, File file) {
        return new Uri.Builder().scheme("content").authority(AUTHORITY).appendPath(folder).appendPath(file.getName()).build();
    }

    /** A file name without folders, illegal characters, or a leading dot ("报告.pdf" stays, "../x" doesn't). */
    static String safeName(String name) {
        if (name == null) return "文件";
        StringBuilder cleaned = new StringBuilder(name.length());
        for (int i = 0; i < name.length(); i++) {
            char c = name.charAt(i);
            if (c < 0x20 || c == 0x7f) continue;
            if (c == '/' || c == '\\' || c == ':' || c == '*' || c == '?' || c == '"' || c == '<' || c == '>' || c == '|') {
                cleaned.append('_');
            } else {
                cleaned.append(c);
            }
        }
        String clean = cleaned.toString().trim();
        while (clean.startsWith(".")) clean = clean.substring(1).trim();
        while (clean.endsWith(".")) clean = clean.substring(0, clean.length() - 1).trim();
        if (clean.isEmpty()) return "文件";
        if (clean.getBytes(StandardCharsets.UTF_8).length <= 180) return clean;
        int dot = clean.lastIndexOf('.');
        String ext = dot > 0 && clean.length() - dot <= 12 ? clean.substring(dot) : "";
        String stem = ext.isEmpty() ? clean : clean.substring(0, dot);
        while (!stem.isEmpty() && (stem + ext).getBytes(StandardCharsets.UTF_8).length > 180) {
            stem = stem.substring(0, stem.length() - 1);
        }
        return stem.isEmpty() ? "文件" + ext : stem + ext;
    }

    static String cleanMime(String mimeType) {
        if (mimeType == null) return null;
        int semi = mimeType.indexOf(';');
        String clean = (semi >= 0 ? mimeType.substring(0, semi) : mimeType).trim().toLowerCase(Locale.ROOT);
        return clean.isEmpty() ? null : clean;
    }

    static String extensionFromMime(String mimeType) {
        String clean = cleanMime(mimeType);
        if (clean == null || "application/octet-stream".equals(clean)) return null;
        String mapped = EXTRA_MIME_TO_EXT.get(clean);
        if (mapped != null) return mapped;
        return MimeTypeMap.getSingleton().getExtensionFromMimeType(clean);
    }

    static boolean hasKnownExtension(String name) {
        if (name == null) return false;
        int dot = name.lastIndexOf('.');
        if (dot <= 0 || dot == name.length() - 1 || name.length() - dot - 1 > 10) return false;
        String ext = name.substring(dot + 1).toLowerCase(Locale.ROOT);
        for (int i = 0; i < ext.length(); i++) {
            char c = ext.charAt(i);
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'))) return false;
        }
        return EXTRA_EXT_TO_MIME.containsKey(ext) || MimeTypeMap.getSingleton().hasExtension(ext);
    }

    static String ensureExtension(String name, String mimeType) {
        String safe = safeName(name);
        if (hasKnownExtension(safe)) return safe;
        String ext = extensionFromMime(mimeType);
        if (ext == null || ext.isEmpty()) return safe;
        if (safe.toLowerCase(Locale.ROOT).endsWith("." + ext.toLowerCase(Locale.ROOT))) return safe;
        return safeName(safe + "." + ext);
    }

    static File fileFor(Context context, Uri uri) throws FileNotFoundException {
        if (context == null || uri == null) throw new FileNotFoundException("null context or uri");
        List<String> parts = uri.getPathSegments();
        if (!AUTHORITY.equals(uri.getAuthority()) || parts.size() != 2
                || !(CAPTURES.equals(parts.get(0)) || SHARED.equals(parts.get(0)))
                || !safeName(parts.get(1)).equals(parts.get(1))) {
            throw new FileNotFoundException(uri.toString());
        }
        return new File(new File(context.getCacheDir(), parts.get(0)), parts.get(1));
    }

    static String typeOf(String name) {
        if (name == null) return "application/octet-stream";
        int dot = name.lastIndexOf('.');
        if (dot < 0 || dot == name.length() - 1) return "application/octet-stream";
        String ext = name.substring(dot + 1).toLowerCase(Locale.ROOT);
        String type = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
        if (type == null) type = EXTRA_EXT_TO_MIME.get(ext);
        return type == null ? "application/octet-stream" : type;
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = fileFor(getContext(), uri);
        int flags = mode != null && mode.contains("w")
                ? ParcelFileDescriptor.MODE_READ_WRITE | ParcelFileDescriptor.MODE_CREATE | ParcelFileDescriptor.MODE_TRUNCATE
                : ParcelFileDescriptor.MODE_READ_ONLY;
        return ParcelFileDescriptor.open(file, flags);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        File file;
        try {
            file = fileFor(getContext(), uri);
        } catch (FileNotFoundException e) {
            return null;
        }
        String[] columns = projection != null ? projection : new String[] {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        MatrixCursor cursor = new MatrixCursor(columns, 1);
        Object[] row = new Object[columns.length];
        for (int i = 0; i < columns.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(columns[i])) row[i] = file.getName();
            else if (OpenableColumns.SIZE.equals(columns[i])) row[i] = file.length();
        }
        cursor.addRow(row);
        return cursor;
    }

    @Override
    public String getType(Uri uri) {
        String last = uri == null ? null : uri.getLastPathSegment();
        return typeOf(last == null ? "" : last);
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
