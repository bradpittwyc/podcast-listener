package com.podcastlearner.tablet;

import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

public class TutorPromptTest {
    @Test public void partialTranscriptIncludesEveryInputWithoutTruncation() throws Exception {
        String transcript=new String(new char[30000]).replace('\0','x')+" LAST_CURRENT_SENTENCE";
        String prompt=TutorPrompt.build(new JSONObject().put("full_transcript",transcript)
            .put("transcript_complete",false).put("question","Explain this")
            .put("selected_text","Selected example"));
        assertTrue(prompt.contains(transcript));
        assertTrue(prompt.contains("Explain this"));
        assertTrue(prompt.contains("Selected example"));
        assertTrue(prompt.contains("仍在加载或转写"));
    }
    @Test public void selectedOnlyUsesAnExplanationPrompt() throws Exception {
        String prompt=TutorPrompt.build(new JSONObject().put("selected_text","Example"));
        assertTrue(prompt.contains("请详细解析"));
        assertTrue(prompt.contains("尚未获得字幕"));
    }
    @Test public void questionWorksBeforeFirstSubtitle() throws Exception {
        assertTrue(TutorPrompt.build(new JSONObject().put("question","Help me listen")).contains("Help me listen"));
    }
    @Test(expected=IllegalArgumentException.class) public void emptyQuestionAndSelectionAreRejected() {
        TutorPrompt.build(new JSONObject());
    }
}
