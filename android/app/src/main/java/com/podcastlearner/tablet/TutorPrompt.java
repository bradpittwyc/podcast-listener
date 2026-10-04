package com.podcastlearner.tablet;

import org.json.JSONObject;

final class TutorPrompt {
    static String build(JSONObject body) {
        String question=body.optString("question").trim();
        String selected=body.optString("selected_text").trim();
        String transcript=body.optString("full_transcript").trim();
        if(question.isEmpty() && selected.isEmpty())throw new IllegalArgumentException("请输入问题或勾选字幕例句");
        if(question.isEmpty())question="请详细解析勾选字幕句子的语法结构、生词习语和地道用法。";
        return "你是专业、耐心的英语播客学习助教，以中文回答，可使用 Markdown。"
            +"结合当前已获得的全部字幕解释上下文、生词、习语和语法，并根据用户的问题回答。"
            +"字幕可能仍在转写，请仅根据提供的内容分析，不要将未完成的字幕当作全集，也不要编造未提供的内容。\n"
            +"【字幕状态】\n"+(body.optBoolean("transcript_complete")?"已完成":"仍在加载或转写")
            +"\n【当前已获得的全部字幕】\n"+(transcript.isEmpty()?"尚未获得字幕":transcript)
            +"\n【用户勾选的字幕例句】\n"+selected+"\n【用户输入的问题】\n"+question;
    }
}
