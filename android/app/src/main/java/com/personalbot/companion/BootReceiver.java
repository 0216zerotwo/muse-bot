package com.personalbot.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** The phone forgets its alarms when it restarts (and on an app update): set the reminders again. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) return;
        final PendingResult result = goAsync();
        final Context app = context.getApplicationContext();
        new Thread(() -> {
            try {
                Reminders.plan(app);
            } catch (Exception ignored) {
                // No network yet: the periodic refresh sets them later.
            } finally {
                result.finish();
            }
        }, "reminders-boot").start();
    }
}
