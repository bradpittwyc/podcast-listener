package com.podcastlearner.tablet;

import java.io.EOFException;
import javax.net.ssl.SSLHandshakeException;
import org.junit.Test;
import static org.junit.Assert.*;

public class NativeBackendTest {
    @Test public void studyPromptIncludesAllPartialSubtitlesQuestionAndExamples() {
        String transcript=new String(new char[30000]).replace('\0','x')+" END_OF_CURRENT_SUBTITLES";
        String prompt=NativeBackend.studyPrompt(transcript,"Explain this expression.","[00:05] Selected example.",false);
        assertTrue(prompt.contains(transcript));
        assertTrue(prompt.contains("Explain this expression."));
        assertTrue(prompt.contains("[00:05] Selected example."));
        assertTrue(prompt.contains("仍在转写"));
    }
    @Test public void studyPromptDefaultsToExplainingSelectedExamplesWithoutQuestion() {
        String prompt=NativeBackend.studyPrompt("Available subtitles.","","Selected example.",true);
        assertTrue(prompt.contains("Selected example."));
        assertTrue(prompt.contains("请详细解析勾选字幕句子"));
        assertTrue(prompt.contains("已全部转写完成"));
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
