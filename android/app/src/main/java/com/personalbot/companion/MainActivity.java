package com.personalbot.companion;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.provider.Settings;
import android.speech.tts.TextToSpeech;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.MimeTypeMap;
import android.webkit.PermissionRequest;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.InputStream;
import java.lang.ref.WeakReference;
import java.net.URLDecoder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

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
 * notifications ({@link Reminders}), replies are read out with the phone's voice, text or
 * media shared from another app lands in the chat's box, and files are saved to 下载 / 相册.
 */
public class MainActivity extends Activity {
    private static final String TAG = "Companion";
    private static final String PREFS = "companion";
    private static final String KEY_SERVER = "server";
    private static final String KEY_DEVICE = "device";
    private static final String SETUP_URL = "file:///android_asset/setup.html";
    private static final String DEFAULT_SERVER = "http://127.0.0.1:8766";
    private static final String COMPANION_PATH = "/companion.html";
    /** Companion sheets, full-screen overlays, and dialogs close on a cancelable Escape keydown. */
    private static final String CLOSE_SHEET_JS =
            "(function(){var hadDialog=!!document.querySelector('[role=\"dialog\"],[data-sheet]');"
                    + "var ev=new KeyboardEvent('keydown',{key:'Escape',code:'Escape',keyCode:27,bubbles:true,cancelable:true});"
                    + "(document.activeElement||document).dispatchEvent(ev);"
                    + "return ev.defaultPrevented||hadDialog;})()";
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
    private static final String[] FILE_PATHS = {
        "/api/media/",
        "/api/webui/companion/creations/file/",
        "/api/webui/companion/avatars/file/"
    };
    /**
     * Installed on the companion page:
     * 1) clipboard writeText fallback over plain http on a home network;
     * 2) hook on window.addEventListener('companion:attach') so attachments queued while the
     *    Activity was recreated or cold-started flush as soon as Composer mounts.
     */
    private static final String PAGE_HOOK_JS =
            "(function(){var b=window.PersonalBotAndroid;"
                    + "if(!window.__pbAttachHooked){window.__pbAttachHooked=true;"
                    + "var set=window.__pbAttachListeners||(window.__pbAttachListeners=new Set());"
                    + "var origAdd=EventTarget.prototype.addEventListener;"
                    + "var origRem=EventTarget.prototype.removeEventListener;"
                    + "EventTarget.prototype.addEventListener=function(t,fn,o){"
                    + "var r=origAdd.call(this,t,fn,o);"
                    + "if(this===window&&t==='companion:attach'&&typeof fn==='function'){set.add(fn);"
                    + "setTimeout(function(){if(window.PersonalBotAndroid&&window.PersonalBotAndroid.flushAttach)"
                    + "window.PersonalBotAndroid.flushAttach();},0);}"
                    + "return r;};"
                    + "EventTarget.prototype.removeEventListener=function(t,fn,o){"
                    + "if(this===window&&t==='companion:attach'&&fn)set.delete(fn);"
                    + "return origRem.call(this,t,fn,o);};}"
                    + "if(!(window.isSecureContext&&navigator.clipboard)&&b&&b.copy){"
                    + "try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{"
                    + "writeText:function(t){b.copy(String(t));return Promise.resolve();},"
                    + "readText:function(){return Promise.reject(new Error('unsupported'));}}});}catch(e){}}"
                    + "})()";

    private static final Pattern HTTP_URL_IN_TEXT = Pattern.compile("(?i)https?://[^\\s\"'<>]+");
    private static final Pattern PAIR_PARAM = Pattern.compile("(?i)(?:[#?&]|\\b)pair=([A-Za-z0-9._~%-]+)");

    /** Weak pointer for the process-singleton ServiceWorkerController so destroyed Activities never leak. */
    private static volatile WeakReference<MainActivity> activeInstance = new WeakReference<>(null);
    private static volatile boolean swHookInstalled;

