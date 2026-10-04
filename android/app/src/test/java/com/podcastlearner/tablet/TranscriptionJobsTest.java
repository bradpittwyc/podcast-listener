package com.podcastlearner.tablet;
import org.junit.Test;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

public class TranscriptionJobsTest {
    @Test public void cancelStopsResourcesAndKeepsNextEpisode() {
        TranscriptionJobs jobs=new TranscriptionJobs();
        TranscriptionJobs.Control old=jobs.register("old"),next=jobs.register("next");
        AtomicInteger calls=new AtomicInteger();old.onCancel(calls::incrementAndGet);
        jobs.cancel("old");jobs.cancel("old");
        assertTrue(old.stopped.get());assertFalse(next.stopped.get());assertEquals(1,calls.get());
    }
    @Test public void cancellationBeforeStartupIsRemembered() {
        TranscriptionJobs jobs=new TranscriptionJobs();jobs.cancel("late");
        TranscriptionJobs.Control control=jobs.register("late");
        AtomicInteger calls=new AtomicInteger();control.onCancel(calls::incrementAndGet);
        assertTrue(control.stopped.get());assertEquals(1,calls.get());
    }
    @Test public void staleFinishDoesNotRemoveReplacement() {
        TranscriptionJobs jobs=new TranscriptionJobs();
        TranscriptionJobs.Control old=jobs.register("same"),next=jobs.register("same");
        jobs.release("same",old);assertSame(next,jobs.get("same"));assertFalse(next.stopped.get());
        jobs.release("same",next);assertNull(jobs.get("same"));
    }
}
