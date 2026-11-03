package com.personalbot.companion;

import android.Manifest;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.Context;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.pdf.PdfRenderer;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.ParcelFileDescriptor;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.util.Base64;
import android.util.Size;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/** The phone's photos and files, for the parts of the app a WebView can't reach. */
final class MediaLibrary {
    /** Files fetched from the server for sharing or opening, at most this big. */
    private static final long MAX_FETCH_BYTES = 300L * 1024 * 1024;

    private MediaLibrary() {}

    /** A photo or video in the ＋ strip. */
    static final class Recent {
        final Uri uri;
        final boolean video;
        final long durationMs;
        Bitmap thumbnail;

        Recent(Uri uri, boolean video, long durationMs) {
            this.uri = uri;
            this.video = video;
            this.durationMs = durationMs;
        }
    }

    static String[] readPermissions() {
        if (Build.VERSION.SDK_INT >= 34) {
            return new String[] {Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO,
                    Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED};
        }
        if (Build.VERSION.SDK_INT >= 33) {
            return new String[] {Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO};
        }
        return new String[] {Manifest.permission.READ_EXTERNAL_STORAGE};
    }

    /** Some or all of the photos may be read (on Android 14 the user may pick which ones). */
    static boolean canRead(Context context) {
        for (String permission : readPermissions()) {
            if (context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED) return true;
        }
        return false;
    }

    /** The newest photos and videos, newest first, without their thumbnails yet. */
    static List<Recent> recent(Context context, int limit) {
        List<Recent> items = new ArrayList<>();
        if (!canRead(context)) return items;
        Uri files = MediaStore.Files.getContentUri("external");
        String[] columns = Build.VERSION.SDK_INT >= 29
                ? new String[] {MediaStore.MediaColumns._ID, MediaStore.Files.FileColumns.MEDIA_TYPE, MediaStore.MediaColumns.DURATION}
                : new String[] {MediaStore.MediaColumns._ID, MediaStore.Files.FileColumns.MEDIA_TYPE};
        String where = MediaStore.Files.FileColumns.MEDIA_TYPE + "=? OR " + MediaStore.Files.FileColumns.MEDIA_TYPE + "=?";
        String[] args = {String.valueOf(MediaStore.Files.FileColumns.MEDIA_TYPE_IMAGE), String.valueOf(MediaStore.Files.FileColumns.MEDIA_TYPE_VIDEO)};
        try (Cursor cursor = context.getContentResolver().query(files, columns, where, args,
                MediaStore.MediaColumns.DATE_ADDED + " DESC")) {
            while (cursor != null && cursor.moveToNext() && items.size() < limit) {
                long id = cursor.getLong(0);
                boolean video = cursor.getInt(1) == MediaStore.Files.FileColumns.MEDIA_TYPE_VIDEO;
                long duration = columns.length > 2 && !cursor.isNull(2) ? cursor.getLong(2) : 0;
                Uri base = video ? MediaStore.Video.Media.EXTERNAL_CONTENT_URI : MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
                items.add(new Recent(ContentUris.withAppendedId(base, id), video, duration));
            }
        } catch (RuntimeException ignored) {
            // A phone whose media index can't be read: the strip just stays empty.
        }
        return items;
    }

    static Bitmap thumbnail(Context context, Recent item, int px) {
        try {
            ContentResolver resolver = context.getContentResolver();
            if (Build.VERSION.SDK_INT >= 29) return resolver.loadThumbnail(item.uri, new Size(px, px), null);
            long id = ContentUris.parseId(item.uri);
            return item.video
                    ? MediaStore.Video.Thumbnails.getThumbnail(resolver, id, MediaStore.Video.Thumbnails.MINI_KIND, null)
                    : MediaStore.Images.Thumbnails.getThumbnail(resolver, id, MediaStore.Images.Thumbnails.MINI_KIND, null);
        } catch (Exception e) {
            return null;
        }
    }

