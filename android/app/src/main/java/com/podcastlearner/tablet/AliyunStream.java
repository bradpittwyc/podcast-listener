package com.podcastlearner.tablet;

import org.json.JSONArray;
import org.json.JSONObject;
import okhttp3.*;
import okio.ByteString;
import java.io.IOException;
import java.net.Proxy;
import java.util.HashSet;
import java.util.Set;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** One recoverable duplex ASR task. PCM uploads and stable results run concurrently. */
final class AliyunStream implements AutoCloseable {
    static final OkHttpClient CLIENT = new OkHttpClient.Builder().proxy(Proxy.NO_PROXY)
        .connectTimeout(20, TimeUnit.SECONDS).readTimeout(0, TimeUnit.SECONDS)
        .pingInterval(15, TimeUnit.SECONDS).build();
    static final String MODEL = "qwen-audio-3.1-asr-flash-streaming";
    final double offset;
    final int index;
    volatile double duration;
    volatile long lastEvent = System.nanoTime(), finishStarted;
    volatile IOException failure;
    volatile double windowStart = Double.NEGATIVE_INFINITY, windowEnd = Double.POSITIVE_INFINITY;
    final BlockingQueue<JSONObject> messages = new ArrayBlockingQueue<>(32);
    private final AtomicBoolean closed = new AtomicBoolean();
    private final CountDownLatch started = new CountDownLatch(1);
    private final String id = UUID.randomUUID().toString().replace("-", "");
    private final Set<String> seen = new HashSet<>();
    private final WebSocket socket;
    private Runnable removeCancel=()->{};

