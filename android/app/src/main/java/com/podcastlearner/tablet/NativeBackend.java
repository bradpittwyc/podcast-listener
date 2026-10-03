package com.podcastlearner.tablet;

import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.AudioFormat;
import android.util.AtomicFile;
import android.util.Xml;
import org.xmlpull.v1.XmlPullParser;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Device-local HTTP API. Audio decoding, storage and all cloud requests run on Android. */
public final class NativeBackend implements AutoCloseable {
    interface CredentialsWriter { void save(JSONObject value) throws Exception; }
    private final Context context;
    private volatile JSONObject credentials;
    private final CredentialsWriter writer;
    private final ExecutorService clients = Executors.newCachedThreadPool();
    private final ScheduledExecutorService timer = Executors.newScheduledThreadPool(1);
    private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
    private final ConcurrentHashMap<String,Semaphore> locks = new ConcurrentHashMap<>();
    private ServerSocket listener;
    private volatile boolean closed;

    NativeBackend(Context context, JSONObject credentials, CredentialsWriter writer) {
        this.context = context; this.credentials = credentials; this.writer = writer;
    }

    void start() throws IOException {
        listener = new ServerSocket(); listener.setReuseAddress(true);
        listener.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"),8557));
        clients.execute(() -> {
            while (!closed) {
                try { Socket socket=listener.accept(); sockets.add(socket); clients.execute(() -> serve(socket)); }
                catch (IOException e) { if (closed) return; }
            }
        });
    }
    @Override public void close() {
        closed=true;
        try { listener.close(); } catch (Exception ignored) {}
        for (Socket socket:sockets) try { socket.close(); } catch (Exception ignored) {}
        clients.shutdownNow(); timer.shutdownNow();
    }
    static JSONObject object(Object... pairs) throws Exception {
        JSONObject result=new JSONObject(); for(int i=0;i<pairs.length;i+=2) result.put((String)pairs[i],pairs[i+1]); return result;
    }
    static String encode(String value) throws Exception { return URLEncoder.encode(value,"UTF-8"); }
    static byte[] bytes(String text) { return text.getBytes(StandardCharsets.UTF_8); }
    static byte[] read(InputStream input, int maximum) throws IOException {
        try(InputStream source=input; ByteArrayOutputStream result=new ByteArrayOutputStream()) {
            byte[] buffer=new byte[16384]; int count;
            while((count=source.read(buffer))!=-1) { if(result.size()+count>maximum) throw new IOException("Response too large"); result.write(buffer,0,count); }
            return result.toByteArray();
        }
    }
    static String line(InputStream input) throws IOException {
        ByteArrayOutputStream buffer=new ByteArrayOutputStream(); int value;
        while((value=input.read())!=-1 && value!='\n') { if(buffer.size()>16384) throw new IOException("Header too large"); if(value!='\r') buffer.write(value); }
        return buffer.toString("UTF-8");
    }
    private static class RequestData {
        String method,path; Map<String,String> query=new HashMap<>(), headers=new HashMap<>(); JSONObject body=new JSONObject();
        String q(String name) { return query.getOrDefault(name,""); }
    }
    private void serve(Socket socket) {
        try(Socket connection=socket) {
            connection.setSoTimeout(15000);
            InputStream input=new BufferedInputStream(connection.getInputStream()); OutputStream output=connection.getOutputStream();
            String[] first=line(input).split(" "); if(first.length<2) return;
            RequestData request=new RequestData(); request.method=first[0]; URI uri=new URI(first[1]); request.path=uri.getPath();
            if(uri.getRawQuery()!=null) for(String pair:uri.getRawQuery().split("&")) { String[] parts=pair.split("=",2); request.query.put(URLDecoder.decode(parts[0],"UTF-8"),parts.length>1?URLDecoder.decode(parts[1],"UTF-8"):""); }
            int headerSize=0; String header;
            while(!(header=line(input)).isEmpty()) { headerSize+=header.length(); if(headerSize>65536) return; int colon=header.indexOf(':'); if(colon>0)request.headers.put(header.substring(0,colon).toLowerCase(Locale.ROOT),header.substring(colon+1).trim()); }
            String host=request.headers.getOrDefault("host","");
            if(!host.equals("127.0.0.1:8557") && !host.equals("localhost:8557")) { response(output,403,"application/json",bytes("{}")); return; }
            int length=Integer.parseInt(request.headers.getOrDefault("content-length","0"));
            if(length<0 || length>4*1024*1024) { response(output,413,"application/json",bytes("{}")); return; }
            if(length>0) { byte[] body=new byte[length]; int offset=0,count; while(offset<length && (count=input.read(body,offset,length-offset))>0)offset+=count; if(offset!=length)return; request.body=new JSONObject(new String(body,StandardCharsets.UTF_8)); }
            connection.setSoTimeout(0);
            try { route(request,connection,output); }
            catch(Exception e) { response(output,500,"application/json",bytes(object("status","error","message",safeError(e)).toString())); }
        } catch(Exception ignored) {} finally { sockets.remove(socket); }
    }
    static void response(OutputStream output,int status,String type,byte[] body) throws IOException {
        output.write(bytes("HTTP/1.1 "+status+" OK\r\nContent-Type: "+type+"\r\nContent-Length: "+body.length+"\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")); output.write(body); output.flush();
    }
    private void json(OutputStream output,JSONObject value) throws IOException { response(output,200,"application/json; charset=utf-8",bytes(value.toString())); }

