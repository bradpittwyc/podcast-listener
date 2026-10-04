package com.podcastlearner.tablet;

import java.io.EOFException;
import javax.net.ssl.SSLHandshakeException;
import org.junit.Test;
import static org.junit.Assert.*;

public class NativeBackendTest {
    @Test public void automaticDictionaryTracksProxyChanges() {
        assertEquals("qwen",NativeBackend.chooseDictionaryProvider("auto",false));
        assertEquals("gemini",NativeBackend.chooseDictionaryProvider("auto",true));
        assertEquals("qwen",NativeBackend.chooseDictionaryProvider("auto",false));
    }
    @Test public void explicitDictionaryChoiceOverridesAutomaticRouting() {
        assertEquals("qwen",NativeBackend.chooseDictionaryProvider("qwen",true));
        assertEquals("gemini",NativeBackend.chooseDictionaryProvider("gemini",false));
    }
    @Test public void tlsHandshakeFailureIsRecognizedEvenWithNestedEof() {
        SSLHandshakeException error=new SSLHandshakeException("private diagnostic");
        error.initCause(new EOFException("private EOF"));
        String message=NativeBackend.safeError(error);
        assertTrue(message.contains("安全连接失败"));
        assertFalse(message.contains("private"));
    }
}