    static final class Failure extends IOException {
        final boolean retryable;
        Failure(String message, boolean retryable) { super(message); this.retryable = retryable; }
    }
    static Failure taskError(JSONObject header) {
        String code = header.optString("error_code").toLowerCase(java.util.Locale.ROOT);
        boolean permanent = code.matches(".*(auth|apikey|api_key|invalidparameter|accessdenied|modelnotfound|arrearage|quota|freetieronly).*");
        return new Failure(permanent ? "阿里云转写额度或模型权限不可用，请检查阿里云模型额度设置。" : "阿里云暂时失败，正在从断点重连。", !permanent);
    }
    AliyunStream(String key, String region, double offset, int index) throws Exception {
        this(CLIENT,key,region,offset,index);
    }
    AliyunStream(OkHttpClient client, String key, String region, double offset, int index) throws Exception {
        this(client,key,region,offset,index,new TranscriptionJobs.Control());
    }
    AliyunStream(OkHttpClient client, String key, String region, double offset, int index,TranscriptionJobs.Control control) throws Exception {
        this.offset = offset; this.index = index;
        if(control.stopped.get())throw new IOException("Cancelled");
        String host = region.equals("singapore") ? "dashscope-intl.aliyuncs.com" : "dashscope.aliyuncs.com";
        Request request = new Request.Builder().url("wss://" + host + "/api-ws/v1/inference")
            .header("Authorization", "Bearer " + key).build();
        socket = client.newWebSocket(request, new WebSocketListener() {
            @Override public void onOpen(WebSocket ws, Response response) {
                if(control.stopped.get()){ws.cancel();return;}
                try {
                    JSONObject command = NativeBackend.object("header", NativeBackend.object("action", "run-task", "task_id", id, "streaming", "duplex"),
                        "payload", NativeBackend.object("task_group", "audio", "task", "asr", "function", "recognition", "model", MODEL,
                            "parameters", NativeBackend.object("format", "pcm", "sample_rate", 16000, "max_sentence_silence", 500,
                                "multi_threshold_mode_enabled", true, "heartbeat", true), "input", new JSONObject()));
                    if (!ws.send(command.toString())) fail(new Failure("阿里云启动任务失败。", true));
                } catch (Exception e) { fail(new Failure("阿里云启动任务失败。", true)); }
            }
            @Override public void onMessage(WebSocket ws, String text) {
                lastEvent = System.nanoTime();
                try {
                    JSONObject data = new JSONObject(text), header = data.getJSONObject("header");
                    String event = header.optString("event");
                    if (event.equals("task-started")) { started.countDown(); return; }
                    if (event.equals("task-failed")) { fail(taskError(header)); return; }
                    if (event.equals("task-finished")) { enqueue(NativeBackend.object("status", "finished")); return; }
                    JSONObject payload = data.optJSONObject("payload"), output = payload == null ? null : payload.optJSONObject("output");
                    JSONObject sentence = output == null ? null : output.optJSONObject("sentence");
                    if (!event.equals("result-generated") || sentence == null || !sentence.optBoolean("sentence_end") || sentence.optBoolean("heartbeat")) return;
                    String ident = sentence.optString("sentence_id") + ":" + sentence.optString("begin_time") + ":" + sentence.optString("end_time");
                    if (!seen.add(ident) || sentence.optString("text").trim().isEmpty()) return;
                    JSONArray cues = sentenceCues(sentence, offset, windowStart, windowEnd);
                    for (int i=0; i<cues.length(); i++) enqueue(NativeBackend.object("status", "cue", "cue", cues.getJSONObject(i)));
                } catch (Exception e) { fail(new Failure("阿里云返回的字幕格式异常，请重连。", true)); }
            }
            @Override public void onFailure(WebSocket ws, Throwable error, Response response) {
                if(closed.get()){if(response!=null)response.close();return;}
                int status = response == null ? 0 : response.code();
                android.util.Log.w("PodcastBackend","ASR connection "+error.getClass().getSimpleName()+" HTTP "+status);
                fail(new Failure("阿里云连接失败，请检查网络、密钥和地域。", status != 400 && status != 401 && status != 403 && status != 404));
                if (response != null) response.close();
            }
            @Override public void onClosed(WebSocket ws, int code, String reason) {
                if (!closed.get()) fail(new Failure("阿里云连接中断，正在从断点重连。", true));
            }
        });
        removeCancel=control.onCancel(this::close);
        try {
            long startDeadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(25);
            while(!started.await(100,TimeUnit.MILLISECONDS)){
                if(control.stopped.get()||closed.get())throw new IOException("Cancelled");
                if(System.nanoTime()>startDeadline)throw new Failure("阿里云启动任务超时。", true);
            }
            if(control.stopped.get()||closed.get())throw new IOException("Cancelled");
            if (failure != null) throw failure;
        } catch (Exception e) { close(); throw e; }
    }
    private void fail(IOException error) { if (!closed.get()) { failure = error; started.countDown(); } }
    private void enqueue(JSONObject message) throws IOException {
        if (!closed.get() && !messages.offer(message)) throw new Failure("字幕处理积压，正在从断点重连。", true);
    }
    void send(byte[] pcm) throws Exception {
        check();
        // Bound pending network bytes; do not buffer the entire episode in OkHttp.
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        while (socket.queueSize() > 64000) {
            check(); if (System.nanoTime() > deadline) throw new Failure("阿里云音频上传超时。", true);
            Thread.sleep(20);
        }
        duration += pcm.length / 32000.0;
        if (!socket.send(ByteString.of(pcm))) throw new Failure("阿里云音频上传中断。", true);
        Thread.sleep(Math.max(1, pcm.length / 32)); // Stream 16 kHz mono PCM at real-time speed.
    }
    void finish() throws Exception {
        if(finishStarted!=0)return;
        check(); finishStarted = System.nanoTime();
        if (!socket.send(NativeBackend.object("header", NativeBackend.object("action", "finish-task", "task_id", id, "streaming", "duplex"),
            "payload", NativeBackend.object("input", new JSONObject())).toString())) throw new Failure("阿里云任务结束请求失败。", true);
    }
    void check() throws IOException {
        if (closed.get()) throw new IOException("Cancelled");
        if (failure != null) throw failure;
        long now = System.nanoTime();
        if (now - lastEvent > TimeUnit.SECONDS.toNanos(90) || (finishStarted != 0 && now - finishStarted > TimeUnit.SECONDS.toNanos(45)))
            throw new Failure("阿里云转写超时，正在从断点重连。", true);
    }
    @Override public void close() { if (closed.compareAndSet(false, true)) {removeCancel.run();started.countDown();socket.cancel();} }

