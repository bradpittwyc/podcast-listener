package com.podcastlearner.tablet;

import java.io.EOFException;
import javax.net.ssl.SSLHandshakeException;
import org.junit.Test;
import static org.junit.Assert.*;

public class NativeBackendTest {
    @Test public void realtimeTranscriptionAlwaysUsesKeyOne() throws Exception {
        org.json.JSONObject config=NativeBackend.object("aliyun_key_1"," first-key ","aliyun_key_2","second-key");
        assertEquals("first-key",NativeBackend.transcriptionKey(config));
        config.put("aliyun_key_2","another-key");
        assertEquals("first-key",NativeBackend.transcriptionKey(config));
    }
    @Test public void realtimeTranscriptionDoesNotFallBackToKeyTwo() throws Exception {
        try {
            NativeBackend.transcriptionKey(NativeBackend.object("aliyun_key_1"," ","aliyun_key_2","private-second-key"));
            fail("Missing Key 1 should require configuration");
        } catch(AliyunStream.Failure error) {
            assertFalse(error.retryable);
            assertTrue(error.getMessage().contains("Key 1"));
            assertFalse(error.getMessage().contains("private-second-key"));
        }
    }
    @Test public void tutorAllowsPlainTextWhileDictionaryAndTranslationRequestJson() throws Exception {
        org.json.JSONObject tutor=NativeBackend.qwenPayload("Explain the subtitles",false);
        assertFalse(tutor.has("response_format"));
        assertEquals("qwen-flash",tutor.getString("model"));
        assertEquals("Explain the subtitles",tutor.getJSONArray("messages").getJSONObject(0).getString("content"));
        assertEquals("json_object",NativeBackend.qwenPayload("Translate",true).getJSONObject("response_format").getString("type"));
    }
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
