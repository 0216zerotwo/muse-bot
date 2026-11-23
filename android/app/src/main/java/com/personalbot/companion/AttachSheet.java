package com.personalbot.companion;

import android.app.Activity;
import android.app.Dialog;
import android.content.Context;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowInsets;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.List;
import java.util.Locale;

/**
 * ＋ in the composer, as the iPhone app (and Muse) has it: 资源库 with 查看全部, a strip with the
 * camera and the phone's latest photos and videos (a tap attaches one), then 添加文件 and 添加视频.
 */
final class AttachSheet {
    interface Listener {
        void onCamera(boolean video);

        /** 查看全部: the system's photo picker. */
        void onLibrary();

        void onFiles();

        void onVideos();

        void onRecent(Uri uri);

        /** The strip's "允许访问照片" tile. */
        void onAllowPhotos();
    }

    private static final int TILE_DP = 96;
    private static final int RECENT = 24;

    private final Activity activity;
    private final Listener listener;
    private final float density;
    private Dialog dialog;
    private LinearLayout strip;
    private boolean dark;
    /** Thumbnails still loading for an older strip are dropped. */
    private volatile int generation;

    AttachSheet(Activity activity, Listener listener) {
        this.activity = activity;
        this.listener = listener;
        this.density = activity.getResources().getDisplayMetrics().density;
    }

    boolean showing() {
        return dialog != null && dialog.isShowing();
    }

    void show(boolean canRead) {
        if (activity.isFinishing() || activity.isDestroyed()) return;
        dismiss();
        dark = (activity.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
                == Configuration.UI_MODE_NIGHT_YES;
        dialog = new Dialog(activity);
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);
        View rootView = content();
        dialog.setContentView(rootView);
        Window window = dialog.getWindow();
        if (window != null) {
            window.setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
            window.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            window.setGravity(Gravity.BOTTOM);
            window.setWindowAnimations(android.R.style.Animation_InputMethod);
        }
        dialog.show();
        if (window != null) window.getDecorView().requestApplyInsets();
        fill(canRead);
    }

    /** After the photo permission was answered. */
    void refresh(boolean canRead) {
        if (!activity.isFinishing() && !activity.isDestroyed() && showing()) fill(canRead);
    }

    void dismiss() {
        generation++;
        Dialog current = dialog;
        dialog = null;
        if (current != null) {
            try {
                if (current.isShowing()) current.dismiss();
            } catch (RuntimeException ignored) {
                // Window token may already be gone during activity teardown.
            }
        }
    }

    private int dp(float value) {
        return Math.round(value * density);
    }

    private int text() {
        return dark ? 0xFFF2F2F5 : 0xFF1C1C1E;
    }

    private int secondary() {
        return dark ? 0xFF9A9AA2 : 0xFF6E6E76;
    }

    private int fill() {
        return dark ? 0x1FFFFFFF : 0x12000000;
    }

    private static final int ACCENT = 0xFFBD5A02;  // the companion's --c-primary

