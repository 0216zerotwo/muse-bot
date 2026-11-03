package com.personalbot.companion;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * The phone's own notifications for reminders coming up. This app gets no push, so a reminder
 * (喝水, 每日早报) would otherwise only show once the app is opened. The server says which jobs
 * speak to the user on time ({@code notice}) and when they run over the next two days
 * ({@code upcoming_ms}); all of them are set again each time, so a reminder paused, moved or
 * deleted never goes off from an old plan. Same rules as the iPhone app's ReminderPlan.
 */
final class Reminders {
    private static final String TAG = "Companion";
    static final String PREFS = "companion";
    /** 设置 → 通知 (on unless turned off). */
    static final String KEY_ENABLED = "reminders";
    private static final String KEY_PLANNED = "reminders-planned";
    static final String CHANNEL = "reminders";
    /** Alarms are set at most this many at a time (two days of hourly ones fit). */
    static final int LIMIT = 60;
    private static final int FIRST_CODE = 1000;
    private static final int REFRESH_JOB = 7;
    /** The plan covers two days: it is set again at least this often while the app stays closed. */
    private static final long REFRESH_EVERY_MS = 6L * 60 * 60 * 1000;

    private Reminders() {}

    static final class Time {
        final String id;
        final long at;
        final String body;

        Time(String id, long at, String body) {
            this.id = id;
            this.at = at;
            this.body = body;
        }
    }

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean enabled(Context context) {
        return prefs(context).getBoolean(KEY_ENABLED, true);
    }

    /** What to set: enabled jobs that speak on time, not one already going off, soonest first. */
    static List<Time> times(JSONArray jobs, long now) {
        List<Time> result = new ArrayList<>();
        for (int i = 0; jobs != null && i < jobs.length(); i++) {
            JSONObject job = jobs.optJSONObject(i);
            if (job == null || !job.optBoolean("enabled", false)) continue;
            String body = job.optString("notice", "").trim();
            JSONArray upcoming = job.optJSONArray("upcoming_ms");
            if (body.isEmpty() || upcoming == null) continue;
            for (int k = 0; k < upcoming.length(); k++) {
                long at = upcoming.optLong(k, 0);
                if (at - now <= 5000) continue;
                result.add(new Time(job.optString("id", "") + "-" + at, at, body));
            }
        }
        Collections.sort(result, (a, b) -> Long.compare(a.at, b.at));
        return result.size() > LIMIT ? new ArrayList<>(result.subList(0, LIMIT)) : result;
    }

    /** Fetch the plan and set it, off the main thread. */
    static void planInBackground(Context context) {
        final Context app = context.getApplicationContext();
        new Thread(() -> {
            try {
                plan(app);
            } catch (Exception e) {
                Log.w(TAG, "reminders not planned: " + e.getMessage());
            }
        }, "reminders").start();
    }

    /** Blocking: asks the server, then sets the alarms. Returns how many were set. */
    static int plan(Context context) throws Exception {
        SharedPreferences prefs = prefs(context);
        String server = prefs.getString("server", null);
        if (server == null) return 0;
        if (!enabled(context)) {
            schedule(context, Collections.<Time>emptyList(), "");
            return 0;
        }
        String token = bootstrap(server, prefs.getString("device", ""));
        JSONObject automations = new JSONObject(get(server + "/api/webui/automations", token));
        String title = "Muse Bot";
        try {
            title = new JSONObject(get(server + "/api/webui/companion", token)).optString("name", title);
        } catch (Exception e) {
            Log.w(TAG, "no companion name: " + e.getMessage());
        }
        List<Time> times = times(automations.optJSONArray("jobs"), System.currentTimeMillis());
        schedule(context, times, title.isEmpty() ? "Muse Bot" : title);
        return times.size();
    }

    /** Cancel the last plan's alarms, then set these. */
    static void schedule(Context context, List<Time> times, String title) {
        try {
            setAlarms(context, times, title);
        } catch (RuntimeException e) {
            Log.w(TAG, "reminders not set: " + e.getMessage());
        }
    }

    private static void setAlarms(Context context, List<Time> times, String title) {
        AlarmManager alarms = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        SharedPreferences prefs = prefs(context);
        int planned = prefs.getInt(KEY_PLANNED, 0);
        for (int i = 0; i < planned; i++) {
            PendingIntent old = PendingIntent.getBroadcast(context, FIRST_CODE + i, new Intent(context, ReminderReceiver.class),
                    PendingIntent.FLAG_NO_CREATE | PendingIntent.FLAG_IMMUTABLE);
            if (old != null) {
                alarms.cancel(old);
                old.cancel();
            }
        }
        // Exact when the phone allows it (Android 12+ asks the user); otherwise within minutes.
        boolean exact = Build.VERSION.SDK_INT < 31 || alarms.canScheduleExactAlarms();
        for (int i = 0; i < times.size(); i++) {
            Time time = times.get(i);
            Intent intent = new Intent(context, ReminderReceiver.class)
                    .putExtra(ReminderReceiver.EXTRA_TITLE, title)
                    .putExtra(ReminderReceiver.EXTRA_BODY, time.body)
                    .putExtra(ReminderReceiver.EXTRA_ID, time.id);
            PendingIntent pending = PendingIntent.getBroadcast(context, FIRST_CODE + i, intent,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            if (exact) {
                alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, time.at, pending);
            } else {
                alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, time.at, pending);
            }
        }
        prefs.edit().putInt(KEY_PLANNED, times.size()).apply();
    }

    /** Keep the two-day plan filled while the app stays closed (survives a reboot). */
    static void scheduleRefresh(Context context) {
        // Only a convenience: the app must open whatever the phone thinks of it.
        try {
            JobScheduler jobs = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
            if (jobs == null || jobs.getPendingJob(REFRESH_JOB) != null) return;
            JobInfo job = new JobInfo.Builder(REFRESH_JOB, new ComponentName(context, RefreshJob.class))
                    .setPeriodic(REFRESH_EVERY_MS)
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setPersisted(true)
                    .build();
            jobs.schedule(job);
        } catch (RuntimeException e) {
            Log.w(TAG, "no background refresh: " + e.getMessage());
        }
    }

    /** An API token: with this device's token, or none over USB (the server's own machine). */
    private static String bootstrap(String server, String device) throws Exception {
        HttpURLConnection connection = open(server + "/webui/bootstrap");
        if (device != null && !device.isEmpty()) connection.setRequestProperty("Authorization", "Device " + device);
        return new JSONObject(read(connection)).getString("api_token");
    }

    private static String get(String url, String token) throws Exception {
        HttpURLConnection connection = open(url);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        return read(connection);
    }

    private static HttpURLConnection open(String url) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(10_000);
        connection.setReadTimeout(15_000);
        connection.setRequestProperty("Accept", "application/json");
        return connection;
    }

    private static String read(HttpURLConnection connection) throws Exception {
        try {
            int status = connection.getResponseCode();
            if (status / 100 != 2) throw new IllegalStateException("HTTP " + status + " " + connection.getURL().getPath());
            try (InputStream in = connection.getInputStream()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buffer = new byte[8192];
                for (int n; (n = in.read(buffer)) > 0; ) out.write(buffer, 0, n);
                return new String(out.toByteArray(), StandardCharsets.UTF_8);
            }
        } finally {
            connection.disconnect();
        }
    }
}
