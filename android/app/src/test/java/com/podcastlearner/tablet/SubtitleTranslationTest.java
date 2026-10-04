package com.podcastlearner.tablet;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

public class SubtitleTranslationTest {
    private JSONArray batch() throws Exception {
        return new JSONArray().put(new JSONObject().put("id",4).put("text","It's raining cats and dogs."))
            .put(new JSONObject().put("id",9).put("text","Stay inside."));
    }
    @Test public void translationsAreAlignedByIdRatherThanModelOrder() throws Exception {
        JSONArray result=SubtitleTranslation.translations("{\"translations\":[{\"id\":9,\"translation\":\"待在室内。\"},{\"id\":4,\"translation\":\"雨下得很大。\"}]}",batch());
        assertEquals(4,result.getJSONObject(0).getInt("id"));
        assertEquals("雨下得很大。",result.getJSONObject(0).getString("translation"));
        assertTrue(SubtitleTranslation.prompt(batch()).contains("It's raining cats and dogs."));
    }
    @Test(expected=IllegalArgumentException.class) public void duplicatedModelIdsAreRejected() throws Exception {
        SubtitleTranslation.translations("{\"translations\":[{\"id\":4,\"translation\":\"一\"},{\"id\":4,\"translation\":\"二\"}]}",batch());
    }
    @Test(expected=IllegalArgumentException.class) public void incompleteModelBatchIsRejected() throws Exception {
        SubtitleTranslation.translations("{\"translations\":[]}",batch());
    }
    @Test(expected=IllegalArgumentException.class) public void duplicateRequestIdsAreRejected() throws Exception {
        JSONArray items=batch();items.getJSONObject(1).put("id",4);
        SubtitleTranslation.sentences(new JSONObject().put("sentences",items));
    }
    @Test(expected=IllegalArgumentException.class) public void oversizedRequestIsRejected() throws Exception {
        JSONArray items=new JSONArray();for(int i=0;i<21;i++)items.put(new JSONObject().put("id",i).put("text","Text"));
        SubtitleTranslation.sentences(new JSONObject().put("sentences",items));
    }
}