    static boolean endsSentence(String text) { return text.trim().matches("(?s).*[.!?。！？][\"”’\\)\\]]*$"); }
    static JSONObject append(JSONObject previous, JSONObject cue) throws Exception {
        if (previous == null) return new JSONObject(cue.toString());
        String first = previous.getString("text").trim(), second = cue.getString("text").trim();
        String space = first.matches("(?s).*[A-Za-z0-9]$") && second.matches("(?s)^[A-Za-z0-9].*") && !second.startsWith("n't") ? " " : "";
        return NativeBackend.object("start", previous.getDouble("start"), "end", cue.getDouble("end"), "text", first + space + second);
    }
    static JSONArray sentenceCues(JSONObject sentence, double offset) throws Exception {
        return sentenceCues(sentence,offset,Double.NEGATIVE_INFINITY,Double.POSITIVE_INFINITY);
    }
    private static String normalize(String text) {return text.toLowerCase(java.util.Locale.ROOT).replace('’','\'').replace('‘','\'');}
    static List<String> alignedTokens(JSONObject sentence) throws Exception {
        String canonical=sentence.optString("text");if(canonical.isEmpty())return null;
        String search=normalize(canonical);int cursor=0;List<String> tokens=new ArrayList<>();JSONArray words=sentence.getJSONArray("words");
        for(int i=0;i<words.length();i++){
            JSONObject word=words.getJSONObject(i);String token=word.optString("text").trim(),punctuation=word.optString("punctuation");
            if(token.isEmpty()){tokens.add("");continue;}
            if(!punctuation.isEmpty()&&!token.endsWith(punctuation))token+=punctuation;
            int position=search.indexOf(normalize(token),cursor);
            if(position<0||canonical.substring(cursor,position).matches("(?s).*[\\p{L}\\p{N}_].*"))return null;
            int finish=position+token.length();tokens.add(canonical.substring(cursor,finish));cursor=finish;
        }
        if(canonical.substring(cursor).matches("(?s).*[\\p{L}\\p{N}_].*"))return null;
        if(!tokens.isEmpty())tokens.set(tokens.size()-1,tokens.get(tokens.size()-1)+canonical.substring(cursor));
        return tokens;
    }
    static JSONArray sentenceCues(JSONObject sentence, double offset, double from, double to) throws Exception {
        JSONArray words = sentence.optJSONArray("words"), result = new JSONArray();
        if (words == null || words.length() == 0) return fallback(sentence, offset);
        List<String> tokens=alignedTokens(sentence);
        if(!sentence.optString("text").isEmpty()&&tokens==null)return fallback(sentence,offset);
        JSONObject pending = null;
        for (int i=0; i<words.length(); i++) {
            JSONObject word = words.getJSONObject(i);
            double begin = word.optDouble("begin_time", Double.NaN), end = word.optDouble("end_time", Double.NaN);
            if (!Double.isFinite(begin) || !Double.isFinite(end) || begin < 0 || end <= begin) return fallback(sentence, offset);
            double middle=offset+(begin+end)/2000;if(middle<from||middle>to)continue;
            String token = word.optString("text").trim(), punctuation = word.optString("punctuation");
            if (token.isEmpty()) continue;
            if(tokens!=null){
                token=tokens.get(i);
                if(pending==null)pending=NativeBackend.object("start",offset+begin/1000,"end",offset+end/1000,"text",token);
                else pending.put("end",offset+end/1000).put("text",pending.getString("text")+token);
            }else{
                if (!punctuation.isEmpty() && !token.endsWith(punctuation)) token += punctuation;
                pending = append(pending, NativeBackend.object("start", offset + begin / 1000, "end", offset + end / 1000, "text", token));
            }
            boolean nextContinues=tokens!=null&&i+1<tokens.size()&&tokens.get(i+1).matches("(?s)^[A-Za-z0-9].*");
            if (endsSentence(pending.getString("text"))&&!nextContinues) { pending.put("text",pending.getString("text").trim());result.put(pending); pending = null; }
        }
        if (pending != null) {pending.put("text",pending.getString("text").trim());result.put(pending);}
        return result;
    }
    private static JSONArray fallback(JSONObject sentence, double offset) throws Exception {
        double begin = sentence.optDouble("begin_time", Double.NaN), end = sentence.optDouble("end_time", Double.NaN);
        if (!Double.isFinite(begin) || !Double.isFinite(end) || begin < 0 || end <= begin) throw new IOException("Invalid timestamps");
        return new JSONArray().put(NativeBackend.object("start", offset + begin / 1000, "end", offset + end / 1000, "text", sentence.getString("text").trim()));
    }
}
