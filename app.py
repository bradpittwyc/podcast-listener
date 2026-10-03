import os
import re
import json
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from google import genai as genai_sdk
from dotenv import load_dotenv
from transcription import transcript_events, stream_with_heartbeat, to_vtt, cache_paths

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
gemini_client = genai_sdk.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

app = FastAPI(title="Podcast Listener — RSS Stream & AI Subtitle Tool")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "subtitle_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/cache", StaticFiles(directory=CACHE_DIR), name="cache")

def upgrade_to_hd_image(img_url: str) -> str:
    if not img_url:
        return ""
    if "mzstatic.com" in img_url:
        hd = re.sub(r'\d+x\d+bb\.(png|jpg|jpeg)', '1200x1200bb.jpg', img_url)
        hd = re.sub(r'\d+x\d+bb', '1200x1200bb', hd)
        return hd
    return img_url

KNOWN_CC_SHOWS = {"1200361736", "1322200189", "1222114325", "1508485281", "1089022756", "360084272", "1379959217"}

def parse_rss_feed(feed_url: str):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    resp = requests.get(feed_url, headers=headers, timeout=15)
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"RSS parse error: {str(e)}")

    channel = root.find("channel")
    if channel is None:
        raise HTTPException(status_code=400, detail="Invalid RSS: missing <channel>")

    ns = {
        'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
        'content': 'http://purl.org/rss/1.0/modules/content/',
        'media': 'http://search.yahoo.com/mrss/',
        'podcast': 'https://podcastindex.org/namespace/1.0',
        'podcast_old': 'http://podcastindex.org/namespace/1.0'
    }

    def get_text(el, tag, default=""):
        e = el.find(tag)
        return e.text.strip() if e is not None and e.text else default

    def get_ns(el, p, tag, default=""):
        if p in ns:
            e = el.find(f"{{{ns[p]}}}{tag}")
            return e.text.strip() if e is not None and e.text else default
        return default

    title = get_text(channel, "title", "Unknown Podcast")
    desc = get_text(channel, "description", "")
    author = get_ns(channel, "itunes", "author", get_text(channel, "author", ""))

    image = ""
    itunes_img = channel.find(f"{{{ns['itunes']}}}image")
    if itunes_img is not None and "href" in itunes_img.attrib:
        image = itunes_img.attrib["href"]
    else:
        img_el = channel.find("image")
        if img_el is not None:
            image = get_text(img_el, "url", "")
    image = upgrade_to_hd_image(image)

    episodes = []
    for idx, item in enumerate(channel.findall("item"), 1):
        ep_title = get_text(item, "title", f"Episode {idx}")
        ep_pubdate = get_text(item, "pubDate", "")
        ep_duration = get_ns(item, "itunes", "duration", "")
        ep_desc = get_ns(item, "itunes", "summary", get_text(item, "description", ""))
        ep_desc_clean = re.sub(r'<[^>]+>', '', ep_desc).strip()
        if len(ep_desc_clean) > 300:
            ep_desc_clean = ep_desc_clean[:300] + "..."

        audio_url = ""
        enclosure = item.find("enclosure")
        if enclosure is not None and "url" in enclosure.attrib:
            audio_url = enclosure.attrib["url"]
        else:
            mc = item.find(f"{{{ns['media']}}}content")
            if mc is not None and "url" in mc.attrib:
                audio_url = mc.attrib["url"]

        transcript_url = ""
        for p_ns in ['podcast', 'podcast_old']:
            te = item.find(f"{{{ns[p_ns]}}}transcript")
            if te is not None and "url" in te.attrib:
                transcript_url = te.attrib["url"]
                break

        if audio_url:
            episodes.append({
                "id": idx,
                "title": ep_title,
                "pubDate": ep_pubdate,
                "duration": ep_duration,
                "description": ep_desc_clean,
                "audioUrl": audio_url,
                "transcriptUrl": transcript_url,
            })

    return {
        "title": title,
        "description": desc,
        "author": author,
        "image": image,
        "hasTranscript": any(ep.get("transcriptUrl") for ep in episodes),
        "totalEpisodes": len(episodes),
        "episodes": episodes
    }

