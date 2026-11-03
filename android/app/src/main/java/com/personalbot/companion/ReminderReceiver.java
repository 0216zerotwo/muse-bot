package com.personalbot.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** A reminder is due (see {@link Reminders}): its notification; a tap opens the companion. */
public class ReminderReceiver extends BroadcastReceiver {
    static final String EXTRA_TITLE = "title";
    static final String EXTRA_BODY = "body";
    static final String EXTRA_ID = "id";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Reminders.enabled(context)) return;
        String body = intent.getStringExtra(EXTRA_BODY);
        if (body == null || body.isEmpty()) return;
        String title = intent.getStringExtra(EXTRA_TITLE);
        String id = intent.getStringExtra(EXTRA_ID);
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) return;
        ensureChannel(context, manager);
        Intent open = new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent tap = PendingIntent.getActivity(context, 0, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(context, Reminders.CHANNEL)
                : new Notification.Builder(context);
        builder.setSmallIcon(R.drawable.ic_stat_companion)
                .setContentTitle(title == null || title.isEmpty() ? "Muse Bot" : title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(tap)
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_REMINDER);
        if (Build.VERSION.SDK_INT < 26) builder.setDefaults(Notification.DEFAULT_ALL).setPriority(Notification.PRIORITY_HIGH);
        manager.notify(id == null ? body : id, 1, builder.build());
    }

    static void ensureChannel(Context context, NotificationManager manager) {
        if (Build.VERSION.SDK_INT < 26 || manager.getNotificationChannel(Reminders.CHANNEL) != null) return;
        NotificationChannel channel = new NotificationChannel(Reminders.CHANNEL, "提醒", NotificationManager.IMPORTANCE_HIGH);
        channel.setDescription("你让伙伴设的提醒，到点时通知你");
        manager.createNotificationChannel(channel);
    }
}