    /** A picked file's own name ("IMG_2041.jpg"), or one made up from its type. */
    static String displayName(Context context, Uri uri, String fallback) {
        try (Cursor cursor = context.getContentResolver().query(uri, new String[] {OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst() && !cursor.isNull(0)) {
                String name = cursor.getString(0);
                if (name != null && !name.trim().isEmpty()) return name;
            }
        } catch (RuntimeException ignored) {
            // Fall back below.
        }
        return fallback;
    }

    /** A file of the companion's (a signed link on its server) into the app's cache. */
    static File fetch(Context context, String url, String name) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(15_000);
        connection.setReadTimeout(60_000);
        try {
            int status = connection.getResponseCode();
            if (status / 100 != 2) throw new IllegalStateException("HTTP " + status);
            if (connection.getContentLengthLong() > MAX_FETCH_BYTES) throw new IllegalStateException("too big");
            File file = FilesProvider.newFile(context, FilesProvider.SHARED, name);
            try (InputStream in = connection.getInputStream(); OutputStream out = new FileOutputStream(file)) {
                copy(in, out);
            }
            return file;
        } finally {
            connection.disconnect();
        }
    }

    /**
     * A picture or video into the phone's gallery (相册 → Muse Bot), as the iPhone app saves to 照片.
     * Android 10 and later need no permission for this.
     */
    static void saveToGallery(Context context, String url, String name, boolean video) throws Exception {
        ContentResolver resolver = context.getContentResolver();
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, FilesProvider.safeName(name));
        values.put(MediaStore.MediaColumns.MIME_TYPE, FilesProvider.typeOf(name));
        values.put(MediaStore.MediaColumns.RELATIVE_PATH, (video ? Environment.DIRECTORY_MOVIES : Environment.DIRECTORY_PICTURES) + "/Muse Bot");
        values.put(MediaStore.MediaColumns.IS_PENDING, 1);
        Uri collection = video ? MediaStore.Video.Media.EXTERNAL_CONTENT_URI : MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
        Uri item = resolver.insert(collection, values);
        if (item == null) throw new IllegalStateException("gallery refused");
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(15_000);
        connection.setReadTimeout(60_000);
        try {
            if (connection.getResponseCode() / 100 != 2) throw new IllegalStateException("HTTP " + connection.getResponseCode());
            try (InputStream in = connection.getInputStream(); OutputStream out = resolver.openOutputStream(item)) {
                if (out == null) throw new IllegalStateException("no output");
                copy(in, out);
            }
            values.clear();
            values.put(MediaStore.MediaColumns.IS_PENDING, 0);
            resolver.update(item, values, null, null);
        } catch (Exception e) {
            resolver.delete(item, null, null);
            throw e;
        } finally {
            connection.disconnect();
        }
    }

    /** A PDF's first page as a picture (data: URL), as the iPhone app shows a document's paper. */
    static String pdfFirstPage(File file, int width) throws Exception {
        try (ParcelFileDescriptor descriptor = ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
             PdfRenderer renderer = new PdfRenderer(descriptor)) {
            if (renderer.getPageCount() == 0) return "";
            try (PdfRenderer.Page page = renderer.openPage(0)) {
                int height = Math.max(1, Math.round(width * (float) page.getHeight() / Math.max(1, page.getWidth())));
                Bitmap bitmap = Bitmap.createBitmap(width, Math.min(height, 4000), Bitmap.Config.ARGB_8888);
                bitmap.eraseColor(Color.WHITE);
                page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);
                return "data:image/jpeg;base64," + jpeg(bitmap, 80);
            }
        }
    }

    static String jpeg(Bitmap bitmap, int quality) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        bitmap.compress(Bitmap.CompressFormat.JPEG, quality, out);
        return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);
    }

    private static void copy(InputStream in, OutputStream out) throws Exception {
        byte[] buffer = new byte[64 * 1024];
        long total = 0;
        for (int n; (n = in.read(buffer)) > 0; ) {
            total += n;
            if (total > MAX_FETCH_BYTES) throw new IllegalStateException("too big");
            out.write(buffer, 0, n);
        }
    }
}
