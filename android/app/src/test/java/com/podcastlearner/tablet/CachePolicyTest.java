package com.podcastlearner.tablet;

import org.junit.Test;
import org.junit.Rule;
import org.junit.rules.TemporaryFolder;
import java.io.File;
import java.io.FileOutputStream;
import java.util.Collections;
import static org.junit.Assert.*;

public class CachePolicyTest {
    @Rule public TemporaryFolder temp = new TemporaryFolder();
    private File write(String name,long modified) throws Exception {
        File file=new File(temp.getRoot(),name);
        try(FileOutputStream out=new FileOutputStream(file)){out.write(new byte[10]);}
        assertTrue(file.setLastModified(modified));return file;
    }
    @Test public void evictsOldGroupWithoutTouchingSettingsOrActiveJobs() throws Exception {
        String old=String.join("",Collections.nCopies(64,"a"));
        String active=String.join("",Collections.nCopies(64,"b"));
        File first=write(old+"-aliyun-sentences-v2.json",1);
        File checkpoint=write(old+"-aliyun-sentences-v2.json.checkpoint",1);
        File current=write(active+"-aliyun-sentences-v2.json",1);
        File settings=write("settings.json",1);
        assertEquals(2,CachePolicy.prune(temp.getRoot(),Collections.singleton(active),0,1000));
        assertFalse(first.exists());assertFalse(checkpoint.exists());
        assertTrue(current.exists());assertTrue(settings.exists());
    }
}
