package com.personalbot.companion;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.ClipData;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.content.ClipboardManager;
import android.content.ContentResolver;
import android.content.pm.ActivityInfo;
import android.os.Build;
import android.os.Bundle;
import android.provider.MediaStore;
import android.provider.Settings;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.WebResourceResponse;
import android.widget.FrameLayout;
import android.os.Environment;
import android.speech.tts.TextToSpeech;
import android.util.Log;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.File;
import java.io.InputStream;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * A thin shell around the companion page served by the user's own server.
 *
 * <p>The server runs elsewhere (a rented server, or the user's PC reached over USB with
 * {@code adb reverse}); this app remembers its address and shows {@code /companion.html}
 * full screen. Pages from any other origin open in the phone's browser, so only the server
 * and the bundled setup page run in here.
 *
 * <p>Connecting is done once: the page pairs with a code from the server and gets a device
 * token, which it keeps in its storage and, through {@link Bridge#saveDevice}, in this app's
 * preferences too. Both stay until the user disconnects the device or points the app at
 * another server.
 *
 * <p>What a page in a WebView can't do, the app does: reminders due become the phone's own
 * notifications ({@link Reminders}), replies are read out with the phone's voice, text shared
 * from another app lands in the chat's box, and files are saved to 下载.
 */
public class MainActivity extends Activity {
    private static final String TAG = "Companion";
    private static final String PREFS = "companion";
    private static final String KEY_SERVER = "server";
    private static final String KEY_DEVICE = "device";
    private static final String SETUP_URL = "file:///android_asset/setup.html";
    private static final String DEFAULT_SERVER = "http://127.0.0.1:8766";
    private static final String COMPANION_PATH = "/companion.html";
    /** The companion's sheets are Radix dialogs, which close on Escape. */
    private static final String CLOSE_SHEET_JS =
            "(function(){if(!document.querySelector('[role=\"dialog\"]'))return false;"
                    + "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',keyCode:27,bubbles:true}));"
                    + "return true;})()";
    private static final int REQUEST_FILES = 1;
    private static final int REQUEST_MIC = 2;
    private static final int REQUEST_NOTIFY = 3;
    private static final String KEY_NOTIFY_ASKED = "notify-asked";
    private static final String SHARED_EVENT_JS = "window.dispatchEvent(new Event('companion:shared'))";
    private static final int REQUEST_ATTACH = 4;
    private static final int REQUEST_SHOOT = 5;
    private static final int REQUEST_PHOTOS = 6;
    private static final String KEY_PHOTOS_ASKED = "photos-asked";
    /** Files picked with ＋: the page fetches them from here (answered by the app, never the server). */
    private static final String PICKED_PATH = "/__android/picked/";
    /** The server's file links: opened, shared or saved by the app instead of shown as a page. */
    private static final String[] FILE_PATHS = {"/api/media/", "/api/webui/companion/creations/file/"};
    /** Over plain http on a home network the page has no clipboard: it goes through the app. */
    private static final String CLIPBOARD_JS =
            "(function(){var b=window.PersonalBotAndroid;if(window.isSecureContext&&navigator.clipboard||!b||!b.copy)return;"
                    + "try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{"
                    + "writeText:function(t){b.copy(String(t));return Promise.resolve();},"
                    + "readText:function(){return Promise.reject(new Error('unsupported'));}}});}catch(e){}})()";

    private WebView web;
    private ValueCallback<Uri[]> pendingFiles;
    private PermissionRequest pendingPermission;
    /** Only the setup page may change the server; read from the bridge's thread. */
    private volatile String currentUrl = "";
    private volatile String lastError = "";
    /** Text shared to the app, until the page takes it. */
    private volatile String shared = "";
    private TextToSpeech voice;
    private volatile boolean voiceReady;
    private volatile long plannedAt;
    private FrameLayout root;
    private AttachSheet attachSheet;
    /** Where the camera writes the photo or video it is taking. */
    private Uri shooting;
    private final Map<String, Uri> picked = new ConcurrentHashMap<>();
    /** A video played full screen, over the page. */
    private View fullscreen;
    private WebChromeClient.CustomViewCallback fullscreenDone;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        setContentView(root);
        configure(web);
        if (state == null || web.restoreState(state) == null) {
            open();
        }
        if (state != null) shooting = state.getParcelable("shooting");
        takeShare(getIntent());
        Reminders.scheduleRefresh(this);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (takeShare(intent)) web.evaluateJavascript(SHARED_EVENT_JS, null);
    }

    /** Text (a link, a passage) shared from another app: kept for the page's chat box. */
    private boolean takeShare(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return false;
        CharSequence text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
        if (text == null || text.toString().trim().isEmpty()) return false;
        String subject = intent.getStringExtra(Intent.EXTRA_SUBJECT);
        String body = text.toString().trim();
        // A shared link often comes with its page's title as the subject.
        boolean titled = subject != null && !subject.trim().isEmpty() && !body.contains(subject.trim());
        shared = titled ? subject.trim() + "\n" + body : body;
        intent.setAction(Intent.ACTION_MAIN);
        return true;
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private void configure(WebView view) {
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setUserAgentString(settings.getUserAgentString() + " PersonalBotAndroid/0.4");
        view.addJavascriptInterface(new Bridge(), "PersonalBotAndroid");
        view.setWebViewClient(new Client());
        view.setWebChromeClient(new Chrome());
        view.setDownloadListener((url, userAgent, disposition, mimeType, length) -> save(url, disposition, mimeType));
        // A page with a service worker fetches through it: the picked files are answered there too.
        ServiceWorkerController.getInstance().setServiceWorkerClient(new ServiceWorkerClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                return pickedFile(request.getUrl());
            }
        });
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private String server() {
        return prefs().getString(KEY_SERVER, null);
    }

    private void open() {
        String server = server();
        if (server == null) {
            web.loadUrl(SETUP_URL);
        } else {
            web.loadUrl(server + COMPANION_PATH);
        }
    }

    /** {@code http(s)://host[:port][/path]} without a trailing slash, or {@code null}. */
    static String normalize(String address) {
        String text = address == null ? "" : address.trim();
        if (text.isEmpty()) return null;
        if (!text.contains("://")) text = "http://" + text;
        Uri uri = Uri.parse(text);
        String scheme = uri.getScheme();
        if (!"http".equals(scheme) && !"https".equals(scheme)) return null;
        if (uri.getHost() == null || uri.getHost().isEmpty()) return null;
        String path = uri.getPath() == null ? "" : uri.getPath();
        // A pasted connection link points at the page itself.
        if (path.endsWith(COMPANION_PATH)) path = path.substring(0, path.length() - COMPANION_PATH.length());
        while (path.endsWith("/")) path = path.substring(0, path.length() - 1);
        String port = uri.getPort() == -1 ? "" : ":" + uri.getPort();
        return scheme + "://" + uri.getHost() + port + path;
    }

    private static int port(Uri uri) {
        if (uri.getPort() != -1) return uri.getPort();
        return "https".equals(uri.getScheme()) ? 443 : 80;
    }

    /** The bundled setup page, or the same origin as the configured bot. */
    private boolean isOwn(Uri url) {
        String scheme = url.getScheme();
        if ("about".equals(scheme) || "data".equals(scheme) || "blob".equals(scheme)) return true;
        if (url.toString().startsWith("file:///android_asset/")) return true;
        String server = server();
        if (server == null) return false;
        Uri own = Uri.parse(server);
        return own.getScheme().equals(scheme)
                && own.getHost() != null
                && own.getHost().equalsIgnoreCase(url.getHost())
                && port(own) == port(url);
    }

    private void openExternal(Uri url) {
        Intent intent = new Intent(Intent.ACTION_VIEW, url);
        String scheme = url.getScheme();
        if ("http".equals(scheme) || "https".equals(scheme)) {
            // A Custom Tab: the page opens over the app, with ✕ back to it (the browser's own UI
            // otherwise). These are the extras the androidx.browser library would set.
            Bundle extras = new Bundle();
            extras.putBinder("android.support.customtabs.extra.SESSION", null);
            intent.putExtras(extras);
            intent.putExtra("android.support.customtabs.extra.SHARE_STATE", 1);
        }
        try {
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            Log.w(TAG, "no app to open " + scheme + " links");
        }
    }

    private boolean isFileLink(Uri url) {
        String path = url.getPath() == null ? "" : url.getPath();
        if (!isOwn(url)) return false;
        for (String prefix : FILE_PATHS) {
            if (path.startsWith(prefix)) return true;
        }
        return false;
    }

    /** The file a link names: its own name when the link carries one, else its last segment. */
    private static String nameOf(Uri url, String fallback) {
        if (fallback != null && !fallback.trim().isEmpty()) return fallback.trim();
        String last = url.getLastPathSegment();
        return last == null || last.isEmpty() ? "文件" : last;
    }

    /** A file of the companion's, opened with the phone's own apps (as QuickLook does on iPhone). */
    private void openFile(Uri url, String name) {
        fetchThen(url, name, file -> {
            Uri uri = FilesProvider.uriFor(FilesProvider.SHARED, file);
            Intent view = new Intent(Intent.ACTION_VIEW).setDataAndType(uri, FilesProvider.typeOf(file.getName()))
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            try {
                startActivity(Intent.createChooser(view, "打开「" + file.getName() + "」"));
            } catch (ActivityNotFoundException e) {
                Toast.makeText(this, "手机上没有能打开这种文件的应用", Toast.LENGTH_LONG).show();
            }
        });
    }

    /** A file of the companion's, into the system share sheet. */
    private void shareFile(Uri url, String name) {
        fetchThen(url, name, file -> {
            Uri uri = FilesProvider.uriFor(FilesProvider.SHARED, file);
            Intent send = new Intent(Intent.ACTION_SEND).setType(FilesProvider.typeOf(file.getName()))
                    .putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            send.setClipData(ClipData.newRawUri(file.getName(), uri));
            try {
                startActivity(Intent.createChooser(send, "分享「" + file.getName() + "」"));
            } catch (ActivityNotFoundException e) {
                Toast.makeText(this, "没有能分享的应用", Toast.LENGTH_SHORT).show();
            }
        });
    }

    private interface FileReady {
        void run(File file);
    }

    private void fetchThen(Uri url, String name, FileReady then) {
        if (!isFileLink(url)) return;
        Toast.makeText(this, "正在准备…", Toast.LENGTH_SHORT).show();
        final Context app = getApplicationContext();
        new Thread(() -> {
            try {
                File file = MediaLibrary.fetch(app, url.toString(), nameOf(url, name));
                runOnUiThread(() -> then.run(file));
            } catch (Exception e) {
                Log.w(TAG, "file not fetched: " + e.getMessage());
                runOnUiThread(() -> Toast.makeText(this, "暂时拿不到这个文件，稍后再试", Toast.LENGTH_LONG).show());
            }
        }, "file").start();
    }

    /** The answer to the page's fetch of a file picked with ＋ (null: not one of those). */
    private WebResourceResponse pickedFile(Uri url) {
        String path = url.getPath();
        if (path == null || !path.startsWith(PICKED_PATH) || !isOwn(url)) return null;
        Uri file = picked.get(path.substring(PICKED_PATH.length()));
        if (file == null) return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", new HashMap<String, String>(), null);
        try {
            ContentResolver resolver = getContentResolver();
            InputStream in = resolver.openInputStream(file);
            String type = resolver.getType(file);
            Map<String, String> headers = new HashMap<>();
            headers.put("Cache-Control", "no-store");
            return new WebResourceResponse(type == null ? "application/octet-stream" : type, null, 200, "OK", headers, in);
        } catch (Exception e) {
            return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", new HashMap<String, String>(), null);
        }
    }

    // -- ＋: the attach sheet -------------------------------------------------------------------------

    private void showAttach() {
        if (attachSheet == null) {
            attachSheet = new AttachSheet(this, new AttachSheet.Listener() {
                @Override
                public void onCamera(boolean video) {
                    shoot(video);
                }

                @Override
                public void onLibrary() {
                    pick(true, true);
                }

                @Override
                public void onFiles() {
                    Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                            .setType("*/*").putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    start(intent, REQUEST_ATTACH);
                }

                @Override
                public void onVideos() {
                    pick(false, true);
                }

                @Override
                public void onRecent(Uri uri) {
                    deliver(Collections.singletonList(uri));
                }

                @Override
                public void onAllowPhotos() {
                    askForPhotos(true);
                }
            });
        }
        boolean canRead = MediaLibrary.canRead(this);
        attachSheet.show(canRead);
        // Asked the first time the sheet opens, as the iPhone app asks for the photo library.
        if (!canRead) askForPhotos(false);
    }

    private void askForPhotos(boolean fromTile) {
        if (MediaLibrary.canRead(this)) return;
        if (!prefs().getBoolean(KEY_PHOTOS_ASKED, false)) {
            prefs().edit().putBoolean(KEY_PHOTOS_ASKED, true).apply();
            requestPermissions(MediaLibrary.readPermissions(), REQUEST_PHOTOS);
        } else if (fromTile) {
            // Asked before and refused: only the system settings can change it now.
            startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())));
        }
    }

    /** The system photo picker: photos and videos (查看全部), or videos only (添加视频). */
    private void pick(boolean photos, boolean videos) {
        Intent intent;
        if (Build.VERSION.SDK_INT >= 33) {
            intent = new Intent(MediaStore.ACTION_PICK_IMAGES);
            if (!photos) intent.setType("video/*");
            intent.putExtra(MediaStore.EXTRA_PICK_IMAGES_MAX, Math.min(9, MediaStore.getPickImagesMaxLimit()));
        } else {
            intent = new Intent(Intent.ACTION_GET_CONTENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
            if (photos) {
                intent.setType("*/*").putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"image/*", "video/*"});
            } else {
                intent.setType("video/*");
            }
        }
        start(intent, REQUEST_ATTACH);
    }

    /** The camera app, writing into the app's own cache (no storage permission needed). */
    private void shoot(boolean video) {
        String name = (video ? "录像-" : "照片-") + System.currentTimeMillis() + (video ? ".mp4" : ".jpg");
        File file = FilesProvider.newFile(this, FilesProvider.CAPTURES, name);
        shooting = FilesProvider.uriFor(FilesProvider.CAPTURES, file);
        Intent intent = new Intent(video ? MediaStore.ACTION_VIDEO_CAPTURE : MediaStore.ACTION_IMAGE_CAPTURE)
                .putExtra(MediaStore.EXTRA_OUTPUT, shooting)
                .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        intent.setClipData(ClipData.newRawUri(name, shooting));
        // The chat takes files up to 20 MB.
        if (video) intent.putExtra(MediaStore.EXTRA_SIZE_LIMIT, 19L * 1024 * 1024);
        start(intent, REQUEST_SHOOT);
    }

    private void start(Intent intent, int request) {
        try {
            startActivityForResult(intent, request);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, request == REQUEST_SHOOT ? "这台手机没有可用的相机" : "没有能选文件的应用", Toast.LENGTH_SHORT).show();
        }
    }

    /** Picked files to the page's composer: it fetches each from {@link #PICKED_PATH}. */
    private void deliver(List<Uri> uris) {
        if (uris.isEmpty()) return;
        if (picked.size() > 50) picked.clear();
        JSONArray files = new JSONArray();
        int index = 0;
        for (Uri uri : uris) {
            index++;
            String id = UUID.randomUUID().toString().replace("-", "");
            picked.put(id, uri);
            String type = getContentResolver().getType(uri);
            String fallback = "附件-" + index + "." + extensionOf(type);
            try {
                files.put(new JSONObject()
                        .put("url", PICKED_PATH + id)
                        .put("name", MediaLibrary.displayName(this, uri, fallback))
                        .put("type", type == null ? "" : type));
            } catch (org.json.JSONException ignored) {
                // Not with plain strings.
            }
        }
        web.evaluateJavascript("window.dispatchEvent(new CustomEvent('companion:attach',{detail:" + files + "}))", null);
    }

    private static String extensionOf(String type) {
        String ext = type == null ? null : android.webkit.MimeTypeMap.getSingleton().getExtensionFromMimeType(type);
        return ext == null ? "bin" : ext;
    }

    // -- full-screen video ------------------------------------------------------------------------

    private void enterFullscreen(View view, WebChromeClient.CustomViewCallback done) {
        if (fullscreen != null) {
            done.onCustomViewHidden();
            return;
        }
        fullscreen = view;
        fullscreenDone = done;
        view.setBackgroundColor(android.graphics.Color.BLACK);
        root.addView(view, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR);
        hideSystemBars(true);
    }

    private void exitFullscreen() {
        if (fullscreen == null) return;
        root.removeView(fullscreen);
        fullscreen = null;
        hideSystemBars(false);
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
        if (fullscreenDone != null) fullscreenDone.onCustomViewHidden();
        fullscreenDone = null;
    }

    @SuppressWarnings("deprecation")
    private void hideSystemBars(boolean hide) {
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController controller = getWindow().getInsetsController();
            if (controller == null) return;
            if (hide) {
                controller.hide(WindowInsets.Type.systemBars());
                controller.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            } else {
                controller.show(WindowInsets.Type.systemBars());
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(hide
                    ? View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    : 0);
        }
    }

    /** A file from the page (报告.pdf, a picture): into 下载, with the system's own progress. */
    private void save(String url, String disposition, String mimeType) {
        Uri uri = Uri.parse(url);
        String scheme = uri.getScheme();
        // Older phones need a storage permission for 下载: there the browser saves it.
        DownloadManager downloads = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
        if (Build.VERSION.SDK_INT < 29 || downloads == null || !("http".equals(scheme) || "https".equals(scheme))) {
            openExternal(uri);
            return;
        }
        String name = URLUtil.guessFileName(url, disposition, mimeType);
        String type = mimeType == null || mimeType.isEmpty() || mimeType.equals("application/octet-stream")
                ? FilesProvider.typeOf(name) : mimeType;
        if (isOwn(uri) && (type.startsWith("image/") || type.startsWith("video/"))) {
            final boolean video = type.startsWith("video/");
            final Context app = getApplicationContext();
            new Thread(() -> {
                try {
                    MediaLibrary.saveToGallery(app, url, name, video);
                    runOnUiThread(() -> Toast.makeText(this, "已保存到相册", Toast.LENGTH_SHORT).show());
                } catch (Exception e) {
                    Log.w(TAG, "not saved to the gallery: " + e.getMessage());
                    runOnUiThread(() -> Toast.makeText(this, "保存失败，稍后再试", Toast.LENGTH_LONG).show());
                }
            }, "save").start();
            return;
        }
        DownloadManager.Request request;
        try {
            request = new DownloadManager.Request(uri)
                    .setTitle(name)
                    .setMimeType(mimeType)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
        } catch (RuntimeException e) {
            openExternal(uri);
            return;
        }
        try {
            downloads.enqueue(request);
        } catch (RuntimeException e) {
            openExternal(uri);
            return;
        }
        Toast.makeText(this, "正在保存到「下载」：" + name, Toast.LENGTH_SHORT).show();
    }

    /**
     * Reminders set again (off the main thread); the first time there are some, ask to notify.
     * Coming back to the app does it at most once a minute (a photo picked is a resume too).
     */
    private void planReminders(boolean now) {
        long at = System.currentTimeMillis();
        if (server() == null || (!now && at - plannedAt < 60_000)) return;
        plannedAt = at;
        final android.content.Context app = getApplicationContext();
        new Thread(() -> {
            try {
                if (Reminders.plan(app) > 0) runOnUiThread(() -> askToNotify(false));
            } catch (Exception e) {
                Log.w(TAG, "reminders not planned: " + e.getMessage());
            }
        }, "reminders").start();
    }

    /** Android 13+ asks before an app's first notification: once, or again when the user turns it on. */
    private void askToNotify(boolean again) {
        if (Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return;
        if (!again && prefs().getBoolean(KEY_NOTIFY_ASKED, false)) return;
        prefs().edit().putBoolean(KEY_NOTIFY_ASKED, true).apply();
        requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, REQUEST_NOTIFY);
    }

    /** The phone's voice for 朗读 (the WebView has none), started the first time it is needed. */
    private void speak(String text, String lang) {
        if (voice == null) {
            voice = new TextToSpeech(getApplicationContext(), status -> {
                voiceReady = status == TextToSpeech.SUCCESS;
                if (voiceReady) say(text, lang);
            });
            return;
        }
        if (voiceReady) say(text, lang);
    }

    private void say(String text, String lang) {
        Locale locale = Locale.forLanguageTag(lang == null || lang.isEmpty() ? "zh-CN" : lang);
        if (voice.isLanguageAvailable(locale) >= TextToSpeech.LANG_AVAILABLE) voice.setLanguage(locale);
        voice.speak(text, TextToSpeech.QUEUE_FLUSH, null, "reply");
    }

    /** Called from JavaScript as {@code window.PersonalBotAndroid}. */
    private final class Bridge {
        @JavascriptInterface
        public String server() {
            String server = MainActivity.this.server();
            return server == null ? DEFAULT_SERVER : server;
        }

        @JavascriptInterface
        public String error() {
            return lastError;
        }

        /**
         * The server's address, or a connection link ({@code …/companion.html#pair=CODE}): the
         * page pairs with the code at once. Returns an error message, or "" once it is opening.
         */
        @JavascriptInterface
        public String connect(String address) {
            if (!currentUrl.startsWith(SETUP_URL)) return "";
            String server = normalize(address);
            if (server == null) return "地址要像 https://你的域名，或粘贴服务器给的连接链接";
            String fragment = Uri.parse(address.trim()).getEncodedFragment();
            SharedPreferences.Editor edit = prefs().edit().putString(KEY_SERVER, server);
            // A device token belongs to one server.
            if (!server.equals(MainActivity.this.server())) edit.remove(KEY_DEVICE);
            edit.apply();
            lastError = "";
            String page = server + COMPANION_PATH + (fragment != null && fragment.startsWith("pair=") ? "#" + fragment : "");
            runOnUiThread(() -> web.loadUrl(page));
            return "";
        }

        /** This device's token for the configured server, "" when not paired. */
        @JavascriptInterface
        public String deviceToken() {
            return isOwn(Uri.parse(currentUrl)) ? prefs().getString(KEY_DEVICE, "") : "";
        }

        /** Keep (or with "" forget) the device token, beside the page's own copy. */
        @JavascriptInterface
        public void saveDevice(String token) {
            if (!isOwn(Uri.parse(currentUrl)) || currentUrl.startsWith(SETUP_URL)) return;
            SharedPreferences.Editor edit = prefs().edit();
            if (token == null || token.isEmpty()) edit.remove(KEY_DEVICE);
            else edit.putString(KEY_DEVICE, token);
            edit.apply();
        }

        @JavascriptInterface
        public void changeServer() {
            runOnUiThread(() -> web.loadUrl(SETUP_URL));
        }

        @JavascriptInterface
        public void speak(String text, String lang) {
            if (!isOwn(Uri.parse(currentUrl)) || text == null || text.isEmpty()) return;
            runOnUiThread(() -> MainActivity.this.speak(text, lang));
        }

        @JavascriptInterface
        public void stopSpeaking() {
            runOnUiThread(() -> {
                if (voice != null) voice.stop();
            });
        }

        @JavascriptInterface
        public boolean speaking() {
            TextToSpeech current = voice;
            return current != null && current.isSpeaking();
        }

        @JavascriptInterface
        public void planReminders() {
            MainActivity.this.planReminders(true);
        }

        @JavascriptInterface
        public boolean remindersEnabled() {
            return Reminders.enabled(MainActivity.this);
        }

        @JavascriptInterface
        public void setRemindersEnabled(boolean on) {
            if (!isOwn(Uri.parse(currentUrl))) return;
            prefs().edit().putBoolean(Reminders.KEY_ENABLED, on).apply();
            if (on) {
                runOnUiThread(() -> askToNotify(true));
                MainActivity.this.planReminders(true);
            } else {
                Reminders.schedule(MainActivity.this, Collections.<Reminders.Time>emptyList(), "");
            }
        }

        /** ＋ in the composer: the app's own sheet (camera, latest photos, files, videos). */
        @JavascriptInterface
        public void attach() {
            if (!isOwn(Uri.parse(currentUrl)) || currentUrl.startsWith(SETUP_URL)) return;
            runOnUiThread(MainActivity.this::showAttach);
        }

        @JavascriptInterface
        public void shareFile(String url, String name) {
            if (url == null) return;
            runOnUiThread(() -> MainActivity.this.shareFile(Uri.parse(url), name));
        }

        @JavascriptInterface
        public void openFile(String url, String name) {
            if (url == null) return;
            runOnUiThread(() -> MainActivity.this.openFile(Uri.parse(url), name));
        }

        /** A PDF's first page for its card: answered later as a companion:pdf-preview event. */
        @JavascriptInterface
        public void pdfPreview(String url, String id) {
            if (url == null || id == null || !isFileLink(Uri.parse(url))) return;
            final Context app = getApplicationContext();
            final float density = getResources().getDisplayMetrics().density;
            new Thread(() -> {
                String image = "";
                try {
                    File file = MediaLibrary.fetch(app, url, "preview-" + Integer.toHexString(url.hashCode()) + ".pdf");
                    image = MediaLibrary.pdfFirstPage(file, Math.round(400 * density));
                    file.delete();
                } catch (Exception e) {
                    Log.w(TAG, "no PDF preview: " + e.getMessage());
                }
                String detail;
                try {
                    detail = new JSONObject().put("id", id).put("image", image).toString();
                } catch (org.json.JSONException e) {
                    return;
                }
                runOnUiThread(() -> web.evaluateJavascript(
                        "window.dispatchEvent(new CustomEvent('companion:pdf-preview',{detail:" + detail + "}))", null));
            }, "pdf-preview").start();
        }

        @JavascriptInterface
        public void copy(String text) {
            if (text == null) return;
            runOnUiThread(() -> {
                ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                if (clipboard != null) clipboard.setPrimaryClip(ClipData.newPlainText("Muse Bot", text));
            });
        }

        /** Text shared from another app, "" when none: given to the companion's page once. */
        @JavascriptInterface
        public String takeShared() {
            if (!isOwn(Uri.parse(currentUrl)) || currentUrl.startsWith(SETUP_URL)) return "";
            String text = shared;
            shared = "";
            return text;
        }
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri url = request.getUrl();
            if (request.isForMainFrame() && isFileLink(url)) {
                // 用其他应用打开, a document's link: the phone's own app, not a page in here.
                openFile(url, null);
                return true;
            }
            if (isOwn(url)) return false;
            openExternal(url);
            return true;
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            return pickedFile(request.getUrl());
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (isOwn(Uri.parse(url))) view.evaluateJavascript(CLIPBOARD_JS, null);
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            currentUrl = url;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (!request.isForMainFrame() || request.getUrl().toString().startsWith(SETUP_URL)) return;
            lastError = "连不上 " + MainActivity.this.server() + "（" + error.getDescription() + "）";
            view.loadUrl(SETUP_URL);
        }
    }

    private final class Chrome extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (pendingFiles != null) pendingFiles.onReceiveValue(null);
            pendingFiles = callback;
            boolean multiple = params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE;
            Intent intent = params.createIntent();
            if (onlyImages(params.getAcceptTypes()) && Build.VERSION.SDK_INT >= 33) {
                // A picture to pick (a photo for its look): the photo picker, not the file manager.
                intent = new Intent(MediaStore.ACTION_PICK_IMAGES).setType("image/*");
                if (multiple) intent.putExtra(MediaStore.EXTRA_PICK_IMAGES_MAX, Math.min(9, MediaStore.getPickImagesMaxLimit()));
            } else if (multiple) {
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
            }
            try {
                startActivityForResult(intent, REQUEST_FILES);
                return true;
            } catch (ActivityNotFoundException e) {
                pendingFiles = null;
                return false;
            }
        }

        @Override
        public void onShowCustomView(View view, CustomViewCallback callback) {
            enterFullscreen(view, callback);
        }

        @Override
        public void onHideCustomView() {
            exitFullscreen();
        }

        @Override
        public void onPermissionRequest(PermissionRequest request) {
            runOnUiThread(() -> handlePermission(request));
        }

        @Override
        public boolean onConsoleMessage(ConsoleMessage message) {
            if (message.messageLevel() == ConsoleMessage.MessageLevel.ERROR) {
                Log.w(TAG, message.message() + " (" + message.sourceId() + ":" + message.lineNumber() + ")");
            }
            return true;
        }
    }

    private static boolean onlyImages(String[] accept) {
        boolean any = false;
        for (String entry : accept == null ? new String[0] : accept) {
            for (String type : entry.split(",")) {
                String clean = type.trim().toLowerCase(Locale.ROOT);
                if (clean.isEmpty()) continue;
                if (!clean.startsWith("image/")) return false;
                any = true;
            }
        }
        return any;
    }

    /** Microphone for voice input, only for the bot's own page. */
    private void handlePermission(PermissionRequest request) {
        boolean audio = false;
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) audio = true;
        }
        if (!audio || !isOwn(request.getOrigin())) {
            request.deny();
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            return;
        }
        if (pendingPermission != null) pendingPermission.deny();
        pendingPermission = request;
        requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, REQUEST_MIC);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        if (requestCode == REQUEST_PHOTOS) {
            if (attachSheet != null) attachSheet.refresh(MediaLibrary.canRead(this));
            return;
        }
        if (requestCode == REQUEST_NOTIFY) {
            if (results.length > 0 && results[0] != PackageManager.PERMISSION_GRANTED) {
                Toast.makeText(this, "没有通知权限，提醒到点时手机不会响。可以在系统设置里打开", Toast.LENGTH_LONG).show();
            }
            return;
        }
        if (requestCode != REQUEST_MIC || pendingPermission == null) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            pendingPermission.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        } else {
            pendingPermission.deny();
        }
        pendingPermission = null;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQUEST_SHOOT) {
            Uri taken = shooting;
            shooting = null;
            if (resultCode != RESULT_OK || taken == null) return;
            try {
                File file = FilesProvider.fileFor(this, taken);
                if (file.length() > 0) deliver(Collections.singletonList(taken));
                else if (data != null && data.getData() != null) deliver(Collections.singletonList(data.getData()));
            } catch (java.io.FileNotFoundException ignored) {
                // Not ours: nothing taken.
            }
            return;
        }
        if (requestCode == REQUEST_ATTACH) {
            if (resultCode != RESULT_OK || data == null) return;
            java.util.ArrayList<Uri> uris = new java.util.ArrayList<>();
            ClipData clip = data.getClipData();
            if (clip != null) {
                for (int i = 0; i < clip.getItemCount(); i++) uris.add(clip.getItemAt(i).getUri());
            } else if (data.getData() != null) {
                uris.add(data.getData());
            }
            deliver(uris);
            return;
        }
        if (requestCode != REQUEST_FILES || pendingFiles == null) {
            super.onActivityResult(requestCode, resultCode, data);
            return;
        }
        Uri[] uris = null;
        if (resultCode == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null) {
                uris = new Uri[clip.getItemCount()];
                for (int i = 0; i < uris.length; i++) uris[i] = clip.getItemAt(i).getUri();
            } else if (data.getData() != null) {
                uris = new Uri[] {data.getData()};
            }
        }
        pendingFiles.onReceiveValue(uris);
        pendingFiles = null;
    }

    /**
     * Back always leads home to the companion: the full console keeps its own page
     * history, which is easy to get lost in on a phone. On the companion page, back
     * closes an open sheet first, then sends the app to the background.
     */
    @Override
    public void onBackPressed() {
        if (fullscreen != null) {
            exitFullscreen();
            return;
        }
        String server = server();
        String url = web.getUrl() == null ? "" : web.getUrl();
        if (server == null) {
            super.onBackPressed();
        } else if (!isCompanion(url)) {
            web.loadUrl(server + COMPANION_PATH);
        } else {
            web.evaluateJavascript(CLOSE_SHEET_JS, result -> {
                if (!"true".equals(result)) moveTaskToBack(true);
            });
        }
    }

    private boolean isCompanion(String url) {
        Uri uri = Uri.parse(url);
        return isOwn(uri) && uri.getPath() != null && uri.getPath().endsWith(COMPANION_PATH);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
        // The camera app may push this one out of memory while it takes the picture.
        if (shooting != null) out.putParcelable("shooting", shooting);
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        // Reminders may have changed elsewhere (another phone, the web page): set them again.
        planReminders(false);
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (voice != null) voice.shutdown();
        web.destroy();
        super.onDestroy();
    }
}