    private void route(RequestData r,Socket socket,OutputStream output) throws Exception {
        if(r.path.equals("/") && r.method.equals("GET")) {
            String html=new String(read(context.getAssets().open("index.html"),2*1024*1024),StandardCharsets.UTF_8)
                .replace("https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css","/assets/fontawesome.css")
                .replace("密钥保存在本地服务的 .env 文件中。","密钥加密保存在当前设备中。")
                .replace("新配置用于后续转写任务。","当前设备可独立下载和转写，无需连接电脑。");
            response(output,200,"text/html; charset=utf-8",bytes(html)); return;
        }
        if(r.path.startsWith("/assets/") && !r.path.contains("..")) {
            String name=r.path.substring(8); response(output,200,name.endsWith(".css")?"text/css":"font/woff2",read(context.getAssets().open(name),1024*1024)); return;
        }
        if(r.path.equals("/api/runtime")) { json(output,object("backend","android","independent",true,"device",android.os.Build.MODEL)); return; }
        if(r.path.equals("/api/settings")) {
            if(r.method.equals("POST")) {
                String origin=r.headers.get("origin");
                if(origin!=null && !origin.equals("http://127.0.0.1:8557")) { response(output,403,"application/json",bytes("{}")); return; }
                if(!r.headers.getOrDefault("content-type","").contains("application/json")) { response(output,415,"application/json",bytes("{}")); return; }
                synchronized(this) {
                    JSONObject next=new JSONObject(credentials.toString());
                    for(String name:new String[]{"groq_key_1","groq_key_2","gemini_key"}) {
                        Object value=r.body.opt(name); if(value==null)continue;
                        if(!(value instanceof String) || ((String)value).length()>512 || ((String)value).indexOf('\n')>=0 || ((String)value).indexOf('\r')>=0 || ((String)value).indexOf(0)>=0) throw new IOException("Invalid credentials");
                        if(!((String)value).trim().isEmpty())next.put(name,((String)value).trim());
                    }
                    writer.save(next); credentials=next;
                }
            }
            json(output,object("configured",object("groq_key_1",!credentials.optString("groq_key_1").isEmpty(),"groq_key_2",!credentials.optString("groq_key_2").isEmpty(),"gemini_key",!credentials.optString("gemini_key").isEmpty()))); return;
        }
        String country=r.q("country").isEmpty()?"us":r.q("country");
        if(r.path.equals("/api/top-charts")) {
            JSONArray entries=getJson("https://itunes.apple.com/"+encode(country)+"/rss/toppodcasts/limit=30/json").optJSONObject("feed").optJSONArray("entry"); JSONArray results=new JSONArray();
            if(entries!=null)for(int i=0;i<entries.length();i++) { JSONObject item=entries.getJSONObject(i);JSONArray images=item.getJSONArray("im:image");results.put(object("collectionId",item.getJSONObject("id").getJSONObject("attributes").getString("im:id"),"collectionName",item.getJSONObject("im:name").getString("label"),"artistName",item.getJSONObject("im:artist").optString("label"),"artworkUrl600",images.getJSONObject(images.length()-1).getString("label"))); }
            json(output,object("results",results)); return;
        }
        if(r.path.equals("/api/search")) { json(output,getJson("https://itunes.apple.com/search?media=podcast&limit=20&country="+encode(country)+"&term="+encode(r.q("query")))); return; }
        if(r.path.equals("/api/lookup")) {
            JSONArray items=getJson("https://itunes.apple.com/lookup?id="+encode(r.q("id"))+"&country="+encode(country)).getJSONArray("results");
            if(items.length()==0)throw new IOException("Podcast not found"); JSONObject item=items.getJSONObject(0), feed=feed(item.getString("feedUrl"));
            json(output,object("meta",item,"feedData",feed,"feed",feed));return;
        }
        if(r.path.equals("/api/parse-feed") || r.path.equals("/api/feed")) { json(output,feed(r.q("url")));return; }
        if(r.path.equals("/api/audio")) { audio(r,output);return; }
        if(r.path.equals("/api/transcribe_stream")) { transcribe(r,socket,output);return; }
        if(r.path.equals("/api/ask")) {
            if(!r.body.optBoolean("transcript_complete") || r.body.optString("full_transcript").trim().isEmpty()) { response(output,409,"application/json",bytes(object("detail","请等待全篇字幕加载完成").toString()));return; }
            String prompt="你是英语播客学习助教。以下是本期完整字幕，请结合全文推理、解释背景，以中文回答。\n【完整字幕】\n"+r.body.getString("full_transcript")+"\n【选中字幕】\n"+r.body.optString("selected_text")+"\n【问题】\n"+r.body.optString("question");
            json(output,object("status","success","answer",gemini(prompt,false)));return;
        }
        if(r.path.equals("/api/define")) {
            String prompt="Analyze English word '"+r.q("word")+"' in context '"+r.q("context")+"'. Return a JSON object with word, phonetic, pos, definition_en, translation_cn, example, example_cn, context_note. Use Chinese for translations and context_note.";
            json(output,object("status","success","data",new JSONObject(gemini(prompt,true))));return;
        }
        response(output,404,"application/json",bytes("{}"));
    }
    static HttpURLConnection open(String url,String range) throws Exception {
        for(int attempts=0;attempts<8;attempts++) {
            URL address=new URL(url); if(!address.getProtocol().equals("https")&&!address.getProtocol().equals("http"))throw new IOException("Invalid URL");
            HttpURLConnection connection=(HttpURLConnection)address.openConnection(); connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(20000);connection.setReadTimeout(30000);connection.setRequestProperty("User-Agent","Mozilla/5.0");
            if(range!=null)connection.setRequestProperty("Range",range);
            int code=connection.getResponseCode();
            if(code>=300&&code<400&&connection.getHeaderField("Location")!=null) {url=new URL(address,connection.getHeaderField("Location")).toString();connection.disconnect();continue;}
            return connection;
        } throw new IOException("Too many redirects");
    }
    static JSONObject getJson(String url) throws Exception {
        HttpURLConnection connection=open(url,null);try {int code=connection.getResponseCode();if(code!=200)throw new CloudError(code);return new JSONObject(new String(read(connection.getInputStream(),12*1024*1024),StandardCharsets.UTF_8));}finally {connection.disconnect();}
    }
    private void audio(RequestData r,OutputStream output) throws Exception {
        HttpURLConnection connection=open(r.q("url"),r.headers.get("range"));
        try {
            int code=connection.getResponseCode(); if(code!=200&&code!=206)throw new CloudError(code);
            String headers="HTTP/1.1 "+code+" OK\r\nContent-Type: "+Optional.ofNullable(connection.getContentType()).orElse("audio/mpeg")+"\r\nConnection: close\r\nAccept-Ranges: bytes\r\n";
            for(String name:new String[]{"Content-Length","Content-Range"}) {String value=connection.getHeaderField(name);if(value!=null)headers+=name+": "+value+"\r\n";}
            output.write(bytes(headers+"\r\n")); output.flush();
            try(InputStream input=connection.getInputStream()) {byte[] buffer=new byte[16384];int count;while(!closed&&(count=input.read(buffer))!=-1){output.write(buffer,0,count);}}
        } finally {connection.disconnect();}
    }
    private JSONObject feed(String url) throws Exception {
        HttpURLConnection connection=open(url,null);
        try(InputStream stream=connection.getInputStream()) {
            XmlPullParser parser=Xml.newPullParser(); parser.setFeature(XmlPullParser.FEATURE_PROCESS_NAMESPACES,true);parser.setInput(stream,null);
            JSONObject result=object("title","","author","","image","","episodes",new JSONArray()); JSONObject episode=null; JSONArray episodes=result.getJSONArray("episodes");
            String field="";StringBuilder text=new StringBuilder();boolean inImage=false;
            int event;
            while((event=parser.next())!=XmlPullParser.END_DOCUMENT) {
                if(event==XmlPullParser.START_TAG) {
                    String name=parser.getName();field=name;text.setLength(0);
                    if(name.equals("item"))episode=object("title","","audioUrl","","pubDate","","duration","","description","","transcriptUrl","");
                    if(name.equals("image")){inImage=true;String href=parser.getAttributeValue(null,"href");if(episode==null&&href!=null)result.put("image",href);}
                    if(episode!=null&&name.equals("enclosure")){String source=parser.getAttributeValue(null,"url");if(source!=null)episode.put("audioUrl",source);}
                    if(episode!=null&&name.equals("transcript")){String source=parser.getAttributeValue(null,"url");String type=parser.getAttributeValue(null,"type");if(source!=null&&(type==null||type.contains("vtt")))episode.put("transcriptUrl",source);}
                } else if(event==XmlPullParser.TEXT || event==XmlPullParser.CDSECT) text.append(parser.getText());
                else if(event==XmlPullParser.END_TAG) {
                    String name=parser.getName(),value=text.toString().trim();
                    if(name.equals("item")){if(episode!=null&&!episode.optString("audioUrl").isEmpty())episodes.put(episode);episode=null;}
                    else if(episode!=null) {
                        if(name.equals("title")||name.equals("pubDate")||name.equals("duration"))episode.put(name,value);
                        if(name.equals("description")||name.equals("summary"))episode.put("description",value.replaceAll("<[^>]+>",""));
                    } else {
                        if(name.equals("title"))result.put("title",value);
                        if(name.equals("author"))result.put("author",value);
                        if(name.equals("url")&&inImage)result.put("image",value);
                    }
                    if(name.equals("image"))inImage=false;
                }
            } return result;
        } finally {connection.disconnect();}
    }
    private String gemini(String prompt,boolean json) throws Exception {
        String key=credentials.optString("gemini_key"); if(key.isEmpty())throw new IOException("请在设置中填写 Gemini Key");
        HttpURLConnection connection=cloudConnection("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent");
        connection.setConnectTimeout(20000);connection.setReadTimeout(120000);connection.setRequestMethod("POST");connection.setDoOutput(true);
        connection.setRequestProperty("x-goog-api-key",key);connection.setRequestProperty("Content-Type","application/json");
        connection.setRequestProperty("User-Agent","python-requests/2.32.5");
        JSONObject payload=object("contents",new JSONArray().put(object("parts",new JSONArray().put(object("text",prompt)))));
        if(json)payload.put("generationConfig",object("responseMimeType","application/json"));
        try {try(OutputStream output=connection.getOutputStream()){output.write(bytes(payload.toString()));}
            if(connection.getResponseCode()!=200)throw cloudError(connection,"Gemini");
            JSONObject result=new JSONObject(new String(read(connection.getInputStream(),4*1024*1024),StandardCharsets.UTF_8));
            JSONArray parts=result.getJSONArray("candidates").getJSONObject(0).getJSONObject("content").getJSONArray("parts");StringBuilder answer=new StringBuilder();
            for(int i=0;i<parts.length();i++)if(!parts.getJSONObject(i).optBoolean("thought"))answer.append(parts.getJSONObject(i).optString("text"));
            return answer.toString();
        } finally {connection.disconnect();}
    }
    static class CloudError extends IOException { final int status;CloudError(int status){super("云服务返回 HTTP "+status);this.status=status;} }
    private HttpURLConnection cloudConnection(String url) throws Exception {
        // Use the local Clash HTTP tunnel when available; TLS remains end-to-end verified.
        // This avoids relying on VPN per-app routing and device-side DNS for cloud APIs.
        boolean localProxy=false;
        try(Socket probe=new Socket()){probe.connect(new InetSocketAddress("127.0.0.1",7890),200);localProxy=true;}catch(IOException ignored){}
        return (HttpURLConnection)new URL(url).openConnection(localProxy
            ? new Proxy(Proxy.Type.HTTP,new InetSocketAddress("127.0.0.1",7890)) : Proxy.NO_PROXY);
    }
    private CloudError cloudError(HttpURLConnection connection,String service) {
        int status=500;String detail="";
        try {status=connection.getResponseCode();String body=new String(read(connection.getErrorStream(),16384),StandardCharsets.UTF_8);JSONObject error=new JSONObject(body).optJSONObject("error");if(error!=null)detail=error.optString("message");}catch(Exception ignored){}
        for(String name:new String[]{"groq_key_1","groq_key_2","gemini_key"}){String key=credentials.optString(name);if(!key.isEmpty())detail=detail.replace(key,"[redacted]");}
        final String message=service+" HTTP "+status+(detail.isEmpty()?"":"："+detail);
        return new CloudError(status){@Override public String getMessage(){return message;}};
    }
    static String safeError(Exception error) {
        Throwable cause=error;while(cause.getCause()!=null)cause=cause.getCause();
        if(cause instanceof CloudError)return cause.getMessage();
        return "请求失败，请检查设备网络、API Key 或音频格式后重试。";
    }
    private class Events {
        final OutputStream output;final AtomicBoolean stopped=new AtomicBoolean();
        Events(OutputStream output){this.output=output;}
        synchronized void send(JSONObject item) throws IOException { if(stopped.get())throw new IOException("Cancelled");try{output.write(bytes("data: "+item+"\n\n"));output.flush();}catch(IOException e){stopped.set(true);throw e;} }
    }
    private AtomicFile checkpoint(String url) throws Exception {
        byte[] digest=MessageDigest.getInstance("SHA-256").digest(bytes(url));StringBuilder name=new StringBuilder();for(byte value:digest)name.append(String.format("%02x",value&255));
        File folder=new File(context.getFilesDir(),"transcripts");folder.mkdirs();return new AtomicFile(new File(folder,name+".json"));
    }
    private JSONObject load(AtomicFile file) {try{return new JSONObject(new String(read(file.openRead(),12*1024*1024),StandardCharsets.UTF_8));}catch(Exception e){return new JSONObject();}}
    private void store(AtomicFile file,JSONObject value) throws Exception {
        FileOutputStream stream=file.startWrite();try{stream.write(bytes(value.toString()));file.finishWrite(stream);}catch(Exception e){file.failWrite(stream);throw e;}
    }
    private void transcribe(RequestData request,Socket socket,OutputStream output) throws Exception {
        output.write(bytes("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n"));output.flush();
        Events events=new Events(output);String url=request.q("audio_url");Semaphore lock=locks.computeIfAbsent(url,key->new Semaphore(1));
        ScheduledFuture<?> heartbeat=timer.scheduleAtFixedRate(()->{try{events.send(object("status","heartbeat"));}catch(Exception e){events.stopped.set(true);}},10,10,TimeUnit.SECONDS);
        boolean acquired=false;
        try {
            while(!(acquired=lock.tryAcquire(1,TimeUnit.SECONDS)))if(events.stopped.get()||closed)return;
            AtomicFile file=checkpoint(url);if(request.q("force_refresh").equals("true"))file.delete();JSONObject state=load(file);
            JSONArray cues=state.optJSONArray("cues");if(cues==null)cues=new JSONArray();
            if(state.optBoolean("complete")){events.send(object("status","cached","vtt",vtt(cues)));return;}
            String transcript=request.q("transcript_url");
            if(!transcript.isEmpty()&&state.optDouble("until",0)==0) {
                try {HttpURLConnection cc=open(transcript,null);String content;try{content=new String(read(cc.getInputStream(),12*1024*1024),StandardCharsets.UTF_8);}finally{cc.disconnect();}
                    if(content.startsWith("WEBVTT")&&content.contains("-->")){events.send(object("status","official","vtt",content));return;}
                } catch(Exception ignored){}
            }
            List<String> keys=new ArrayList<>();for(String name:new String[]{"groq_key_1","groq_key_2"}){String key=credentials.optString(name).trim();if(!key.isEmpty()&&!keys.contains(key))keys.add(key);}
            if(keys.isEmpty()){events.send(object("status","error","detail","请在设置中填写 Groq Key","retryable",false));return;}
            double until=state.optDouble("until",0);int index=state.optInt("next_chunk",0);
            if(until>0)events.send(object("status","resumed","until",until,"cues",cues));
            events.send(object("status","audio_source","audio_url","/api/audio?url="+encode(url)));
            events.send(object("status","progress","detail","设备正在流式下载并解码，首段字幕就绪后播放。"));
            decode(url,until,index,cues,keys,file,events);
        } catch(Exception error) {
            if(!events.stopped.get())try {Throwable cause=error;while(cause.getCause()!=null)cause=cause.getCause();boolean retryable=!(cause instanceof CloudError)||((CloudError)cause).status==429||((CloudError)cause).status>=500;
                events.send(object("status","error","detail",safeError(error),"retryable",retryable));}catch(Exception ignored){}
        } finally {events.stopped.set(true);heartbeat.cancel(true);if(acquired)lock.release();}
    }
    private static String time(double seconds) {long millis=Math.round(seconds*1000);return String.format(Locale.US,"%02d:%02d:%02d.%03d",millis/3600000,(millis/60000)%60,(millis/1000)%60,millis%1000);}
    private static String vtt(JSONArray cues) throws Exception {StringBuilder result=new StringBuilder("WEBVTT\n\n");for(int i=0;i<cues.length();i++){JSONObject cue=cues.getJSONObject(i);result.append(time(cue.getDouble("start"))).append(" --> ").append(time(cue.getDouble("end"))).append('\n').append(cue.getString("text")).append("\n\n");}return result.toString();}

