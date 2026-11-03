package com.personalbot.companion;

import android.app.job.JobParameters;
import android.app.job.JobService;

/** Every few hours, with a network: the reminders of the next two days set again (see {@link Reminders}). */
public class RefreshJob extends JobService {
    @Override
    public boolean onStartJob(final JobParameters params) {
        new Thread(() -> {
            boolean retry = false;
            try {
                Reminders.plan(getApplicationContext());
            } catch (Exception e) {
                retry = true;
            }
            jobFinished(params, retry);
        }, "reminders-refresh").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;
    }
}
