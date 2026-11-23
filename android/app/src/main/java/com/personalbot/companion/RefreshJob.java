package com.personalbot.companion;

import android.app.job.JobParameters;
import android.app.job.JobService;

/** Every few hours, with a network: the reminders of the next two days set again (see {@link Reminders}). */
public class RefreshJob extends JobService {
    private volatile Thread worker;

    @Override
    public boolean onStartJob(final JobParameters params) {
        Thread thread = new Thread(() -> {
            boolean retry = false;
            try {
                Reminders.plan(getApplicationContext());
            } catch (Reminders.AuthException e) {
                retry = false;
            } catch (Exception e) {
                retry = true;
            } finally {
                worker = null;
                jobFinished(params, retry);
            }
        }, "reminders-refresh");
        worker = thread;
        thread.start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        Thread current = worker;
        if (current != null) current.interrupt();
        return true;
    }
}
