package com.podcastlearner.tablet;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

public class AliyunStreamTest {
    @Test public void subwordPiecesUseCanonicalSentenceSpacing() throws Exception {
        String[] pieces={"The","failures","and","cru","elt","ies","of","Trump"};JSONArray words=new JSONArray();
        for(int i=0;i<pieces.length;i++)words.put(NativeBackend.object("begin_time",i*500,"end_time",i*500+400,"text",pieces[i],"punctuation",i==7?".":""));
        JSONArray result=AliyunStream.sentenceCues(NativeBackend.object("begin_time",0,"end_time",3900,
            "text","The failures and cruelties of Trump.","words",words),30);
        assertEquals(1,result.length());
        assertEquals("The failures and cruelties of Trump.",result.getJSONObject(0).getString("text"));
        assertEquals(33.9,result.getJSONObject(0).getDouble("end"),.0001);
    }
    @Test public void punctuationUsesRealWordTimestamps() throws Exception {
        JSONObject sentence=NativeBackend.object("begin_time",600,"end_time",5900,"text","Hello world. How are you?",
            "words",new JSONArray()
                .put(NativeBackend.object("begin_time",600,"end_time",900,"text","Hello"))
                .put(NativeBackend.object("begin_time",1000,"end_time",1700,"text","world","punctuation","."))
                .put(NativeBackend.object("begin_time",4500,"end_time",4900,"text","How"))
                .put(NativeBackend.object("begin_time",5000,"end_time",5200,"text","are"))
                .put(NativeBackend.object("begin_time",5400,"end_time",5900,"text","you","punctuation","?")));
        JSONArray result=AliyunStream.sentenceCues(sentence,30);
        assertEquals(2,result.length());
        assertEquals("Hello world.",result.getJSONObject(0).getString("text"));
        assertEquals(30.6,result.getJSONObject(0).getDouble("start"),.0001);
        assertEquals(31.7,result.getJSONObject(0).getDouble("end"),.0001);
        assertEquals(34.5,result.getJSONObject(1).getDouble("start"),.0001);
        assertEquals(35.9,result.getJSONObject(1).getDouble("end"),.0001);
    }
    @Test public void fragmentsAndContractionsJoinAcrossTasks() throws Exception {
        JSONObject pending=AliyunStream.append(NativeBackend.object("start",1,"end",29,"text","It isn"),
            NativeBackend.object("start",30,"end",33,"text","'t finished"));
        assertFalse(AliyunStream.endsSentence(pending.getString("text")));
        pending=AliyunStream.append(pending,NativeBackend.object("start",34,"end",36,"text","until this point."));
        assertEquals("It isn't finished until this point.",pending.getString("text"));
        assertEquals(1,pending.getDouble("start"),.0001);
        assertEquals(36,pending.getDouble("end"),.0001);
        assertTrue(AliyunStream.endsSentence(pending.getString("text")));
    }
    @Test public void invalidKeyIsPermanentAndRateLimitRetries() throws Exception {
        assertFalse(AliyunStream.taskError(NativeBackend.object("error_code","InvalidApiKey","error_message","secret-key")).retryable);
        assertFalse(AliyunStream.taskError(NativeBackend.object("error_code","InvalidApiKey","error_message","secret-key")).getMessage().contains("secret-key"));
        assertTrue(AliyunStream.taskError(NativeBackend.object("error_code","Throttling")).retryable);
    }
    @Test public void missingAlignmentDoesNotFabricateWordTimes() throws Exception {
        JSONArray result=AliyunStream.sentenceCues(NativeBackend.object("begin_time",1000,"end_time",9500,"text","Speech without alignment."),60);
        assertEquals(1,result.length());
        assertEquals(61,result.getJSONObject(0).getDouble("start"),.0001);
        assertEquals(69.5,result.getJSONObject(0).getDouble("end"),.0001);
    }
}
