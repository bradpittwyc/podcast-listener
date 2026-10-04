package com.podcastlearner.tablet;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.ProxyInfo;
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
    private final Set<Socket> sockets = Collections.newSetFromMap(new ConcurrentHashMap<Socket,Boolean>());
    private final ConcurrentHashMap<String,Semaphore> locks = new ConcurrentHashMap<>();
    private final TranscriptionJobs transcriptionJobs=new TranscriptionJobs();
    private ServerSocket listener;
    private volatile boolean closed;
    private final java.util.concurrent.atomic.AtomicInteger sentenceKey = new java.util.concurrent.atomic.AtomicInteger();
    private final java.util.concurrent.atomic.AtomicInteger dictionaryKey = new java.util.concurrent.atomic.AtomicInteger();
    private Network asrNetwork;
    private okhttp3.OkHttpClient asrClient;
    private final Map<String,String> resolvedAudio=Collections.synchronizedMap(new LinkedHashMap<String,String>(128,.75f,true){
        @Override protected boolean removeEldestEntry(Map.Entry<String,String> entry){return size()>128;}
    });

    NativeBackend(Context context, JSONObject credentials, CredentialsWriter writer) {
        this.context = context; this.credentials = credentials; this.writer = writer;
    }

    void start() throws IOException {
        CachePolicy.prune(new File(context.getFilesDir(),"transcripts"),Collections.emptySet(),64L*1024*1024,30L*86400*1000);
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
        transcriptionJobs.close();
        try { listener.close(); } catch (Exception ignored) {}
        for (Socket socket:sockets) try { socket.close(); } catch (Exception ignored) {}
        clients.shutdownNow(); timer.shutdownNow();
        if(asrClient!=null)asrClient.connectionPool().evictAll();
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
        String q(String name) { return query.containsKey(name)?query.get(name):""; }
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
            String host=(request.headers.containsKey("host")?request.headers.get("host"):"");
            if(!host.equals("127.0.0.1:8557") && !host.equals("localhost:8557")) { response(output,403,"application/json",bytes("{}")); return; }
            int length=Integer.parseInt((request.headers.containsKey("content-length")?request.headers.get("content-length"):"0"));
            if(length<0 || length>4*1024*1024) { response(output,413,"application/json",bytes("{}")); return; }
            if(length>0) { byte[] body=new byte[length]; int offset=0,count; while(offset<length && (count=input.read(body,offset,length-offset))>0)offset+=count; if(offset!=length)return; request.body=new JSONObject(new String(body,StandardCharsets.UTF_8)); }
            connection.setSoTimeout(0);
            try { route(request,connection,output); }
            catch(Exception e) {
                StringBuilder types=new StringBuilder(request.path);
                for(Throwable cause=e;cause!=null;cause=cause.getCause()){
                    types.append(" ").append(cause.getClass().getSimpleName());
                    if(cause instanceof android.system.ErrnoException)types.append(":").append(((android.system.ErrnoException)cause).errno);
                }
                android.util.Log.w("PodcastBackend",types.toString());
                response(output,500,"application/json",bytes(object("status","error","message",safeError(e)).toString()));
            }
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
                if(!(r.headers.containsKey("content-type")?r.headers.get("content-type"):"").contains("application/json")) { response(output,415,"application/json",bytes("{}")); return; }
                synchronized(this) {
                    JSONObject next=new JSONObject(credentials.toString());
                    for(String name:new String[]{"aliyun_key_1","aliyun_key_2","gemini_key"}) {
                        Object value=r.body.opt(name); if(value==null)continue;
                        if(!(value instanceof String) || ((String)value).length()>512 || ((String)value).indexOf('\n')>=0 || ((String)value).indexOf('\r')>=0 || ((String)value).indexOf(0)>=0) throw new IOException("Invalid credentials");
                        if(!((String)value).trim().isEmpty())next.put(name,((String)value).trim());
                    }
                    String region=r.body.optString("aliyun_region",next.optString("aliyun_region","beijing"));
                    if(!region.equals("beijing")&&!region.equals("singapore"))throw new IOException("Invalid region");
                    next.put("aliyun_region",region);
                    String dictionary=r.body.optString("dictionary_provider",next.optString("dictionary_provider","auto"));
                    if(!dictionary.equals("auto")&&!dictionary.equals("qwen")&&!dictionary.equals("gemini"))throw new IOException("Invalid dictionary provider");
                    next.put("dictionary_provider",dictionary);
                    String translation=r.body.optString("translation_provider",next.optString("translation_provider","gemini"));
                    if(!translation.equals("qwen")&&!translation.equals("gemini"))throw new IOException("Invalid translation provider");
                    next.put("translation_provider",translation);
                    String tutor=r.body.optString("tutor_provider",next.optString("tutor_provider","qwen"));
                    if(!tutor.equals("qwen")&&!tutor.equals("gemini"))throw new IOException("Invalid tutor provider");
                    next.put("tutor_provider",tutor);writer.save(next);credentials=next;
                }
            }
            json(output,object("configured",object("aliyun_key_1",!credentials.optString("aliyun_key_1").isEmpty(),"aliyun_key_2",!credentials.optString("aliyun_key_2").isEmpty(),"gemini_key",!credentials.optString("gemini_key").isEmpty()),"options",object("subtitle_provider","aliyun","aliyun_region",credentials.optString("aliyun_region","beijing"),"dictionary_provider",credentials.optString("dictionary_provider","auto"),"translation_provider",credentials.optString("translation_provider","gemini"),"tutor_provider",credentials.optString("tutor_provider","qwen")),"dictionary_route",dictionaryProvider())); return;
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
        if(r.path.equals("/api/cancel_transcription") && r.method.equals("POST")) {
            transcriptionJobs.cancel(TranscriptionJobs.token(r.body.optString("request_id")));
            json(output,object("status","cancelled"));return;
        }
        if(r.path.equals("/api/retranscribe_sentence") && r.method.equals("POST")) { regenerate(r,output);return; }
        if(r.path.equals("/api/ask")) {
            if(r.body.optString("full_transcript").trim().isEmpty()) { response(output,409,"application/json",bytes(object("detail","请等待首条字幕加载后提问").toString()));return; }
            String prompt;
            try { prompt=TutorPrompt.build(r.body); }
            catch(IllegalArgumentException e) { response(output,400,"application/json",bytes(object("detail",e.getMessage()).toString()));return; }
            String provider=credentials.optString("tutor_provider","qwen");
            boolean configured=provider.equals("qwen")?(!credentials.optString("aliyun_key_1").trim().isEmpty()||!credentials.optString("aliyun_key_2").trim().isEmpty()):!credentials.optString("gemini_key").trim().isEmpty();
            if(!configured){json(output,object("status","error","message","助教服务未配置，请检查设置。"));return;}
            try {String answer=provider.equals("qwen")?qwenJson(prompt,"助教",false):gemini(prompt,false);
                if(answer.trim().isEmpty())json(output,object("status","error","message","助教未返回回答，请重试。"));
                else json(output,object("status","success","provider",provider,"answer",answer));}
            catch(Exception error){json(output,object("status","error","message","助教请求失败，请检查网络和助教模型设置后重试。"));}
            return;
        }
        if(r.path.equals("/api/translate_subtitles") && r.method.equals("POST")) {
            JSONArray sentences;
            try {sentences=SubtitleTranslation.sentences(r.body);}
            catch(IllegalArgumentException error) {response(output,400,"application/json",bytes(object("detail",error.getMessage()).toString()));return;}
            String provider=credentials.optString("translation_provider","gemini");
            boolean configured=provider.equals("qwen")?(!credentials.optString("aliyun_key_1").trim().isEmpty()||!credentials.optString("aliyun_key_2").trim().isEmpty()):!credentials.optString("gemini_key").trim().isEmpty();
            if(!configured) {json(output,object("status","error","message","翻译服务未配置，请检查设置。"));return;}
            try {String prompt=SubtitleTranslation.prompt(sentences);String text=provider.equals("qwen")?qwenJson(prompt,"翻译"):gemini(prompt,true);
                json(output,object("status","success","provider",provider,"translations",SubtitleTranslation.translations(text,sentences)));}
            catch(Exception error) {json(output,object("status","error","message",error instanceof IllegalArgumentException?error.getMessage():"字幕翻译失败，请检查网络和翻译模型设置后点击双语字幕重试。"));}
            return;
        }
        if(r.path.equals("/api/define")) {
            String prompt="Analyze English word '"+r.q("word")+"' in context '"+r.q("context")+"'. Return a JSON object with word, phonetic, pos, definition_en, translation_cn, example, example_cn, context_note. Use Chinese for translations and context_note.";
            String provider=dictionaryProvider();
            json(output,object("status","success","provider",provider,"data",new JSONObject(provider.equals("qwen")?qwenDictionary(prompt):gemini(prompt,true))));return;
        }
        response(output,404,"application/json",bytes("{}"));
    }
    private Network podcastNetwork() {
        ConnectivityManager manager=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);
        Network candidate=null;
        if(manager==null)return null;
        for(Network network:manager.getAllNetworks()) {
            NetworkCapabilities caps=manager.getNetworkCapabilities(network);
            if(caps==null || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN))continue;
            if(caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED))return network;
            candidate=network;
        }
        return candidate;
    }
    private synchronized okhttp3.OkHttpClient aliyunClient(String region) {
        // NO_PROXY alone only skips HTTP proxies; the VPN still captures sockets
        // and DNS. Beijing ASR uses the physical network for both, like RSS/audio.
        Network network=region.equals("beijing")?podcastNetwork():null;
        if(network==null)return AliyunStream.CLIENT;
        if(asrClient==null || !network.equals(asrNetwork)) {
            if(asrClient!=null)asrClient.connectionPool().evictAll();
            asrNetwork=network;
            asrClient=AliyunStream.CLIENT.newBuilder()
                .socketFactory(network.getSocketFactory())
                .dns(host -> Arrays.asList(network.getAllByName(host)))
                .connectionPool(new okhttp3.ConnectionPool())
                .build();
        }
        return asrClient;
    }
    private HttpURLConnection open(String url,String range) throws Exception {
        // Bind both DNS and sockets to the underlying network for podcast traffic.
        // Do not bind the entire process: Gemini still needs the user's VPN/proxy.
        Network network=podcastNetwork();
        for(int attempts=0;attempts<8;attempts++) {
            URL address=new URL(url); if(!address.getProtocol().equals("https")&&!address.getProtocol().equals("http"))throw new IOException("Invalid URL");
            HttpURLConnection connection=null;
            int code;
            try {
                connection=(HttpURLConnection)(network==null?address.openConnection():network.openConnection(address,Proxy.NO_PROXY));
                configurePodcastConnection(connection,range);
                code=connection.getResponseCode();
            } catch(IOException directError) {
                if(connection!=null)connection.disconnect();
                if(network==null)throw directError;
                // Some VPNs forbid bypass, or a feed may require the proxy. Retry
                // on the system route without changing the Gemini route.
                connection=(HttpURLConnection)address.openConnection();
                configurePodcastConnection(connection,range);
                try {code=connection.getResponseCode();}catch(IOException fallbackError){connection.disconnect();fallbackError.addSuppressed(directError);throw fallbackError;}
            }
            if(code>=300&&code<400&&connection.getHeaderField("Location")!=null) {url=new URL(address,connection.getHeaderField("Location")).toString();connection.disconnect();continue;}
            return connection;
        } throw new IOException("Too many redirects");
    }
    private static void configurePodcastConnection(HttpURLConnection connection,String range) {
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(8000);connection.setReadTimeout(30000);connection.setRequestProperty("User-Agent","Mozilla/5.0");
        if(range!=null)connection.setRequestProperty("Range",range);
    }
    private JSONObject getJson(String url) throws Exception {
        HttpURLConnection connection=open(url,null);try {int code=connection.getResponseCode();if(code!=200)throw new CloudError(code);return new JSONObject(new String(read(connection.getInputStream(),12*1024*1024),StandardCharsets.UTF_8));}finally {connection.disconnect();}
    }
    private void audio(RequestData r,OutputStream output) throws Exception {
        TranscriptionJobs.Control control=transcriptionJobs.get(r.q("request_id"));
        if(control!=null && control.stopped.get())throw new IOException("Cancelled");
        String original=r.q("url"), resolved=resolvedAudio.get(original);
        HttpURLConnection connection;
        try {connection=open(resolved==null?original:resolved,r.headers.get("range"));}
        catch(IOException error){
            if(resolved==null)throw error;
            resolvedAudio.remove(original);connection=open(original,r.headers.get("range"));
        }
        if(resolved!=null && connection.getResponseCode()!=200 && connection.getResponseCode()!=206){
            connection.disconnect();resolvedAudio.remove(original);connection=open(original,r.headers.get("range"));
        }
        final HttpURLConnection source=connection;
        Runnable remove=control==null?()->{}:control.onCancel(source::disconnect);
        try {
            if(control!=null && control.stopped.get())throw new IOException("Cancelled");
            int code=connection.getResponseCode(); if(code!=200&&code!=206)throw new CloudError(code);
            // MediaExtractor seeks repeatedly. Replaying all tracking redirects
            // for every Range request can take minutes before decoding starts.
            resolvedAudio.put(original,connection.getURL().toString());
            String headers="HTTP/1.1 "+code+" OK\r\nContent-Type: "+(connection.getContentType()==null?"audio/mpeg":connection.getContentType())+"\r\nConnection: close\r\nAccept-Ranges: bytes\r\n";
            for(String name:new String[]{"Content-Length","Content-Range"}) {String value=connection.getHeaderField(name);if(value!=null)headers+=name+": "+value+"\r\n";}
            output.write(bytes(headers+"\r\n")); output.flush();
            // Headers are already committed. On disconnect, close this audio
            // response; do not append an HTTP 500 JSON response to MP3 bytes.
            try(InputStream input=connection.getInputStream()) {byte[] buffer=new byte[16384];int count;while(!closed&&(count=input.read(buffer))!=-1){output.write(buffer,0,count);}}
            catch(IOException ignored){}
        } finally {remove.run();connection.disconnect();}
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
    static String chooseDictionaryProvider(String preference,boolean proxyActive) {
        return preference.equals("auto")?(proxyActive?"gemini":"qwen"):preference;
    }
    private String dictionaryProvider() {
        ConnectivityManager manager=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);
        boolean active=false;
        if(manager!=null) {
            NetworkCapabilities caps=manager.getNetworkCapabilities(manager.getActiveNetwork());
            ProxyInfo proxy=manager.getDefaultProxy();
            active=(caps!=null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN))
                || (proxy!=null && ((proxy.getHost()!=null && proxy.getPort()>0)
                    || !android.net.Uri.EMPTY.equals(proxy.getPacFileUrl())));
        }
        return chooseDictionaryProvider(credentials.optString("dictionary_provider","auto"),active);
    }
    private HttpURLConnection cloudConnection(String url) throws Exception {
        // Honor the active VPN's advertised proxy (Clash ports can be dynamic).
        ConnectivityManager manager=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);
        ProxyInfo proxyInfo=manager==null?null:manager.getDefaultProxy();
        if(proxyInfo!=null && proxyInfo.getHost()!=null && proxyInfo.getPort()>0)
            return (HttpURLConnection)new URL(url).openConnection(new Proxy(Proxy.Type.HTTP,
                new InetSocketAddress(proxyInfo.getHost(),proxyInfo.getPort())));
        // A listening 7890 port can belong to an inactive Clash profile. Without
        // an advertised HTTP proxy, use the active VPN's normal system route.
        return (HttpURLConnection)new URL(url).openConnection();
    }
    private CloudError cloudError(HttpURLConnection connection,String service) {
        int status=500;String detail="";
        try {status=connection.getResponseCode();String body=new String(read(connection.getErrorStream(),16384),StandardCharsets.UTF_8);JSONObject error=new JSONObject(body).optJSONObject("error");if(error!=null)detail=error.optString("message");}catch(Exception ignored){}
        for(String name:new String[]{"aliyun_key_1","aliyun_key_2","gemini_key"}){String key=credentials.optString(name);if(!key.isEmpty())detail=detail.replace(key,"[redacted]");}
        final String message=service+" HTTP "+status+(detail.isEmpty()?"":"："+detail);
        return new CloudError(status){@Override public String getMessage(){return message;}};
    }
    static String safeError(Exception error) {
        for(Throwable cause=error;cause!=null;cause=cause.getCause()){
        if(cause instanceof CloudError || cause instanceof AliyunStream.Failure)return cause.getMessage();
        if(cause instanceof java.net.UnknownHostException)return "无法解析播客服务器地址，请检查网络或代理的 DNS 设置。";
        if(cause instanceof java.net.ConnectException)return "无法连接播客服务器，请检查网络或代理连接。";
        if(cause instanceof java.net.SocketTimeoutException)return "播客服务器连接超时，请重试或检查代理节点。";
        if(cause instanceof javax.net.ssl.SSLException)return "播客服务器的安全连接失败，请检查设备时间和代理。";
        }
        return "请求失败，请检查设备网络、API Key 或音频格式后重试。";
    }
    private class Events {
        final OutputStream output;final TranscriptionJobs.Control control;final AtomicBoolean stopped;
        String jobId="";
        AtomicFile correctionsFile;
        double recognitionStart=Double.NEGATIVE_INFINITY,recognitionEnd=Double.POSITIVE_INFINITY;
        Events(OutputStream output){this(output,new TranscriptionJobs.Control());}
        Events(OutputStream output,TranscriptionJobs.Control control){this.output=output;this.control=control;this.stopped=control.stopped;}
        synchronized void send(JSONObject item) throws IOException {
            if(stopped.get())throw new IOException("Cancelled");
            try {
                if(correctionsFile!=null){JSONArray records=load(correctionsFile).optJSONArray("cues");
                    if(item.optString("status").equals("cue"))item.put("cue",correctCue(item.getJSONObject("cue"),records));
                    if(item.optString("status").equals("resumed"))item.put("cues",correctCues(item.getJSONArray("cues"),records));
                }
                output.write(bytes("data: "+item+"\n\n"));output.flush();
            }catch(Exception e){control.stop();throw new IOException("Subtitle connection interrupted",e);}
        }
    }
    private String qwenDictionary(String prompt) throws Exception { return qwenJson(prompt,"查词"); }
    private String qwenJson(String prompt,String purpose) throws Exception { return qwenJson(prompt,purpose,true); }
    static JSONObject qwenPayload(String prompt,boolean jsonOutput) throws Exception {
        JSONObject payload=object("model","qwen-flash","enable_thinking",false,
            "messages",new JSONArray().put(object("role","user","content",prompt)));
        if(jsonOutput)payload.put("response_format",object("type","json_object"));
        return payload;
    }
    private String qwenJson(String prompt,String purpose,boolean jsonOutput) throws Exception {
        JSONObject config=credentials;
        List<String> keys=new ArrayList<>();
        for(String name:new String[]{"aliyun_key_1","aliyun_key_2"}){String key=config.optString(name).trim();if(!key.isEmpty()&&!keys.contains(key))keys.add(key);}
        if(keys.isEmpty())throw new AliyunStream.Failure("请在设置中填写阿里云百炼 Key。",false);
        String region=config.optString("aliyun_region","beijing");
        String host=region.equals("singapore")?"dashscope-intl.aliyuncs.com":"dashscope.aliyuncs.com";
        JSONObject payload=qwenPayload(prompt,jsonOutput);
        okhttp3.Request request=new okhttp3.Request.Builder().url("https://"+host+"/compatible-mode/v1/chat/completions")
            .header("Authorization","Bearer "+keys.get(Math.floorMod(dictionaryKey.getAndIncrement(),keys.size())))
            .post(okhttp3.RequestBody.create(bytes(payload.toString()),okhttp3.MediaType.get("application/json"))).build();
        try(okhttp3.Response response=aliyunClient(region).newBuilder().readTimeout(60,TimeUnit.SECONDS)
            .callTimeout(75,TimeUnit.SECONDS).build().newCall(request).execute()) {
            if(!response.isSuccessful())throw new CloudError(response.code()){
                @Override public String getMessage(){return "Qwen "+purpose+"返回 HTTP "+status+"，请检查阿里云密钥、权限或额度。";}
            };
            JSONObject result=new JSONObject(new String(read(response.body().byteStream(),1024*1024),StandardCharsets.UTF_8));
            return result.getJSONArray("choices").getJSONObject(0).getJSONObject("message").getString("content");
        }
    }
    private AtomicFile corrections(String url) throws Exception {return new AtomicFile(new File(checkpoint(url).getBaseFile().getPath()+".corrections"));}
    private static boolean sameRange(JSONObject a,JSONObject b) {
        return Math.abs(a.optDouble("source_start",a.optDouble("start"))-b.optDouble("source_start",b.optDouble("start")))<.002
            && Math.abs(a.optDouble("source_end",a.optDouble("end"))-b.optDouble("source_end",b.optDouble("end")))<.002;
    }
    private static JSONObject correctCue(JSONObject cue,JSONArray records) throws Exception {
        if(records!=null)for(int i=0;i<records.length();i++)if(sameRange(cue,records.getJSONObject(i)))return records.getJSONObject(i);
        return cue;
    }
    private static JSONArray correctCues(JSONArray cues,JSONArray records) throws Exception {
        JSONArray result=new JSONArray();for(int i=0;i<cues.length();i++)result.put(correctCue(cues.getJSONObject(i),records));return result;
    }
    private void regenerate(RequestData request,OutputStream output) throws Exception {
        String url=request.body.optString("audio_url");double start=request.body.optDouble("start",Double.NaN),end=request.body.optDouble("end",Double.NaN);
        if(!(url.startsWith("https://")||url.startsWith("http://"))||!Double.isFinite(start)||!Double.isFinite(end)||start<0||end<=start||end-start>300){
            response(output,400,"application/json",bytes(object("detail","单句音频范围无效，最长支持 5 分钟。").toString()));return;
        }
        AtomicFile overrides=corrections(url);JSONArray previous=load(overrides).optJSONArray("cues");
        if(previous!=null)for(int i=0;i<previous.length();i++){JSONObject cue=previous.getJSONObject(i);
            if(Math.abs(cue.getDouble("start")-start)<.002&&Math.abs(cue.getDouble("end")-end)<.002){start=cue.getDouble("source_start");end=cue.getDouble("source_end");break;}}
        List<String> keys=new ArrayList<>();for(String name:new String[]{"aliyun_key_1","aliyun_key_2"}){String key=credentials.optString(name);if(!key.isEmpty()&&!keys.contains(key))keys.add(key);}
        if(keys.isEmpty())throw new AliyunStream.Failure("请在设置中填写阿里云百炼 Key。",false);
        JSONArray recognized=new JSONArray();final double from=start,to=end;
        String token=TranscriptionJobs.token(request.body.optString("request_id"));
        TranscriptionJobs.Control control=transcriptionJobs.register(token);
        Events capture=new Events(new ByteArrayOutputStream(),control){
            @Override synchronized void send(JSONObject event) throws IOException {
                try{if(event.optString("status").equals("cue")){
                    JSONObject cue=event.getJSONObject("cue");double middle=(cue.getDouble("start")+cue.getDouble("end"))/2;
                    if(middle>=from&&middle<=to)recognized.put(cue);
                }}catch(Exception e){throw new IOException("Invalid sentence result",e);}
            }
        };
        capture.jobId=token;
        capture.recognitionStart=start;capture.recognitionEnd=end;
        AtomicFile temporary=new AtomicFile(File.createTempFile("sentence-",".json",context.getCacheDir()));
        try {
            decode(url,Math.max(0,start-.5),sentenceKey.getAndIncrement(),new JSONArray(),null,keys,temporary,capture,end+.5);
            if(control.stopped.get())throw new IOException("Cancelled");
            JSONObject result=null;for(int i=0;i<recognized.length();i++)result=AliyunStream.append(result,recognized.getJSONObject(i));
            if(result==null)throw new AliyunStream.Failure("这一句未识别到语音，原字幕已保留。",true);
            result.put("start",Math.max(start,result.getDouble("start"))).put("end",Math.min(end,result.getDouble("end")))
                .put("source_start",start).put("source_end",end);
            synchronized(this){JSONArray records=load(overrides).optJSONArray("cues"),next=new JSONArray();
                if(records!=null)for(int i=0;i<records.length();i++)if(!sameRange(result,records.getJSONObject(i)))next.put(records.getJSONObject(i));
                next.put(result);store(overrides,object("cues",next));}
            json(output,object("status","success","cue",result));
        } finally {transcriptionJobs.release(token,control);temporary.delete();}
    }
    private AtomicFile checkpoint(String url) throws Exception {
        byte[] digest=MessageDigest.getInstance("SHA-256").digest(bytes(url));StringBuilder name=new StringBuilder();for(byte value:digest)name.append(String.format("%02x",value&255));
        File folder=new File(context.getFilesDir(),"transcripts");folder.mkdirs();return new AtomicFile(new File(folder,name+"-aliyun-sentences-v2.json"));
    }
    private JSONObject load(AtomicFile file) {try{return new JSONObject(new String(read(file.openRead(),12*1024*1024),StandardCharsets.UTF_8));}catch(Exception e){return new JSONObject();}}
    private void store(AtomicFile file,JSONObject value) throws Exception {
        FileOutputStream stream=file.startWrite();try{stream.write(bytes(value.toString()));file.finishWrite(stream);}catch(Exception e){file.failWrite(stream);throw e;}
    }
    private void transcribe(RequestData request,Socket socket,OutputStream output) throws Exception {
        output.write(bytes("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n"));output.flush();
        String token=TranscriptionJobs.token(request.q("request_id"));
        TranscriptionJobs.Control control=transcriptionJobs.register(token);
        control.onCancel(()->{try{socket.close();}catch(IOException ignored){}});
        Events events=new Events(output,control);events.jobId=token;String url=request.q("audio_url");events.correctionsFile=corrections(url);Semaphore lock; synchronized(locks){lock=locks.get(url);if(lock==null){lock=new Semaphore(1);locks.put(url,lock);}}
        ScheduledFuture<?> heartbeat=timer.scheduleAtFixedRate(()->{try{events.send(object("status","heartbeat"));}catch(Exception e){control.stop();}},10,10,TimeUnit.SECONDS);
        boolean acquired=false;
        try {
            while(!(acquired=lock.tryAcquire(1,TimeUnit.SECONDS)))if(events.stopped.get()||closed)return;
            if(events.stopped.get()||closed)return;
            events.send(object("status","audio_source","audio_url","/api/audio?url="+encode(url)));
            AtomicFile file=checkpoint(url);if(request.q("force_refresh").equals("true")){file.delete();events.correctionsFile.delete();}JSONObject state=load(file);
            JSONArray cues=state.optJSONArray("cues");if(cues==null)cues=new JSONArray();
            if(state.optBoolean("complete")){file.getBaseFile().setLastModified(System.currentTimeMillis());events.send(object("status","cached","vtt",vtt(correctCues(cues,load(events.correctionsFile).optJSONArray("cues")))));return;}
            String transcript=request.q("transcript_url");
            if(!transcript.isEmpty()&&state.optDouble("until",0)==0) {
                try {HttpURLConnection cc=open(transcript,null);String content;try{content=new String(read(cc.getInputStream(),12*1024*1024),StandardCharsets.UTF_8);}finally{cc.disconnect();}
                    if(content.startsWith("WEBVTT")&&content.contains("-->")){events.send(object("status","official","vtt",content));return;}
                } catch(Exception ignored){}
            }
            List<String> keys=new ArrayList<>();for(String name:new String[]{"aliyun_key_1","aliyun_key_2"}){String key=credentials.optString(name).trim();if(!key.isEmpty()&&!keys.contains(key))keys.add(key);}
            if(keys.isEmpty()){events.send(object("status","error","detail","字幕服务未配置，请检查设置","retryable",false));return;}
            double until=state.optDouble("until",0);int index=state.optInt("next_chunk",0);
            JSONObject fragment=state.optJSONObject("pending_sentence");
            double coverage=fragment==null?until:(cues.length()==0?0:cues.getJSONObject(cues.length()-1).getDouble("end"));
            events.send(object("status","resumed","until",coverage,"cues",cues));
            events.send(object("status","progress","detail","设备正在解码，首段字幕就绪后播放"));
            decode(url,until,index,cues,fragment,keys,file,events);
        } catch(Exception error) {
            if(!events.stopped.get())try {Throwable cause=error;while(cause.getCause()!=null)cause=cause.getCause();boolean retryable=cause instanceof AliyunStream.Failure?((AliyunStream.Failure)cause).retryable:(!(cause instanceof CloudError)||((CloudError)cause).status==429||((CloudError)cause).status>=500);
                events.send(object("status","error","detail",safeError(error),"retryable",retryable));}catch(Exception ignored){}
        } finally {transcriptionJobs.release(token,control);heartbeat.cancel(true);if(acquired)lock.release();}
    }
    private static String time(double seconds) {long millis=Math.round(seconds*1000);return String.format(Locale.US,"%02d:%02d:%02d.%03d",millis/3600000,(millis/60000)%60,(millis/1000)%60,millis%1000);}
    private static String vtt(JSONArray cues) throws Exception {StringBuilder result=new StringBuilder("WEBVTT\n\n");for(int i=0;i<cues.length();i++){JSONObject cue=cues.getJSONObject(i);result.append(time(cue.getDouble("start"))).append(" --> ").append(time(cue.getDouble("end"))).append('\n').append(cue.getString("text")).append("\n\n");}return result.toString();}

    private double length(double offset) {return offset<300?Math.min(30,300-offset):offset<900?Math.min(120,900-offset):300;}
    private class StreamingPipeline implements AutoCloseable {
        final ArrayDeque<AliyunStream> tasks=new ArrayDeque<>();
        final JSONArray cues;final List<String> keys;final AtomicFile file;final Events events;final String region;
        JSONObject fragment; AliyunStream current;double offset;int index;
        double windowStart=Double.NEGATIVE_INFINITY,windowEnd=Double.POSITIVE_INFINITY;
        StreamingPipeline(double offset,int index,JSONArray cues,JSONObject fragment,List<String> keys,AtomicFile file,Events events){
            this.offset=offset;this.index=index;this.cues=cues;this.fragment=fragment;this.keys=keys;this.file=file;this.events=events;
            region=credentials.optString("aliyun_region","beijing");
        }
        void checkpoint(double until,int next,boolean complete) throws Exception {
            JSONObject state=object("until",until,"next_chunk",next,"cues",cues,"complete",complete);
            if(fragment!=null)state.put("pending_sentence",fragment);store(file,state);
        }
        void drain(boolean wait) throws Exception {
            while(!tasks.isEmpty()&&!events.stopped.get()&&!closed){
                AliyunStream first=tasks.peek();JSONObject message=first.messages.poll();
                if(message==null){first.check();if(!wait)return;Thread.sleep(20);continue;}
                if(message.getString("status").equals("finished")){
                    checkpoint(first.offset+first.duration,first.index+1,false);
                    if(fragment==null)events.send(object("status","chunk_ready","until",first.offset+first.duration));
                    tasks.remove().close();return;
                }
                JSONObject cue=message.getJSONObject("cue");double end=Math.min(cue.getDouble("end"),first.offset+first.duration);
                if(cue.getDouble("start")<first.offset||end<=cue.getDouble("start"))continue;cue.put("end",end);
                fragment=AliyunStream.append(fragment,cue);boolean complete=AliyunStream.endsSentence(fragment.getString("text"));
                JSONObject ready=fragment;
                if(complete){cues.put(fragment);fragment=null;}
                checkpoint(end,first.index,false);
                if(complete){events.send(object("status","cue","cue",ready));events.send(object("status","chunk_ready","until",end));}
            }
        }
        void send(byte[] pcm) throws Exception {
            if(events.stopped.get()||closed)throw new IOException("Cancelled");
            drain(false);
            if(current==null){
                while(tasks.size()>=keys.size()){drain(true);if(events.stopped.get()||closed)throw new IOException("Cancelled");}
                current=new AliyunStream(aliyunClient(region),keys.get(index%keys.size()),region,offset,index,events.control);tasks.add(current);
                current.windowStart=windowStart;current.windowEnd=windowEnd;
            }
            // Split a PCM frame exactly at the task boundary, including resumed offsets.
            int available=Math.max(2,(int)Math.round((length(offset)-current.duration)*32000));available-=available%2;
            if(pcm.length>available){send(Arrays.copyOfRange(pcm,0,available));send(Arrays.copyOfRange(pcm,available,pcm.length));return;}
            current.send(pcm);
            if(current.duration>=length(offset)-.00001){current.finish();offset+=current.duration;index++;current=null;}
            drain(false);
        }
        void finish() throws Exception {
            if(events.stopped.get()||closed)return;
            events.send(object("status","finishing"));
            if(current!=null){current.finish();offset+=current.duration;index++;current=null;}
            while(!tasks.isEmpty()&&!events.stopped.get()&&!closed)drain(true);
            if(events.stopped.get()||closed)return;
            if(fragment!=null){JSONObject ready=fragment;cues.put(ready);fragment=null;checkpoint(offset,index,false);events.send(object("status","cue","cue",ready));}
            if(cues.length()==0)throw new IOException("No speech recognized");
            checkpoint(offset,index,true);events.send(object("status","done"));
        }
        @Override public void close(){for(AliyunStream task:tasks)task.close();tasks.clear();}
    }
    private void decode(String url,double resume,int firstIndex,JSONArray cues,JSONObject fragment,List<String> keys,AtomicFile file,Events events) throws Exception {
        decode(url,resume,firstIndex,cues,fragment,keys,file,events,Double.POSITIVE_INFINITY);
    }
    private void decode(String url,double resume,int firstIndex,JSONArray cues,JSONObject fragment,List<String> keys,AtomicFile file,Events events,double endAt) throws Exception {
        MediaExtractor extractor=new MediaExtractor();MediaCodec codec=null;
        StreamingPipeline pipeline=new StreamingPipeline(resume,firstIndex,cues,fragment,keys,file,events);
        pipeline.windowStart=events.recognitionStart;pipeline.windowEnd=events.recognitionEnd;
        try {
            if(events.stopped.get()||closed)return;
            extractor.setDataSource("http://127.0.0.1:8557/api/audio?url="+encode(url)+"&request_id="+encode(events.jobId),Collections.singletonMap("User-Agent","PodcastLearner/2.0"));
            MediaFormat format=null;for(int i=0;i<extractor.getTrackCount();i++){MediaFormat candidate=extractor.getTrackFormat(i);if(candidate.getString(MediaFormat.KEY_MIME).startsWith("audio/")){format=candidate;extractor.selectTrack(i);break;}}
            if(format==null)throw new IOException("No supported audio track");
            if(format.containsKey(MediaFormat.KEY_DURATION))endAt=Math.min(endAt,format.getLong(MediaFormat.KEY_DURATION)/1000000.0+.25);
            // Seek to the preceding frame, then discard only its overlap with the checkpoint.
            if(resume>0)extractor.seekTo((long)(resume*1000000),MediaExtractor.SEEK_TO_PREVIOUS_SYNC);
            double decodedStart=Math.max(0,extractor.getSampleTime()/1000000.0);
            codec=MediaCodec.createDecoderByType(format.getString(MediaFormat.KEY_MIME));codec.configure(format,null,null,0);codec.start();
            int sourceRate=format.getInteger(MediaFormat.KEY_SAMPLE_RATE),channels=format.getInteger(MediaFormat.KEY_CHANNEL_COUNT),encoding=AudioFormat.ENCODING_PCM_16BIT;
            int targetRate=16000;long sourceFrame=Math.round(decodedStart*sourceRate),nextTargetFrame=Math.round(decodedStart*targetRate),skip=Math.round(resume*targetRate);
            ByteArrayOutputStream pcm=new ByteArrayOutputStream();boolean inputEnded=false,outputEnded=false;MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
            while(!outputEnded&&!events.stopped.get()&&!closed) {
                pipeline.drain(false);
                if(!inputEnded){int inputIndex=codec.dequeueInputBuffer(10000);if(inputIndex>=0){ByteBuffer input=codec.getInputBuffer(inputIndex);int size=extractor.readSampleData(input,0);if(size<0){codec.queueInputBuffer(inputIndex,0,0,0,MediaCodec.BUFFER_FLAG_END_OF_STREAM);inputEnded=true;}else{codec.queueInputBuffer(inputIndex,0,size,extractor.getSampleTime(),0);extractor.advance();}}}
                int outputIndex=codec.dequeueOutputBuffer(info,10000);
                if(outputIndex==MediaCodec.INFO_OUTPUT_FORMAT_CHANGED){MediaFormat decoded=codec.getOutputFormat();sourceRate=decoded.getInteger(MediaFormat.KEY_SAMPLE_RATE);channels=decoded.getInteger(MediaFormat.KEY_CHANNEL_COUNT);encoding=decoded.containsKey(MediaFormat.KEY_PCM_ENCODING)?decoded.getInteger(MediaFormat.KEY_PCM_ENCODING):AudioFormat.ENCODING_PCM_16BIT;}
                if(outputIndex>=0){ByteBuffer decoded=codec.getOutputBuffer(outputIndex).order(ByteOrder.LITTLE_ENDIAN);decoded.position(info.offset);decoded.limit(info.offset+info.size);int sampleBytes=encoding==AudioFormat.ENCODING_PCM_FLOAT?4:2;
                    while(decoded.remaining()>=channels*sampleBytes){double mixed=0;for(int channel=0;channel<channels;channel++)mixed+=encoding==AudioFormat.ENCODING_PCM_FLOAT?decoded.getFloat()*32767:decoded.getShort();short sample=(short)Math.max(-32768,Math.min(32767,mixed/channels));
                        if(nextTargetFrame>=endAt*targetRate){outputEnded=true;break;}
                        if(sourceFrame*targetRate>=nextTargetFrame*sourceRate){if(nextTargetFrame>=skip){pcm.write(sample&255);pcm.write((sample>>8)&255);}nextTargetFrame++;}sourceFrame++;
                        if(pcm.size()>=3200){pipeline.send(pcm.toByteArray());pcm.reset();}
                    }
                    outputEnded=outputEnded||(info.flags&MediaCodec.BUFFER_FLAG_END_OF_STREAM)!=0;codec.releaseOutputBuffer(outputIndex,false);
                }
            }
            if(events.stopped.get()||closed)return;
            if(pcm.size()>0)pipeline.send(pcm.toByteArray());
            pipeline.finish();
        } finally {pipeline.close();if(codec!=null){try{codec.stop();}catch(Exception ignored){}codec.release();}extractor.release();}
    }
}