def get_local_proxy():
    # Use an explicitly configured HTTP proxy; never guess the protocol of open ports.
    proxy = os.environ.get("PODCAST_PROXY", "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


@app.get("/api/audio")
def stream_audio(url: str, request: Request):
    # Use one stable playback URL while transcription downloads in the background.
    # Once the full cache is available, subsequent range requests use that file.
    _, audio_path, _ = cache_paths(CACHE_DIR, url)
    if audio_path.exists() and audio_path.stat().st_size:
        return FileResponse(audio_path, media_type="audio/mpeg")
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        upstream = requests.get(url, headers=headers, stream=True, timeout=(10, 15), proxies=get_local_proxy())
        if upstream.status_code not in (200, 206):
            status = upstream.status_code
            upstream.close()
            raise HTTPException(status_code=status, detail="音频源暂时不可用。")
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="无法连接音频源，请检查网络。")

    def audio_bytes():
        try:
            yield from upstream.iter_content(chunk_size=16 * 1024)
        finally:
            upstream.close()
    forwarded = {name: upstream.headers[name] for name in ("Content-Range", "Accept-Ranges") if name in upstream.headers}
    if "Content-Length" in upstream.headers and upstream.headers.get("Content-Encoding", "identity") == "identity":
        forwarded["Content-Length"] = upstream.headers["Content-Length"]
    return StreamingResponse(audio_bytes(), status_code=upstream.status_code,
                             media_type=upstream.headers.get("Content-Type", "audio/mpeg"),
                             headers=forwarded, background=BackgroundTask(upstream.close))


@app.get("/api/transcribe")
def get_or_generate_transcript(audio_url: str, title: str = "", transcript_url: str = "", force_refresh: bool = False):
    cues = []
    local_audio = None
    for event in transcript_events(audio_url, transcript_url, force_refresh, CACHE_DIR, get_local_proxy()):
        status = event["status"]
        if status in ("official", "cached"):
            return {"source": status, "vtt": event["vtt"], **({"local_audio": event["local_audio"]} if "local_audio" in event else {})}
        if status == "audio_ready":
            local_audio = event["local_audio"]
        elif status == "cue":
            cues.append(event["cue"])
        elif status == "error":
            return {"source": "error", "detail": event["detail"], "vtt": ""}
    return {"source": "groq_api", "vtt": to_vtt(cues), "local_audio": local_audio}