    private static class Slice {byte[] wav;double offset,duration;int index;Future<JSONArray> result;}
    private JSONArray groq(byte[] wav,String key,AtomicBoolean stopped) throws Exception {
        for(int attempt=0;attempt<3&&!stopped.get();attempt++) {
            HttpURLConnection connection=cloudConnection("https://api.groq.com/openai/v1/audio/transcriptions");
            connection.setConnectTimeout(20000);connection.setReadTimeout(90000);connection.setRequestMethod("POST");connection.setDoOutput(true);
            String boundary="Podcast"+UUID.randomUUID().toString().replace("-","");connection.setRequestProperty("Authorization","Bearer "+key);connection.setRequestProperty("Content-Type","multipart/form-data; boundary="+boundary);
            connection.setRequestProperty("User-Agent","python-requests/2.32.5");
            connection.setRequestProperty("Accept","application/json");
            ByteArrayOutputStream payload=new ByteArrayOutputStream();
            for(String[] entry:new String[][]{{"model","whisper-large-v3-turbo"},{"response_format","verbose_json"},{"timestamp_granularities[]","segment"}})payload.write(bytes("--"+boundary+"\r\nContent-Disposition: form-data; name=\""+entry[0]+"\"\r\n\r\n"+entry[1]+"\r\n"));
            payload.write(bytes("--"+boundary+"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"slice.wav\"\r\nContent-Type: audio/wav\r\n\r\n"));payload.write(wav);payload.write(bytes("\r\n--"+boundary+"--\r\n"));
            try {
                connection.setFixedLengthStreamingMode(payload.size());try(OutputStream stream=connection.getOutputStream()){payload.writeTo(stream);}
                int code=connection.getResponseCode();if(code!=200){if((code==429||code>=500)&&attempt<2){Thread.sleep(1000L*(attempt+1));continue;}throw cloudError(connection,"Groq");}
                JSONObject response=new JSONObject(new String(read(connection.getInputStream(),4*1024*1024),StandardCharsets.UTF_8));JSONArray segments=response.optJSONArray("segments");
                if(segments==null)throw new IOException("No timestamp segments");return segments;
            } finally {connection.disconnect();}
        } throw new IOException("Cancelled");
    }
    private byte[] wav(byte[] pcm,int rate) {
        ByteBuffer data=ByteBuffer.allocate(44+pcm.length).order(ByteOrder.LITTLE_ENDIAN);data.put(bytes("RIFF")).putInt(36+pcm.length).put(bytes("WAVEfmt ")).putInt(16).putShort((short)1).putShort((short)1).putInt(rate).putInt(rate*2).putShort((short)2).putShort((short)16).put(bytes("data")).putInt(pcm.length).put(pcm);return data.array();
    }
    private int length(double offset) {return offset<300?30:offset<900?120:300;}
    private void completeSlice(Slice slice,JSONArray cues,AtomicFile file,Events events) throws Exception {
        JSONArray segments=slice.result.get();
        int first=cues.length();for(int i=0;i<segments.length();i++){JSONObject segment=segments.getJSONObject(i);double start=Math.max(0,segment.optDouble("start",0)),end=Math.min(slice.duration,segment.optDouble("end",0));String text=segment.optString("text").trim();if(end>start&&!text.isEmpty())cues.put(object("start",slice.offset+start,"end",slice.offset+end,"text",text));}
        store(file,object("until",slice.offset+slice.duration,"next_chunk",slice.index+1,"cues",cues,"complete",false));
        for(int i=first;i<cues.length();i++)events.send(object("status","cue","cue",cues.getJSONObject(i)));
        events.send(object("status","chunk_ready","until",slice.offset+slice.duration));
    }
    private void decode(String url,double resume,int firstIndex,JSONArray cues,List<String> keys,AtomicFile file,Events events) throws Exception {
        MediaExtractor extractor=new MediaExtractor();MediaCodec codec=null;
        List<ExecutorService> workers=new ArrayList<>();for(String ignored:keys)workers.add(Executors.newSingleThreadExecutor());
        ArrayDeque<Slice> pending=new ArrayDeque<>();
        try {
            extractor.setDataSource(url,Collections.singletonMap("User-Agent","PodcastLearner/2.0"));
            MediaFormat format=null;for(int i=0;i<extractor.getTrackCount();i++){MediaFormat candidate=extractor.getTrackFormat(i);if(candidate.getString(MediaFormat.KEY_MIME).startsWith("audio/")){format=candidate;extractor.selectTrack(i);break;}}
            if(format==null)throw new IOException("No supported audio track");
            codec=MediaCodec.createDecoderByType(format.getString(MediaFormat.KEY_MIME));codec.configure(format,null,null,0);codec.start();
            int sourceRate=format.getInteger(MediaFormat.KEY_SAMPLE_RATE),channels=format.getInteger(MediaFormat.KEY_CHANNEL_COUNT),encoding=AudioFormat.ENCODING_PCM_16BIT;
            int targetRate=16000,index=firstIndex;long sourceFrame=0,nextTargetFrame=0,skip=Math.round(resume*targetRate);double offset=resume;
            ByteArrayOutputStream pcm=new ByteArrayOutputStream();boolean inputEnded=false,outputEnded=false;MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
            while(!outputEnded&&!events.stopped.get()&&!closed) {
                while(!pending.isEmpty()&&pending.peek().result.isDone())completeSlice(pending.remove(),cues,file,events);
                if(!inputEnded){int inputIndex=codec.dequeueInputBuffer(10000);if(inputIndex>=0){ByteBuffer input=codec.getInputBuffer(inputIndex);int size=extractor.readSampleData(input,0);if(size<0){codec.queueInputBuffer(inputIndex,0,0,0,MediaCodec.BUFFER_FLAG_END_OF_STREAM);inputEnded=true;}else{codec.queueInputBuffer(inputIndex,0,size,extractor.getSampleTime(),0);extractor.advance();}}}
                int outputIndex=codec.dequeueOutputBuffer(info,10000);
                if(outputIndex==MediaCodec.INFO_OUTPUT_FORMAT_CHANGED){MediaFormat decoded=codec.getOutputFormat();sourceRate=decoded.getInteger(MediaFormat.KEY_SAMPLE_RATE);channels=decoded.getInteger(MediaFormat.KEY_CHANNEL_COUNT);encoding=decoded.containsKey(MediaFormat.KEY_PCM_ENCODING)?decoded.getInteger(MediaFormat.KEY_PCM_ENCODING):AudioFormat.ENCODING_PCM_16BIT;}
                if(outputIndex>=0){ByteBuffer decoded=codec.getOutputBuffer(outputIndex).order(ByteOrder.LITTLE_ENDIAN);decoded.position(info.offset);decoded.limit(info.offset+info.size);int sampleBytes=encoding==AudioFormat.ENCODING_PCM_FLOAT?4:2;
                    while(decoded.remaining()>=channels*sampleBytes){double mixed=0;for(int channel=0;channel<channels;channel++)mixed+=encoding==AudioFormat.ENCODING_PCM_FLOAT?decoded.getFloat()*32767:decoded.getShort();short sample=(short)Math.max(-32768,Math.min(32767,mixed/channels));
                        if(sourceFrame*targetRate>=nextTargetFrame*sourceRate){if(nextTargetFrame>=skip){pcm.write(sample&255);pcm.write((sample>>8)&255);}nextTargetFrame++;}sourceFrame++;
                        if(pcm.size()>=length(offset)*targetRate*2){while(pending.size()>=keys.size()&&!events.stopped.get())completeSlice(pending.remove(),cues,file,events);
                            Slice slice=new Slice();slice.offset=offset;slice.duration=pcm.size()/(double)(targetRate*2);slice.index=index++;slice.wav=wav(pcm.toByteArray(),targetRate);pcm.reset();offset+=slice.duration;
                            final String key=keys.get(slice.index%keys.size());slice.result=workers.get(slice.index%keys.size()).submit(()->groq(slice.wav,key,events.stopped));pending.add(slice);
                        }
                    }
                    outputEnded=(info.flags&MediaCodec.BUFFER_FLAG_END_OF_STREAM)!=0;codec.releaseOutputBuffer(outputIndex,false);
                }
            }
            if(events.stopped.get()||closed)return;
            if(pcm.size()>0){while(pending.size()>=keys.size())completeSlice(pending.remove(),cues,file,events);Slice slice=new Slice();slice.offset=offset;slice.duration=pcm.size()/(double)(targetRate*2);slice.index=index++;slice.wav=wav(pcm.toByteArray(),targetRate);offset+=slice.duration;final String key=keys.get(slice.index%keys.size());slice.result=workers.get(slice.index%keys.size()).submit(()->groq(slice.wav,key,events.stopped));pending.add(slice);}
            while(!pending.isEmpty())completeSlice(pending.remove(),cues,file,events);
            if(cues.length()==0)throw new IOException("No speech recognized");
            store(file,object("until",offset,"next_chunk",index,"cues",cues,"complete",true));events.send(object("status","done"));
        } finally {for(Slice slice:pending)if(slice.result!=null)slice.result.cancel(true);for(ExecutorService worker:workers)worker.shutdownNow();if(codec!=null){try{codec.stop();}catch(Exception ignored){}codec.release();}extractor.release();}
    }
}
