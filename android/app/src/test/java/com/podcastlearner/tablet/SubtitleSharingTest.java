package com.podcastlearner.tablet;

import org.junit.Test;
import static org.junit.Assert.*;

public class SubtitleSharingTest {
    @Test public void filenameCannotEscapeShareDirectory() {
        assertEquals(".._.._episode.txt",SubtitleSharing.filename("../../episode"));
        assertEquals("播客字幕.txt",SubtitleSharing.filename(".."));
        assertFalse(SubtitleSharing.filename("a/b\\c").contains("/"));
        assertFalse(SubtitleSharing.filename("a/b\\c").contains("\\"));
    }
}