@app.get("/api/transcribe_stream")
def transcribe_stream(audio_url: str, title: str = "", transcript_url: str = "", force_refresh: bool = False):
    proxies = get_local_proxy()
    def events(stopped):
        return transcript_events(audio_url, transcript_url, force_refresh, CACHE_DIR, proxies, stopped)
    return StreamingResponse(stream_with_heartbeat(events), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.api_route("/api/define", methods=["GET", "POST", "HEAD"])
def define_word(word: str = Query(..., min_length=1), context: str = ""):
    """
    Use Gemini AI to look up a word with phonetic, part of speech, English definition, 
    Chinese translation, and example sentence. Optionally context-aware.
    """
    prompt = f"""You are a professional lexicographer and ESL teacher. Analyze the English word "{word}".
    {"Context sentence: " + context if context else ""}

    Return ONLY a raw JSON object (no markdown codeblock, no triple backticks) with this structure:
    {{
        "word": "{word}",
        "phonetic": "/.../",
        "pos": "noun/verb/adj/adv etc.",
        "definition_en": "Concise English definition",
        "translation_cn": "准确中文释义（含词性）",
        "example": "An engaging example sentence containing the word.",
        "example_cn": "例句的中文翻译",
        "context_note": "If context sentence was provided, brief note on how it is used in that specific context (in Chinese), otherwise empty string"
    }}
    """
    if not gemini_client:
        return {"status": "error", "message": "GEMINI_API_KEY environment variable not configured"}

    try:
        response = gemini_client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        text = (response.text or "").strip()
        # Clean markdown formatting if present
        if text.startswith("```"):
            text = re.sub(r'^```[a-z]*\n', '', text)
            text = re.sub(r'\n```$', '', text)
        data = json.loads(text)
        return {"status": "success", "data": data}
    except Exception as e:
        print("Gemini define error:", str(e))
        return {"status": "error", "message": str(e)}

@app.get("/api/top-charts")
def get_top_charts(country: str = "us", limit: int = 30):
    url = f"https://itunes.apple.com/{country.lower()}/rss/toppodcasts/limit={limit}/json"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        entries = data.get("feed", {}).get("entry", [])
        results = []
        for e in entries:
            try:
                pid = e['id']['attributes']['im:id']
                name = e['im:name']['label']
                artist = e['im:artist']['label']
                img = upgrade_to_hd_image(e['im:image'][-1]['label'])
                category = e.get('category', {}).get('attributes', {}).get('label', '')
                has_cc = pid in KNOWN_CC_SHOWS or "Daily" in name or "NPR" in artist
                results.append({
                    "collectionId": pid,
                    "collectionName": name,
                    "artistName": artist,
                    "artworkUrl600": img,
                    "category": category,
                    "hasTranscript": has_cc
                })
            except Exception:
                continue
        return {"country": country, "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/search")
def search_podcasts(query: str = Query(..., min_length=1), country: str = "us"):
    query_str = query.strip()

    if query_str.startswith("http://") or query_str.startswith("https://"):
        if "podcasts.apple.com" not in query_str:
            try:
                feed_data = parse_rss_feed(query_str)
                return {
                    "collectionId": "rss",
                    "artistName": feed_data.get("author"),
                    "collectionName": feed_data.get("title"),
                    "artworkUrl600": feed_data.get("image"),
                    "hasTranscript": feed_data.get("hasTranscript", False),
                    "feedUrl": query_str,
                    "feedData": feed_data
                }
            except Exception:
                pass

    match = re.search(r'id(\d+)', query_str)
    if match or query_str.isdigit():
        pid = match.group(1) if match else query_str
        try:
            return lookup_podcast(pid, country=country)
        except HTTPException:
            pass

    url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query_str)}&media=podcast&country={country}&limit=20"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        results = []
        for item in data.get("results", []):
            pid = str(item.get("collectionId"))
            name = item.get("collectionName", "")
            results.append({
                "collectionId": item.get("collectionId"),
                "artistName": item.get("artistName"),
                "collectionName": name,
                "artworkUrl600": upgrade_to_hd_image(item.get("artworkUrl600") or item.get("artworkUrl100")),
                "feedUrl": item.get("feedUrl"),
                "hasTranscript": pid in KNOWN_CC_SHOWS or "Daily" in name
            })
        return {"results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/lookup")
def lookup_podcast(id: str, country: str = "us"):
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = f"https://itunes.apple.com/lookup?id={id}&country={country}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("resultCount", 0) == 0:
            search_url = f"https://itunes.apple.com/search?term={id}&media=podcast&country={country}&limit=1"
            resp = requests.get(search_url, headers=headers, timeout=10)
            data = resp.json()
        if data.get("resultCount", 0) > 0:
            item = data["results"][0]
            feed_url = item.get("feedUrl")
            feed_data = parse_rss_feed(feed_url) if feed_url else {}
            return {
                "collectionId": item.get("collectionId"),
                "artistName": item.get("artistName"),
                "collectionName": item.get("collectionName"),
                "artworkUrl600": upgrade_to_hd_image(item.get("artworkUrl600") or item.get("artworkUrl100")),
                "feedUrl": feed_url,
                "hasTranscript": feed_data.get("hasTranscript", False),
                "feedData": feed_data
            }
        raise HTTPException(status_code=404, detail="Podcast not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/parse-feed")
def get_feed_by_url(url: str):
    return parse_rss_feed(url)

@app.get("/", response_class=HTMLResponse)
def index_page():
    path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Loading...</h1>"

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8557"))
    print(f"Podcast Learner starting at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
