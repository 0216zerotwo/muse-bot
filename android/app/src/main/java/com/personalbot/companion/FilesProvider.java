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
import java.util.List;

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

    /** A file name without folders or a leading dot ("报告.pdf" stays, "../x" doesn't). */
    static String safeName(String name) {
        String clean = name == null ? "" : name.replace('/', '_').replace('\\', '_').trim();
        while (clean.startsWith(".")) clean = clean.substring(1);
        return clean.isEmpty() ? "文件" : clean;
    }

    static File fileFor(Context context, Uri uri) throws FileNotFoundException {
        List<String> parts = uri.getPathSegments();
        if (!AUTHORITY.equals(uri.getAuthority()) || parts.size() != 2
                || !(CAPTURES.equals(parts.get(0)) || SHARED.equals(parts.get(0)))
                || !safeName(parts.get(1)).equals(parts.get(1))) {
            throw new FileNotFoundException(uri.toString());
        }
        return new File(new File(context.getCacheDir(), parts.get(0)), parts.get(1));
    }

    static String typeOf(String name) {
        int dot = name.lastIndexOf('.');
        String type = dot < 0 ? null : MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substring(dot + 1).toLowerCase());
        return type == null ? "application/octet-stream" : type;
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = fileFor(getContext(), uri);
        int flags = mode.contains("w")
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
        return typeOf(uri.getLastPathSegment() == null ? "" : uri.getLastPathSegment());
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