    @SuppressWarnings("deprecation")
    private View content() {
        LinearLayout root = new LinearLayout(activity);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(0, dp(10), 0, dp(16));
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            int bottom = Build.VERSION.SDK_INT >= 30
                    ? insets.getInsets(WindowInsets.Type.systemBars()).bottom
                    : insets.getSystemWindowInsetBottom();
            view.setPadding(0, dp(10), 0, dp(16) + Math.max(0, bottom));
            return insets;
        });
        GradientDrawable sheet = new GradientDrawable();
        float r = dp(28);
        sheet.setCornerRadii(new float[] {r, r, r, r, 0, 0, 0, 0});
        sheet.setColor(dark ? 0xFF1C1C1F : 0xFFFFFFFF);
        root.setBackground(sheet);

        View grip = new View(activity);
        GradientDrawable gripShape = new GradientDrawable();
        gripShape.setCornerRadius(dp(3));
        gripShape.setColor(dark ? 0x33FFFFFF : 0x22000000);
        grip.setBackground(gripShape);
        LinearLayout.LayoutParams gripAt = new LinearLayout.LayoutParams(dp(40), dp(5));
        gripAt.gravity = Gravity.CENTER_HORIZONTAL;
        gripAt.bottomMargin = dp(14);
        root.addView(grip, gripAt);

        LinearLayout header = new LinearLayout(activity);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(24), 0, dp(12), 0);
        TextView title = label("资源库", 17, secondary());
        title.setTypeface(Typeface.DEFAULT_BOLD);
        header.addView(title, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView all = label("查看全部", 16, ACCENT);
        all.setPadding(dp(12), dp(12), dp(12), dp(12));
        all.setOnClickListener(v -> choose(listener::onLibrary));
        header.addView(all);
        root.addView(header);

        HorizontalScrollView scroller = new HorizontalScrollView(activity);
        scroller.setHorizontalScrollBarEnabled(false);
        strip = new LinearLayout(activity);
        strip.setPadding(dp(16), dp(6), dp(16), dp(6));
        scroller.addView(strip);
        root.addView(scroller, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(TILE_DP + 12)));

        LinearLayout rows = new LinearLayout(activity);
        rows.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable group = new GradientDrawable();
        group.setCornerRadius(dp(24));
        group.setColor(fill());
        rows.setBackground(group);
        rows.addView(row("添加文件", R.drawable.ic_attach, () -> choose(listener::onFiles)));
        View divider = new View(activity);
        divider.setBackgroundColor(dark ? 0x22FFFFFF : 0x18000000);
        LinearLayout.LayoutParams line = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Math.max(1, dp(0.5f)));
        line.leftMargin = dp(62);
        rows.addView(divider, line);
        rows.addView(row("添加视频", R.drawable.ic_play_circle, () -> choose(listener::onVideos)));
        LinearLayout.LayoutParams rowsAt = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        rowsAt.setMargins(dp(16), dp(16), dp(16), 0);
        root.addView(rows, rowsAt);
        return root;
    }

    private void choose(Runnable action) {
        dismiss();
        action.run();
    }

    private TextView label(String words, float sp, int color) {
        TextView view = new TextView(activity);
        view.setText(words);
        view.setTextSize(sp);
        view.setTextColor(color);
        return view;
    }

    private View row(String words, int icon, Runnable action) {
        LinearLayout row = new LinearLayout(activity);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setMinimumHeight(dp(60));
        row.setPadding(dp(18), 0, dp(18), 0);
        row.setClickable(true);
        row.setBackgroundResource(pressable());
        row.setOnClickListener(v -> action.run());
        ImageView image = new ImageView(activity);
        image.setImageResource(icon);
        image.setImageTintList(ColorStateList.valueOf(text()));
        row.addView(image, new LinearLayout.LayoutParams(dp(26), dp(26)));
        TextView label = label(words, 17, text());
        label.setPadding(dp(18), 0, 0, 0);
        row.addView(label, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        row.setContentDescription(words);
        return row;
    }

    private int pressable() {
        android.util.TypedValue value = new android.util.TypedValue();
        activity.getTheme().resolveAttribute(android.R.attr.selectableItemBackground, value, true);
        return value.resourceId;
    }

    private GradientDrawable tileShape() {
        GradientDrawable shape = new GradientDrawable();
        shape.setCornerRadius(dp(22));
        shape.setColor(fill());
        return shape;
    }

    private LinearLayout.LayoutParams tileAt(int width) {
        LinearLayout.LayoutParams at = new LinearLayout.LayoutParams(width, dp(TILE_DP));
        at.rightMargin = dp(10);
        return at;
    }

    private View iconTile(int icon, String words, Runnable action) {
        FrameLayout tile = new FrameLayout(activity);
        tile.setBackground(tileShape());
        tile.setClickable(true);
        tile.setForeground(activity.getDrawable(pressable()));
        tile.setContentDescription(words);
        tile.setOnClickListener(v -> choose(action));
        ImageView image = new ImageView(activity);
        image.setImageResource(icon);
        image.setImageTintList(ColorStateList.valueOf(text()));
        tile.addView(image, new FrameLayout.LayoutParams(dp(30), dp(30), Gravity.CENTER));
        return tile;
    }

    /** The camera, then either the latest photos or a tile asking to allow them. */
    private void fill(boolean canRead) {
        if (strip == null) return;
        final int run = ++generation;
        strip.removeAllViews();
        strip.addView(iconTile(R.drawable.ic_camera, "拍照", () -> listener.onCamera(false)), tileAt(dp(TILE_DP)));
        strip.addView(iconTile(R.drawable.ic_videocam, "录像", () -> listener.onCamera(true)), tileAt(dp(TILE_DP)));
        if (!canRead) {
            TextView ask = label("允许访问照片后，\n最近的照片会出现在这里", 13, secondary());
            ask.setGravity(Gravity.CENTER);
            ask.setPadding(dp(14), 0, dp(14), 0);
            ask.setBackground(tileShape());
            ask.setOnClickListener(v -> listener.onAllowPhotos());
            strip.addView(ask, tileAt(ViewGroup.LayoutParams.WRAP_CONTENT));
            return;
        }
        final int px = dp(TILE_DP);
        final Context app = activity.getApplicationContext();
        new Thread(() -> {
            List<MediaLibrary.Recent> items = MediaLibrary.recent(app, RECENT);
            for (MediaLibrary.Recent item : items) {
                if (run != generation) return;
                item.thumbnail = MediaLibrary.thumbnail(app, item, px);
                if (run != generation) return;
                activity.runOnUiThread(() -> {
                    if (run == generation && !activity.isFinishing() && !activity.isDestroyed() && showing() && strip != null) {
                        strip.addView(recentTile(item), tileAt(px));
                    }
                });
            }
        }, "attach-recent").start();
    }

    private View recentTile(MediaLibrary.Recent item) {
        FrameLayout tile = new FrameLayout(activity);
        tile.setBackground(tileShape());
        tile.setClipToOutline(true);
        tile.setClickable(true);
        tile.setForeground(activity.getDrawable(pressable()));
        tile.setContentDescription(item.video ? "最近的视频" : "最近的照片");
        tile.setOnClickListener(v -> choose(() -> listener.onRecent(item.uri)));
        ImageView image = new ImageView(activity);
        image.setScaleType(ImageView.ScaleType.CENTER_CROP);
        Bitmap thumbnail = item.thumbnail;
        if (thumbnail != null) image.setImageBitmap(thumbnail);
        tile.addView(image, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        if (item.video) {
            long seconds = Math.round(item.durationMs / 1000.0);
            TextView badge = label(String.format(Locale.ROOT, "%d:%02d", seconds / 60, seconds % 60), 11, Color.WHITE);
            badge.setTypeface(Typeface.DEFAULT_BOLD);
            badge.setPadding(dp(6), dp(2), dp(6), dp(2));
            GradientDrawable pill = new GradientDrawable();
            pill.setCornerRadius(dp(10));
            pill.setColor(0x73000000);
            badge.setBackground(pill);
            FrameLayout.LayoutParams at = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM | Gravity.END);
            at.setMargins(0, 0, dp(6), dp(6));
            tile.addView(badge, at);
        }
        return tile;
    }
}