    private static final class StaticServiceWorkerClient extends ServiceWorkerClient {
        @Override
        public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
            MainActivity current = activeInstance.get();
            if (current == null || request == null) return null;
            return current.pickedFile(request.getUrl());
        }
    }

    private static void installServiceWorkerHook() {
        if (swHookInstalled) return;
        synchronized (MainActivity.class) {
            if (swHookInstalled) return;
            try {
                ServiceWorkerController.getInstance().setServiceWorkerClient(new StaticServiceWorkerClient());
                swHookInstalled = true;
            } catch (Throwable t) {
                Log.w(TAG, "service worker hook unavailable: " + t.getMessage());
            }
        }
    }

    private WebView web;
    private ValueCallback<Uri[]> pendingFiles;
    private PermissionRequest pendingPermission;
    /** Current main-frame URL; read from the bridge's thread. */
    private volatile String currentUrl = "";
    private volatile String lastError = "";
    /** Text shared to the app, until the page takes it. */
    private volatile String shared = "";
    private TextToSpeech voice;
    private volatile boolean voiceReady;
    private String pendingSpeechText;
    private String pendingSpeechLang;
    private volatile long plannedAt;
    private FrameLayout root;
    private AttachSheet attachSheet;
    /** Where the camera writes the photo or video it is taking. */
    private Uri shooting;
    private final Map<String, Uri> picked = new ConcurrentHashMap<>();
    /** URIs waiting to be dispatched as companion:attach once /companion.html Composer is mounted. */
    private final ArrayList<Uri> pendingAttachUris = new ArrayList<>();
    private boolean companionPageLoaded;
    private boolean attachFlushInFlight;
    private int attachRetryCount;
    /** A video played full screen, over the page. */
    private View fullscreen;
    private WebChromeClient.CustomViewCallback fullscreenDone;

    @Override
    @SuppressWarnings("deprecation")
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        activeInstance = new WeakReference<>(this);
        installServiceWorkerHook();
        web = new WebView(this);
        root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        setContentView(root);
        configure(web);
        if (state != null) {
            shooting = state.getParcelable("shooting");
            ArrayList<Uri> savedAttach = state.getParcelableArrayList("pendingAttachUris");
            if (savedAttach != null) {
                for (Uri uri : savedAttach) {
                    if (uri != null && pendingAttachUris.size() < 12) pendingAttachUris.add(uri);
                }
            }
            String savedShared = state.getString("sharedText");
            if (savedShared != null && !savedShared.isEmpty()) shared = savedShared;
        }
        if (state == null || web.restoreState(state) == null) {
            open();
        } else if (web.getUrl() != null) {
            currentUrl = web.getUrl();
        }
        takeShare(getIntent());
        Reminders.scheduleRefresh(this);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        ShareOutcome outcome = takeShare(intent);
        if (outcome.hasText && isAlive() && web != null && isCompanion(currentUrl)) {
            web.evaluateJavascript(SHARED_EVENT_JS, null);
        }
    }

    private static final class ShareOutcome {
        final boolean hasText;
        final boolean hasMedia;

        ShareOutcome(boolean hasText, boolean hasMedia) {
            this.hasText = hasText;
            this.hasMedia = hasMedia;
        }
    }

    /** Text or media shared from another app: text goes into {@link #shared}, files into {@link #deliver}. */
    @SuppressWarnings("deprecation")
    private ShareOutcome takeShare(Intent intent) {
        if (intent == null) return new ShareOutcome(false, false);
        String action = intent.getAction();
        boolean single = Intent.ACTION_SEND.equals(action);
        boolean multiple = Intent.ACTION_SEND_MULTIPLE.equals(action);
        if (!single && !multiple) return new ShareOutcome(false, false);

        boolean hasText = false;
        if (single) {
            CharSequence text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
            if (text != null && !text.toString().trim().isEmpty()) {
                String subject = intent.getStringExtra(Intent.EXTRA_SUBJECT);
                String body = text.toString().trim();
                boolean titled = subject != null && !subject.trim().isEmpty() && !body.contains(subject.trim());
                shared = titled ? subject.trim() + "\n" + body : body;
                hasText = true;
            }
        }

        List<Uri> sharedUris = new ArrayList<>();
        if (single) {
            Uri stream = intent.getParcelableExtra(Intent.EXTRA_STREAM);
            if (isSafeExternalShareUri(stream)) sharedUris.add(stream);
        } else {
            ArrayList<Uri> streams = intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
            if (streams != null) {
                for (Uri uri : streams) {
                    if (isSafeExternalShareUri(uri)) sharedUris.add(uri);
                }
            }
        }
        if (sharedUris.isEmpty() && !hasText) {
            ClipData clip = intent.getClipData();
            if (clip != null) {
                for (int i = 0; i < clip.getItemCount(); i++) {
                    Uri uri = clip.getItemAt(i).getUri();
                    if (isSafeExternalShareUri(uri)) sharedUris.add(uri);
                }
            }
        }
        if (!sharedUris.isEmpty()) {
            deliver(sharedUris);
        }

        // Clear action/extras so configuration changes do not replay the same share intent.
        intent.setAction(Intent.ACTION_MAIN);
        intent.removeExtra(Intent.EXTRA_TEXT);
        intent.removeExtra(Intent.EXTRA_SUBJECT);
        intent.removeExtra(Intent.EXTRA_STREAM);
        intent.setClipData(null);
        return new ShareOutcome(hasText, !sharedUris.isEmpty());
    }

    /** Rejects file:// and self-owned authorities to prevent confused-deputy reads of private app data. */
    private boolean isSafeExternalShareUri(Uri uri) {
        if (uri == null) return false;
        String scheme = uri.getScheme();
        if (!ContentResolver.SCHEME_CONTENT.equalsIgnoreCase(scheme)) return false;
        String authority = uri.getAuthority();
        if (authority == null || authority.isEmpty()) return false;
        String lowerAuth = authority.toLowerCase(Locale.ROOT);
        if (FilesProvider.AUTHORITY.equalsIgnoreCase(lowerAuth)) return false;
        if (lowerAuth.startsWith(getPackageName().toLowerCase(Locale.ROOT))) return false;
        return true;
    }

    private boolean isAlive() {
        return !isFinishing() && !isDestroyed();
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
            currentUrl = SETUP_URL;
            web.loadUrl(SETUP_URL);
        } else {
            web.loadUrl(server + COMPANION_PATH);
        }
    }

    static final class ParsedConnect {
        final String server;
        final String pairCode;

        ParsedConnect(String server, String pairCode) {
            this.server = server;
            this.pairCode = pairCode;
        }
    }

    private static int countChar(String text, char target) {
        int count = 0;
        for (int i = 0; i < text.length(); i++) {
            if (text.charAt(i) == target) count++;
        }
        return count;
    }

    private static String stripTrailingSentencePunctuation(String value) {
        if (value == null) return null;
        String out = value.trim();
        while (!out.isEmpty()) {
            char tail = out.charAt(out.length() - 1);
            boolean strip;
            if (tail == ']') {
                strip = countChar(out, ']') > countChar(out, '[');
            } else if (tail == ')') {
                strip = countChar(out, ')') > countChar(out, '(');
            } else {
                strip = tail == '.' || tail == ',' || tail == ';' || tail == '!' || tail == '?'
                        || tail == '>' || tail == '\'' || tail == '"'
                        || tail == '。' || tail == '，' || tail == '；' || tail == '！' || tail == '？'
                        || tail == '）' || tail == '】' || tail == '》' || tail == '」' || tail == '』';
            }
            if (strip) {
                out = out.substring(0, out.length() - 1).trim();
            } else {
                break;
            }
        }
        return out;
    }

    /** Parses a server address, connection URL, or pasted pairing snippet into base URL + optional pair code. */
    static ParsedConnect parseConnectInput(String raw) {
        if (raw == null) return null;
        String trimmed = raw.trim();
        if (trimmed.isEmpty()) return null;

        String pairCode = null;
        Matcher pairMatcher = PAIR_PARAM.matcher(trimmed);
        if (pairMatcher.find()) {
            String encoded = stripTrailingSentencePunctuation(pairMatcher.group(1));
            try {
                pairCode = stripTrailingSentencePunctuation(URLDecoder.decode(encoded.replace("+", "%2B"), "UTF-8"));
            } catch (Exception ignored) {
                pairCode = encoded;
            }
            if (pairCode == null || pairCode.isEmpty()) pairCode = null;
        }

        String candidate = stripTrailingSentencePunctuation(trimmed);
        Matcher urlMatcher = HTTP_URL_IN_TEXT.matcher(trimmed);
        if (urlMatcher.find()) {
            candidate = stripTrailingSentencePunctuation(urlMatcher.group(0));
        }
        String server = normalize(candidate);
        if (server == null) return null;
        return new ParsedConnect(server, pairCode);
    }

    /** {@code http(s)://host[:port][/path]} without a trailing slash, or {@code null}. */
    static String normalize(String address) {
        String text = address == null ? "" : address.trim();
        while (!text.isEmpty() && (text.startsWith("<") || text.startsWith("\"") || text.startsWith("'"))) {
            text = text.substring(1).trim();
        }
        while (!text.isEmpty() && (text.endsWith(">") || text.endsWith("\"") || text.endsWith("'"))) {
            text = text.substring(0, text.length() - 1).trim();
        }
        if (text.isEmpty()) return null;
        int colonSlash = text.indexOf("://");
        if (colonSlash > 0) {
            String rawScheme = text.substring(0, colonSlash).toLowerCase(Locale.ROOT);
            if (!"http".equals(rawScheme) && !"https".equals(rawScheme)) return null;
            text = rawScheme + text.substring(colonSlash);
        } else if (colonSlash == 0) {
            return null;
        } else {
            text = (isLikelyPlainHttpHost(text) ? "http://" : "https://") + text;
        }
        Uri uri = Uri.parse(text);
        String scheme = uri.getScheme();
        if (!"http".equals(scheme) && !"https".equals(scheme)) return null;
        if (uri.getUserInfo() != null) return null;
        String host = uri.getHost();
        if (host != null) {
            while (host.endsWith(".")) host = host.substring(0, host.length() - 1);
        }
        if (host == null || host.isEmpty()) return null;
        for (int i = 0; i < host.length(); i++) {
            if (Character.isWhitespace(host.charAt(i))) return null;
        }
        int explicitPort = uri.getPort();
        if (explicitPort != -1 && (explicitPort < 1 || explicitPort > 65535)) return null;
        String path = uri.getPath() == null ? "" : uri.getPath();
        // A pasted connection link points at the page itself.
        if (path.endsWith(COMPANION_PATH)) path = path.substring(0, path.length() - COMPANION_PATH.length());
        while (path.endsWith("/")) path = path.substring(0, path.length() - 1);
        boolean defaultPort = explicitPort == -1
                || ("http".equals(scheme) && explicitPort == 80)
                || ("https".equals(scheme) && explicitPort == 443);
        String portPart = defaultPort ? "" : ":" + explicitPort;
        return scheme + "://" + host.toLowerCase(Locale.ROOT) + portPart + path;
    }

    /** Local/LAN/Tailscale/explicit-:8766 bare hosts default to http://; public domains default to https://. */
    private static boolean isLikelyPlainHttpHost(String bareInput) {
        int end = bareInput.length();
        for (char sep : new char[] {'/', '?', '#'}) {
            int idx = bareInput.indexOf(sep);
            if (idx >= 0 && idx < end) end = idx;
        }
        String authority = bareInput.substring(0, end).trim().toLowerCase(Locale.ROOT);
        if (authority.endsWith(":8766")) return true;
        String host = authority;
        if (host.startsWith("[")) {
            int bracket = host.indexOf(']');
            if (bracket > 0) host = host.substring(0, bracket + 1);
            return "[::1]".equals(host) || host.startsWith("[fc") || host.startsWith("[fd") || host.startsWith("[fe80:");
        }
        int colon = host.lastIndexOf(':');
        if (colon > 0) host = host.substring(0, colon);
        if ("localhost".equals(host) || !host.contains(".")) return true;
        if (host.endsWith(".local") || host.endsWith(".lan") || host.endsWith(".internal") || host.endsWith(".home.arpa")) {
            return true;
        }
        String[] parts = host.split("\\.");
        if (parts.length == 4) {
            int[] octets = new int[4];
            boolean allNumeric = true;
            for (int i = 0; i < 4; i++) {
                try {
                    octets[i] = Integer.parseInt(parts[i]);
                    if (octets[i] < 0 || octets[i] > 255) allNumeric = false;
                } catch (NumberFormatException e) {
                    allNumeric = false;
                }
            }
            if (allNumeric) {
                if (octets[0] == 127 || octets[0] == 10) return true;
                if (octets[0] == 192 && octets[1] == 168) return true;
                if (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31) return true;
                if (octets[0] == 100 && octets[1] >= 64 && octets[1] <= 127) return true; // Tailscale CGNAT
                return true; // Bare IPv4 addresses rarely have public CA certs unless https:// is given
            }
        }
        return false;
    }

    private static int port(Uri uri) {
        if (uri.getPort() != -1) return uri.getPort();
        return "https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80;
    }

    /** The bundled setup page, or the same origin as the configured bot. */
    private boolean isOwn(Uri url) {
        if (url == null) return false;
        if (url.toString().startsWith("file:///android_asset/")) return true;
        String scheme = url.getScheme();
        if (!"http".equalsIgnoreCase(scheme) && !"https".equalsIgnoreCase(scheme)) return false;
        String server = server();
        if (server == null) return false;
        Uri own = Uri.parse(server);
        return own.getScheme() != null
                && own.getScheme().equalsIgnoreCase(scheme)
                && own.getHost() != null
                && own.getHost().equalsIgnoreCase(url.getHost())
                && port(own) == port(url);
    }

    private boolean isSetupOrigin() {
        String url = currentUrl;
        return url != null && url.startsWith(SETUP_URL);
    }

    private boolean isCompanionOrigin() {
        String url = currentUrl;
        if (url == null || url.isEmpty() || url.startsWith(SETUP_URL)) return false;
        Uri parsed = Uri.parse(url);
        String scheme = parsed.getScheme();
        if (!"http".equalsIgnoreCase(scheme) && !"https".equalsIgnoreCase(scheme)) return false;
        return isOwn(parsed);
    }

    private void openExternal(Uri url) {
        if (url == null || !isAlive()) return;
        Intent intent = new Intent(Intent.ACTION_VIEW, url);
        String scheme = url.getScheme();
        if ("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme)) {
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

    private Uri resolveOwnUrl(String raw) {
        if (raw == null || raw.trim().isEmpty()) return null;
        String trimmed = raw.trim();
        if (trimmed.startsWith("/")) {
            String base = server();
            if (base == null) return null;
            return Uri.parse(base + trimmed);
        }
        return Uri.parse(trimmed);
    }

    private boolean isFileLink(Uri url) {
        if (url == null || !isOwn(url)) return false;
        String path = url.getPath() == null ? "" : url.getPath();
        String server = server();
        if (server != null) {
            String basePath = Uri.parse(server).getPath();
            if (basePath != null && !basePath.isEmpty() && path.startsWith(basePath + "/")) {
                path = path.substring(basePath.length());
            }
        }
        for (String prefix : FILE_PATHS) {
            if (path.startsWith(prefix)) return true;
        }
        return false;
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
        if (!isFileLink(url) || !isAlive()) return;
        Toast.makeText(this, "正在准备…", Toast.LENGTH_SHORT).show();
        final Context app = getApplicationContext();
        final String cleanHint = (name != null && !name.trim().isEmpty()) ? name.trim() : null;
        new Thread(() -> {
            try {
                File file = MediaLibrary.fetch(app, url.toString(), cleanHint);
                runOnUiThread(() -> {
                    if (isAlive()) then.run(file);
                });
            } catch (Exception e) {
                Log.w(TAG, "file not fetched: " + e.getMessage());
                runOnUiThread(() -> {
                    if (isAlive()) Toast.makeText(this, "暂时拿不到这个文件，稍后再试", Toast.LENGTH_LONG).show();
                });
            }
        }, "file").start();
    }

    private static WebResourceResponse notFoundResponse() {
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", headers, null);
    }

    /** The answer to the page's fetch of a file picked with ＋ (null: not one of those). */
    private WebResourceResponse pickedFile(Uri url) {
        if (url == null) return null;
        String path = url.getPath();
        if (path == null || !path.startsWith(PICKED_PATH) || !isOwn(url)) return null;
        Uri file = picked.get(path.substring(PICKED_PATH.length()));
        if (file == null) return notFoundResponse();
        InputStream in = null;
        try {
            Context app = getApplicationContext();
            ContentResolver resolver = app.getContentResolver();
            in = resolver.openInputStream(file);
            if (in == null) return notFoundResponse();
            String type = resolver.getType(file);
            if (type == null || type.isEmpty()) {
                type = FilesProvider.typeOf(MediaLibrary.displayName(app, file, ""));
            }
            Map<String, String> headers = new HashMap<>();
            headers.put("Cache-Control", "no-store");
            return new WebResourceResponse(type, null, 200, "OK", headers, in);
        } catch (Exception e) {
            if (in != null) {
                try {
                    in.close();
                } catch (Exception ignored) {
                    // Best-effort stream cleanup.
                }
            }
            return notFoundResponse();
        }
    }

    // -- ＋: the attach sheet -------------------------------------------------------------------------

    private void showAttach() {
        if (!isAlive()) return;
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
        if (!isAlive() || MediaLibrary.canRead(this)) return;
        if (!prefs().getBoolean(KEY_PHOTOS_ASKED, false)) {
            prefs().edit().putBoolean(KEY_PHOTOS_ASKED, true).apply();
            requestPermissions(MediaLibrary.readPermissions(), REQUEST_PHOTOS);
        } else if (fromTile) {
            // Asked before and refused: only the system settings can change it now.
            openAppSettings();
        }
    }

    private void openAppSettings() {
        try {
            startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException ignored) {
            // Ignore on restricted profiles without Settings UI.
        }
    }

    /** The system photo picker: photos and videos (查看全部), or videos only (添加视频). */
    private void pick(boolean photos, boolean videos) {
        Intent intent;
        if (Build.VERSION.SDK_INT >= 33) {
            intent = new Intent(MediaStore.ACTION_PICK_IMAGES);
            if (!photos && videos) intent.setType("video/*");
            intent.putExtra(MediaStore.EXTRA_PICK_IMAGES_MAX, Math.min(6, MediaStore.getPickImagesMaxLimit()));
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
        if (!isAlive()) return;
        try {
            startActivityForResult(intent, request);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, request == REQUEST_SHOOT ? "这台手机没有可用的相机" : "没有能选文件的应用", Toast.LENGTH_SHORT).show();
        }
    }

    /** Queues picked files and delivers them once {@code /companion.html} Composer is mounted. */
    private void deliver(List<Uri> uris) {
        if (uris == null || uris.isEmpty()) return;
        for (Uri uri : uris) {
            if (uri == null) continue;
            if (pendingAttachUris.size() >= 12) break;
            pendingAttachUris.add(uri);
        }
        attachRetryCount = 0;
        flushPendingAttach();
    }

    private void flushPendingAttach() {
        if (!isAlive() || web == null || attachFlushInFlight || pendingAttachUris.isEmpty()) return;
        if (!companionPageLoaded || !isCompanion(currentUrl)) return;
        if (picked.size() > 64) picked.clear();

        final List<Uri> batch = new ArrayList<>(pendingAttachUris);
        final List<String> batchIds = new ArrayList<>(batch.size());
        JSONArray files = new JSONArray();
        int index = 0;
        for (Uri uri : batch) {
            index++;
            String id = UUID.randomUUID().toString().replace("-", "");
            picked.put(id, uri);
            batchIds.add(id);
            String type = getContentResolver().getType(uri);
            String fallback = "附件-" + index + "." + extensionOf(type);
            String displayName = MediaLibrary.displayName(this, uri, fallback);
            if (type == null || type.isEmpty()) {
                type = FilesProvider.typeOf(displayName);
            }
            try {
                files.put(new JSONObject()
                        .put("url", PICKED_PATH + id)
                        .put("name", displayName)
                        .put("type", "application/octet-stream".equals(type) ? "" : type));
            } catch (JSONException ignored) {
                // Plain strings will not fail JSON serialization.
            }
        }
        attachFlushInFlight = true;
        String script = "(function(){var hasSet=!!(window.__pbAttachListeners&&window.__pbAttachListeners.size>0);"
                + "var hasBtn=!!document.querySelector('button[aria-label=\"添加文件\"]');"
                + "if(!hasSet&&!hasBtn){"
                + "var tab=document.querySelector('nav[aria-label=\"主导航\"] button[aria-label^=\"主要聊天\"]:not([aria-current=\"page\"])');"
                + "if(tab)tab.click();return 'wait';}"
                + "var detail=" + files + ";"
                + "if(hasSet)window.dispatchEvent(new CustomEvent('companion:attach',{detail:detail}));"
                + "else setTimeout(function(){window.dispatchEvent(new CustomEvent('companion:attach',{detail:detail}));},0);"
                + "return 'ok';})()";
        web.evaluateJavascript(script, result -> {
            attachFlushInFlight = false;
            if (!isAlive() || web == null) return;
            if ("\"ok\"".equals(result)) {
                pendingAttachUris.removeAll(batch);
                attachRetryCount = 0;
                if (!pendingAttachUris.isEmpty()) flushPendingAttach();
            } else {
                for (String id : batchIds) picked.remove(id);
                if (attachRetryCount < 60 && isCompanion(currentUrl)) {
                    attachRetryCount++;
                    web.postDelayed(this::flushPendingAttach, 300);
                }
            }
        });
    }

    private static String extensionOf(String type) {
        String ext = FilesProvider.extensionFromMime(type);
        if (ext != null) return ext;
        String fallback = type == null ? null : MimeTypeMap.getSingleton().getExtensionFromMimeType(type);
        return fallback == null ? "bin" : fallback;
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

    /** A file from the page (报告.pdf, a picture): into 下载 or 相册, with proper UTF-8 filename and MIME. */
    private void save(String url, String disposition, String mimeType) {
        if (url == null || url.isEmpty() || !isAlive()) return;
        Uri uri = Uri.parse(url);
        String scheme = uri.getScheme();
        DownloadManager downloads = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
        if (Build.VERSION.SDK_INT < 29 || downloads == null || !("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme))) {
            openExternal(uri);
            return;
        }
        final String name = MediaLibrary.resolveFileName(url, disposition, mimeType, null);
        final String type = MediaLibrary.effectiveMimeType(mimeType, name);
        if (isOwn(uri) && (type.startsWith("image/") || type.startsWith("video/"))) {
            final Context app = getApplicationContext();
            new Thread(() -> {
                try {
                    MediaLibrary.saveToGallery(app, url, name, type);
                    runOnUiThread(() -> {
                        if (isAlive()) Toast.makeText(this, "已保存到相册", Toast.LENGTH_SHORT).show();
                    });
                } catch (Exception e) {
                    Log.w(TAG, "not saved to the gallery: " + e.getMessage());
                    runOnUiThread(() -> {
                        if (isAlive()) Toast.makeText(this, "保存失败，稍后再试", Toast.LENGTH_LONG).show();
                    });
                }
            }, "save").start();
            return;
        }
        DownloadManager.Request request;
        try {
            request = new DownloadManager.Request(uri)
                    .setTitle(name)
                    .setMimeType(type)
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
        final Context app = getApplicationContext();
        new Thread(() -> {
            try {
                if (Reminders.plan(app) > 0) {
                    runOnUiThread(() -> {
                        if (isAlive()) askToNotify(false);
                    });
                }
            } catch (Exception e) {
                Log.w(TAG, "reminders not planned: " + e.getMessage());
            }
        }, "reminders").start();
    }

    /** Android 13+ asks before an app's first notification: once, or again when the user turns it on. */
    private void askToNotify(boolean again) {
        if (!isAlive() || Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return;
        if (!again && prefs().getBoolean(KEY_NOTIFY_ASKED, false)) return;
        prefs().edit().putBoolean(KEY_NOTIFY_ASKED, true).apply();
        requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, REQUEST_NOTIFY);
    }

    /** The phone's voice for 朗读 (the WebView has none), started the first time it is needed. */
    private void speak(String text, String lang) {
        if (!isAlive() || text == null || text.isEmpty()) return;
        pendingSpeechText = text;
        pendingSpeechLang = lang;
        if (voice == null) {
            voice = new TextToSpeech(getApplicationContext(), status -> runOnUiThread(() -> {
                if (!isAlive() || voice == null) return;
                voiceReady = status == TextToSpeech.SUCCESS;
                String queuedText = pendingSpeechText;
                String queuedLang = pendingSpeechLang;
                pendingSpeechText = null;
                pendingSpeechLang = null;
                if (voiceReady && queuedText != null && !queuedText.isEmpty()) {
                    say(queuedText, queuedLang);
                }
            }));
            return;
        }
        if (voiceReady) {
            pendingSpeechText = null;
            pendingSpeechLang = null;
            say(text, lang);
        }
    }

    private void say(String text, String lang) {
        if (voice == null) return;
        Locale locale = Locale.forLanguageTag(lang == null || lang.isEmpty() ? "zh-CN" : lang);
        if (voice.isLanguageAvailable(locale) >= TextToSpeech.LANG_AVAILABLE) voice.setLanguage(locale);
        voice.speak(text, TextToSpeech.QUEUE_FLUSH, null, "reply");
    }

    /** Called from JavaScript as {@code window.PersonalBotAndroid}. */
    private final class Bridge {
        @JavascriptInterface
        public String server() {
            if (!isSetupOrigin() && !isCompanionOrigin()) return "";
            String server = MainActivity.this.server();
            return server == null ? DEFAULT_SERVER : server;
        }

        @JavascriptInterface
        public boolean hasServer() {
            if (!isSetupOrigin() && !isCompanionOrigin()) return false;
            return MainActivity.this.server() != null;
        }

        @JavascriptInterface
        public String error() {
            if (!isSetupOrigin() && !isCompanionOrigin()) return "";
            return lastError;
        }

        @JavascriptInterface
        public void retry() {
            if (!isSetupOrigin()) return;
            final String server = MainActivity.this.server();
            if (server == null) return;
            lastError = "";
            runOnUiThread(() -> {
                if (isAlive() && web != null) web.loadUrl(server + COMPANION_PATH);
            });
        }

        /**
         * The server's address, or a connection link ({@code …/companion.html#pair=CODE}): the
         * page pairs with the code at once. Returns an error message, or "" once it is opening.
         */
        @JavascriptInterface
        public String connect(String address) {
            if (!isSetupOrigin()) return "";
            ParsedConnect parsed = parseConnectInput(address);
            if (parsed == null) return "地址要像 https://你的域名，或粘贴服务器给的连接链接";
            String previous = MainActivity.this.server();
            SharedPreferences.Editor edit = prefs().edit().putString(KEY_SERVER, parsed.server);
            boolean serverChanged = !parsed.server.equals(previous);
            if (serverChanged) edit.remove(KEY_DEVICE);
            edit.apply();
            if (serverChanged) Reminders.clearInBackground(MainActivity.this);
            lastError = "";
            String suffix = "";
            if (parsed.pairCode != null && !parsed.pairCode.isEmpty()) {
                suffix = "#pair=" + Uri.encode(parsed.pairCode);
            }
            final String page = parsed.server + COMPANION_PATH + suffix;
            runOnUiThread(() -> {
                if (isAlive() && web != null) web.loadUrl(page);
            });
            return "";
        }

        /** This device's token for the configured server, "" when not paired. */
        @JavascriptInterface
        public String deviceToken() {
            return isCompanionOrigin() ? prefs().getString(KEY_DEVICE, "") : "";
        }

        /** Keep (or with "" forget) the device token, beside the page's own copy. */
        @JavascriptInterface
        public void saveDevice(String token) {
            if (!isCompanionOrigin()) return;
            boolean clearing = token == null || token.isEmpty();
            SharedPreferences.Editor edit = prefs().edit();
            if (clearing) edit.remove(KEY_DEVICE);
            else edit.putString(KEY_DEVICE, token);
            edit.apply();
            if (clearing) Reminders.clearInBackground(MainActivity.this);
        }

        @JavascriptInterface
        public void changeServer() {
            if (!isCompanionOrigin() && !isSetupOrigin()) return;
            lastError = "";
            runOnUiThread(() -> {
                if (isAlive() && web != null) web.loadUrl(SETUP_URL);
            });
        }

        @JavascriptInterface
        public void speak(String text, String lang) {
            if (!isCompanionOrigin() || text == null || text.isEmpty()) return;
            runOnUiThread(() -> MainActivity.this.speak(text, lang));
        }

        @JavascriptInterface
        public void stopSpeaking() {
            if (!isCompanionOrigin()) return;
            runOnUiThread(() -> {
                pendingSpeechText = null;
                pendingSpeechLang = null;
                if (voice != null) voice.stop();
            });
        }

        @JavascriptInterface
        public boolean speaking() {
            if (!isCompanionOrigin()) return false;
            TextToSpeech current = voice;
            return (current != null && current.isSpeaking()) || pendingSpeechText != null;
        }

        @JavascriptInterface
        public void planReminders() {
            if (!isCompanionOrigin()) return;
            MainActivity.this.planReminders(true);
        }

        @JavascriptInterface
        public boolean remindersEnabled() {
            return isCompanionOrigin() && Reminders.enabled(MainActivity.this);
        }

        @JavascriptInterface
        public void setRemindersEnabled(boolean on) {
            if (!isCompanionOrigin()) return;
            prefs().edit().putBoolean(Reminders.KEY_ENABLED, on).apply();
            if (on) {
                runOnUiThread(() -> askToNotify(true));
                MainActivity.this.planReminders(true);
            } else {
                Reminders.clearInBackground(MainActivity.this);
            }
        }

        /** ＋ in the composer: the app's own sheet (camera, latest photos, files, videos). */
        @JavascriptInterface
        public void attach() {
            if (!isCompanionOrigin()) return;
            runOnUiThread(MainActivity.this::showAttach);
        }

        /** Triggered by the page hook as soon as Composer registers its companion:attach listener. */
        @JavascriptInterface
        public void flushAttach() {
            if (!isCompanionOrigin()) return;
            runOnUiThread(() -> {
                attachRetryCount = 0;
                flushPendingAttach();
            });
        }

        @JavascriptInterface
        public void shareFile(String url, String name) {
            if (!isCompanionOrigin() || url == null) return;
            final Uri resolved = resolveOwnUrl(url);
            if (resolved == null) return;
            runOnUiThread(() -> MainActivity.this.shareFile(resolved, name));
        }

        @JavascriptInterface
        public void openFile(String url, String name) {
            if (!isCompanionOrigin() || url == null) return;
            final Uri resolved = resolveOwnUrl(url);
            if (resolved == null) return;
            runOnUiThread(() -> MainActivity.this.openFile(resolved, name));
        }

        /** A PDF's first page for its card: answered later as a companion:pdf-preview event. */
        @JavascriptInterface
        public void pdfPreview(String url, String id) {
            final Uri resolved = resolveOwnUrl(url);
            if (!isCompanionOrigin() || resolved == null || id == null || !isFileLink(resolved)) return;
            final String targetUrl = resolved.toString();
            final Context app = getApplicationContext();
            final float density = getResources().getDisplayMetrics().density;
            new Thread(() -> {
                String image = "";
                File file = null;
                try {
                    file = MediaLibrary.fetchTemp(app, targetUrl, ".pdf");
                    image = MediaLibrary.pdfFirstPage(file, Math.round(400 * density));
                } catch (Exception e) {
                    Log.w(TAG, "no PDF preview: " + e.getMessage());
                } finally {
                    if (file != null) file.delete();
                }
                final String detail;
                try {
                    detail = new JSONObject().put("id", id).put("image", image).toString();
                } catch (JSONException e) {
                    return;
                }
                runOnUiThread(() -> {
                    if (isAlive() && web != null && isCompanionOrigin()) {
                        web.evaluateJavascript(
                                "window.dispatchEvent(new CustomEvent('companion:pdf-preview',{detail:" + detail + "}))", null);
                    }
                });
            }, "pdf-preview").start();
        }

        @JavascriptInterface
        public void copy(String text) {
            if ((!isCompanionOrigin() && !isSetupOrigin()) || text == null) return;
            runOnUiThread(() -> {
                if (!isAlive()) return;
                ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                if (clipboard != null) clipboard.setPrimaryClip(ClipData.newPlainText("Muse Bot", text));
            });
        }

        /** Text shared from another app, "" when none: given to the companion's page once. */
        @JavascriptInterface
        public String takeShared() {
            if (!isCompanionOrigin()) return "";
            String text = shared;
            shared = "";
            return text;
        }
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            if (request == null) return true;
            Uri url = request.getUrl();
            if (url == null) return true;
            if (request.isForMainFrame() && isFileLink(url)) {
                // 用其他应用打开, a document's link: the phone's own app, not a page in here.
                openFile(url, null);
                return true;
            }
            if (isOwn(url)) return false;
            String scheme = url.getScheme();
            if (!request.isForMainFrame()
                    && ("about".equalsIgnoreCase(scheme)
                            || "blob".equalsIgnoreCase(scheme)
                            || "data".equalsIgnoreCase(scheme))) {
                return false;
            }
            if ("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme)) {
                openExternal(url);
            }
            return true;
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            return request == null ? null : pickedFile(request.getUrl());
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            currentUrl = url == null ? "" : url;
            companionPageLoaded = false;
            if (isCompanion(currentUrl)) {
                view.evaluateJavascript(PAGE_HOOK_JS, null);
            }
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (url != null) currentUrl = url;
            if (isOwn(Uri.parse(currentUrl)) && !currentUrl.startsWith(SETUP_URL)) {
                view.evaluateJavascript(PAGE_HOOK_JS, null);
                if (isCompanion(currentUrl)) {
                    companionPageLoaded = true;
                    flushPendingAttach();
                }
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request == null || !request.isForMainFrame()) return;
            String failingUrl = request.getUrl() == null ? "" : request.getUrl().toString();
            if (failingUrl.startsWith(SETUP_URL)) return;
            companionPageLoaded = false;
            CharSequence desc = error == null ? null : error.getDescription();
            String saved = MainActivity.this.server();
            String target = saved != null ? saved : failingUrl;
            lastError = "暂时连不上 " + target + (desc != null && desc.length() > 0 ? "（" + desc + "）" : "")
                    + "，请检查网络或确认服务器正在运行";
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
                if (multiple) intent.putExtra(MediaStore.EXTRA_PICK_IMAGES_MAX, Math.min(6, MediaStore.getPickImagesMaxLimit()));
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
        if (request == null) return;
        if (!isAlive()) {
            request.deny();
            return;
        }
        boolean audio = false;
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) audio = true;
        }
        Uri origin = request.getOrigin();
        if (!audio || origin == null || origin.toString().startsWith("file:///android_asset/") || !isOwn(origin)) {
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
                else if (data != null && data.getData() != null && isSafeExternalShareUri(data.getData())) {
                    deliver(Collections.singletonList(data.getData()));
                }
            } catch (java.io.FileNotFoundException ignored) {
                // Not ours: nothing taken.
            }
            return;
        }
        if (requestCode == REQUEST_ATTACH) {
            if (resultCode != RESULT_OK || data == null) return;
            ArrayList<Uri> uris = new ArrayList<>();
            ClipData clip = data.getClipData();
            if (clip != null) {
                for (int i = 0; i < clip.getItemCount(); i++) {
                    Uri uri = clip.getItemAt(i).getUri();
                    if (isSafeExternalShareUri(uri)) uris.add(uri);
                }
            } else if (isSafeExternalShareUri(data.getData())) {
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
            ArrayList<Uri> safeList = new ArrayList<>();
            ClipData clip = data.getClipData();
            if (clip != null) {
                for (int i = 0; i < clip.getItemCount(); i++) {
                    Uri uri = clip.getItemAt(i).getUri();
                    if (isSafeExternalShareUri(uri)) safeList.add(uri);
                }
            } else if (isSafeExternalShareUri(data.getData())) {
                safeList.add(data.getData());
            }
            if (!safeList.isEmpty()) uris = safeList.toArray(new Uri[0]);
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
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (fullscreen != null) {
            exitFullscreen();
            return;
        }
        String server = server();
        String url = web.getUrl() == null ? "" : web.getUrl();
        if (server == null || url.startsWith(SETUP_URL)) {
            if (server != null && lastError.isEmpty()) {
                web.loadUrl(server + COMPANION_PATH);
            } else {
                super.onBackPressed();
            }
        } else if (!isCompanion(url)) {
            web.loadUrl(server + COMPANION_PATH);
        } else {
            web.evaluateJavascript(CLOSE_SHEET_JS, result -> {
                if (!"true".equals(result) && isAlive()) moveTaskToBack(true);
            });
        }
    }

    private boolean isCompanion(String url) {
        if (url == null || url.isEmpty() || url.startsWith(SETUP_URL)) return false;
        Uri uri = Uri.parse(url);
        return isOwn(uri) && uri.getPath() != null && uri.getPath().endsWith(COMPANION_PATH);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        if (web != null) web.saveState(out);
        // The camera or picker app may push this Activity out of memory before returning.
        if (shooting != null) out.putParcelable("shooting", shooting);
        if (!pendingAttachUris.isEmpty()) {
            out.putParcelableArrayList("pendingAttachUris", new ArrayList<>(pendingAttachUris));
        }
        if (shared != null && !shared.isEmpty()) {
            out.putString("sharedText", shared);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        activeInstance = new WeakReference<>(this);
        if (web != null) web.onResume();
        // Reminders may have changed elsewhere (another phone, the web page): set them again.
        planReminders(false);
    }

    @Override
    protected void onPause() {
        if (web != null) web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (attachSheet != null) {
            attachSheet.dismiss();
            attachSheet = null;
        }
        if (pendingPermission != null) {
            pendingPermission.deny();
            pendingPermission = null;
        }
        if (pendingFiles != null) {
            pendingFiles.onReceiveValue(null);
            pendingFiles = null;
        }
        pendingSpeechText = null;
        pendingSpeechLang = null;
        if (voice != null) {
            voice.shutdown();
            voice = null;
        }
        if (activeInstance.get() == this) {
            activeInstance = new WeakReference<>(null);
        }
        if (web != null) {
            ViewGroup parent = (ViewGroup) web.getParent();
            if (parent != null) parent.removeView(web);
            web.stopLoading();
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
