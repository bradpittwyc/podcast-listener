import os
import asyncio
import re
import json
import tempfile
import threading
import math
import shutil
import aliyun
import corrections
import subtitle_translation
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import requests
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from google import genai as genai_sdk
from dotenv import load_dotenv, set_key
from transcription import transcript_events, stream_with_heartbeat, to_vtt, cache_paths
from transcription_jobs import TranscriptionJobs

transcription_jobs = TranscriptionJobs()

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
app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

SETTINGS_PATH = os.path.join(BASE_DIR, ".env")
settings_lock = threading.Lock()
corrections_lock = threading.Lock()
KEY_FIELDS = {"gemini_key": "GEMINI_API_KEY",
              "aliyun_key_1": "DASHSCOPE_API_KEY_1", "aliyun_key_2": "DASHSCOPE_API_KEY_2"}
OPTION_FIELDS = {"subtitle_provider": ("AI_PROVIDER", ("aliyun",)),
                 "tutor_provider": ("TUTOR_PROVIDER", ("qwen", "gemini")),
                 "translation_provider": ("TRANSLATION_PROVIDER", ("gemini", "qwen")),
                 "dictionary_provider": ("DICTIONARY_PROVIDER", ("auto", "qwen", "gemini")),
                 "aliyun_region": ("ALIYUN_REGION", ("beijing", "singapore"))}

def provider_settings():
    return {field: (os.environ.get(name) if os.environ.get(name) in values else values[0]) for field, (name, values) in OPTION_FIELDS.items()}

def key_settings():
    return {field: bool(os.environ.get(name, "") or (os.environ.get("DASHSCOPE_API_KEY", "") if field == "aliyun_key_1" else ""))
            for field, name in KEY_FIELDS.items()}

def dictionary_proxy():
    explicit = get_local_proxy()
    proxies = explicit or urllib.request.getproxies()
    return proxies.get('https') or proxies.get('all') or proxies.get('http')

def dictionary_provider():
    choice = provider_settings()['dictionary_provider']
    return ('gemini' if dictionary_proxy() else 'qwen') if choice == 'auto' else choice

@app.get("/api/settings")
def get_settings():
    return {"configured": key_settings(), "options": provider_settings(), "dictionary_route": dictionary_provider()}

@app.post("/api/settings")
async def save_settings(request: Request):
    global GEMINI_API_KEY, gemini_client
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "请从本地页面保存设置")
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "需要 JSON 请求")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "设置格式错误")
    if not isinstance(body, dict) or set(body) - (set(KEY_FIELDS) | set(OPTION_FIELDS)):
        raise HTTPException(400, "设置格式错误")
    updates = {}
    for field, value in body.items():
        if field in OPTION_FIELDS:
            name, allowed = OPTION_FIELDS[field]
            if value not in allowed:
                raise HTTPException(400, "字幕服务或地域设置错误")
            updates[name] = value
            continue
        if not isinstance(value, str) or len(value) > 512 or any(c in value for c in "\r\n\x00"):
            raise HTTPException(400, "API Key 格式错误")
        if value.strip():
            updates[KEY_FIELDS[field]] = value.strip()
    new_client = genai_sdk.Client(api_key=updates["GEMINI_API_KEY"]) if "GEMINI_API_KEY" in updates else None
    with settings_lock:
        fd, temporary = tempfile.mkstemp(prefix=".env-", dir=BASE_DIR)
        os.close(fd)
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, encoding="utf-8") as source, open(temporary, "w", encoding="utf-8") as target:
                    target.write(source.read())
            for name, value in updates.items():
                set_key(temporary, name, value)
            os.replace(temporary, SETTINGS_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        os.environ.update(updates)
        if new_client is not None:
            GEMINI_API_KEY = updates["GEMINI_API_KEY"]
            gemini_client = new_client
    return {"configured": key_settings(), "options": provider_settings()}

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
        elif status == "resumed":
            cues = event["cues"]
        elif status == "cue":
            cues.append(event["cue"])
        elif status == "error":
            return {"source": "error", "detail": event["detail"], "vtt": ""}
    return {"source": "aliyun_streaming", "vtt": to_vtt(cues), "local_audio": local_audio}


@app.get("/api/transcribe_stream")
def transcribe_stream(audio_url: str, title: str = "", transcript_url: str = "", force_refresh: bool = False, request_id: str = ""):
    try:
        token = transcription_jobs.token(request_id)
    except ValueError:
        raise HTTPException(400, '无效的转写任务 ID。')
    stopped = transcription_jobs.register(token)
    proxies = get_local_proxy()
    def events(signal):
        return transcript_events(audio_url, transcript_url, force_refresh, CACHE_DIR, proxies, signal)
    def stream():
        try:
            yield from stream_with_heartbeat(events, stopped)
        finally:
            transcription_jobs.release(token, stopped)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post('/api/cancel_transcription')
def cancel_transcription(body: dict):
    try:
        token = transcription_jobs.token(body.get('request_id'))
    except ValueError:
        raise HTTPException(400, '无效的转写任务 ID。')
    transcription_jobs.cancel(token)
    return {'status': 'cancelled'}


@app.post('/api/retranscribe_sentence')
def retranscribe_sentence(body: dict):
    url, start, end = body.get('audio_url'), body.get('start'), body.get('end')
    if (not isinstance(url, str) or urllib.parse.urlparse(url).scheme not in ('http', 'https')
            or any(type(value) not in (float, int) or not math.isfinite(value) for value in (start, end))
            or not 0 <= start < end or end - start > 300):
        raise HTTPException(400, '单句音频范围无效，最长支持 5 分钟。')
    if not shutil.which('ffmpeg'):
        raise HTTPException(503, '找不到 FFmpeg。')
    try:
        token = transcription_jobs.token(body.get('request_id'))
    except ValueError:
        raise HTTPException(400, '无效的转写任务 ID。')
    stopped = transcription_jobs.register(token)
    try:
        for previous in corrections.load(corrections.path_for(CACHE_DIR, url)):
            if abs(previous['start'] - start) < .002 and abs(previous['end'] - end) < .002:
                start, end = previous['source_start'], previous['source_end']
                break
        _, audio_path, _ = cache_paths(CACHE_DIR, url)
        cue = aliyun.regenerate(url, audio_path, start, end, stopped=stopped)
        if stopped.is_set():
            raise HTTPException(409, '转写已取消。')
        with corrections_lock:
            corrections.save(corrections.path_for(CACHE_DIR, url), cue)
        return {'status': 'success', 'cue': cue}
    except RuntimeError as error:
        raise HTTPException(502, str(error))
    finally:
        transcription_jobs.release(token, stopped)


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
    if dictionary_provider() == 'qwen':
        keys = aliyun.api_keys()
        if not keys:
            return {"status": "error", "message": "请在设置中填写阿里云百炼 Key。"}
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(aliyun.root() + '/compatible-mode/v1/chat/completions',
                    headers={'Authorization': 'Bearer ' + keys[0]},
                    json={'model': 'qwen-flash', 'enable_thinking': False,
                          'response_format': {'type': 'json_object'},
                          'messages': [{'role': 'user', 'content': prompt}]}, timeout=(15, 60))
                if not response.ok:
                    return {'status': 'error', 'message': f'Qwen 查词返回 HTTP {response.status_code}，请检查密钥、权限或额度。'}
                data = json.loads(response.json()['choices'][0]['message']['content'])
                return {'status': 'success', 'provider': 'qwen', 'data': data}
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return {'status': 'error', 'message': 'Qwen 查词失败，请检查阿里云连接后重试。'}
    if not gemini_client:
        return {"status": "error", "message": "GEMINI_API_KEY environment variable not configured"}

    try:
        proxy = dictionary_proxy()
        if proxy:
            # The SDK ignores Windows system-proxy settings. Use the detected
            # HTTP route explicitly, so choosing Gemini also uses that proxy.
            with requests.Session() as session:
                session.trust_env = False
                response = session.post('https://generativelanguage.googleapis.com/v1beta/models/'
                    + os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash') + ':generateContent',
                    proxies={'http': proxy, 'https': proxy}, headers={'x-goog-api-key': GEMINI_API_KEY},
                    json={'contents': [{'parts': [{'text': prompt}]}],
                          'generationConfig': {'responseMimeType': 'application/json'}}, timeout=(15, 60))
                if not response.ok:
                    return {'status': 'error', 'message': f'Gemini 查词返回 HTTP {response.status_code}，请检查代理或密钥。'}
                parts = response.json()['candidates'][0]['content']['parts']
                text = ''.join(part.get('text', '') for part in parts if not part.get('thought'))
                return {'status': 'success', 'provider': 'gemini', 'data': json.loads(text)}
        response = gemini_client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
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

@app.post("/api/ask")
async def ask_podcast_ai(request: Request):
    """
    Use the selected tutor model with the client's current subtitle snapshot.
    """
    provider = provider_settings()["tutor_provider"]
    if (provider == "gemini" and not gemini_client) or (provider == "qwen" and not aliyun.api_keys()):
        return {"status": "error", "message": "助教服务未配置，请检查设置。"}

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(body, dict) or any(not isinstance(body.get(name, ""), str) for name in ("question", "selected_text", "audio_url", "full_transcript")):
        raise HTTPException(status_code=400, detail="Invalid question fields")

    question = (body.get("question") or "").strip()
    selected_text = (body.get("selected_text") or "").strip()
    audio_url = (body.get("audio_url") or "").strip()
    client_transcript = (body.get("full_transcript") or "").strip()

    if not question and not selected_text:
        return {"status": "error", "message": "提问内容或勾选的字幕不能同时为空"}

    # Use the exact subtitle snapshot submitted by the player, including corrections.
    transcript_text = client_transcript
    if not transcript_text and audio_url:
        try:
            _, _, vtt_path = cache_paths(CACHE_DIR, audio_url)
            if vtt_path.exists():
                vtt_content = corrections.apply_vtt(vtt_path.read_text(encoding="utf-8"), corrections.load(corrections.path_for(CACHE_DIR, audio_url)))
                lines = []
                for line in vtt_content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit():
                        continue
                    lines.append(line)
                transcript_text = " ".join(lines)
        except Exception as e:
            print("Failed to read VTT cache for Q&A:", e)

    if not transcript_text:
        raise HTTPException(status_code=409, detail="请等待首条字幕加载后提问。")
    # Include all currently available subtitles without truncation.
    bg_context = transcript_text
    transcript_status = "本期字幕已全部转写完成。" if body.get("transcript_complete") is True else "本期字幕仍在转写，以下仅为当前已获得的全部内容；回答应基于这些内容，不要推测尚未获得的部分。"
    quote_section = f"【用户勾选引用的字幕语句】:\n{selected_text}\n" if selected_text else ""
    user_query = question if question else "请详细解析上述勾选字幕句子的语法结构、生词习语和地道用法。"

    prompt = f"""你是一个专业、耐心的英语播客学习助教。
用户正在边听播客边学习，并向你提问。{transcript_status}
以下是当前已获得的全部字幕背景：
--- 播客背景转写 ---
{bg_context}
--- 背景结束 ---

{quote_section}
【用户的问题】:
{user_query}

请结合播客的上下文，给出清晰、详实、通俗易懂的解答：
1. 若涉及生词或短语：说明其在此处播客语境中的确切含义与地道用法，并给出生动的例句；
2. 若涉及复杂句式或语法：拆解句子结构并进行通俗解释；
3. 若询问播客内容或背景：结合当前已获得的全部字幕进行提炼和解释。
回答使用流畅自然的中文，可适当使用 Markdown 格式（粗体、列表、引用等），便于排版阅读。"""

    try:
        if provider == "qwen":
            answer = (await asyncio.to_thread(qwen_completion, prompt, False)).strip()
        else:
            response = await asyncio.wait_for(gemini_client.aio.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"), contents=prompt,
            ), timeout=150)
            answer = (response.text or "").strip()
        if not answer:
            return {"status": "error", "message": "助教未返回回答，请重试。"}
        return {"status": "success", "provider": provider, "answer": answer}
    except Exception:
        return {"status": "error", "message": "助教请求失败，请检查网络和助教模型设置后重试。"}

@app.post("/api/translate_subtitles")
async def translate_subtitles(request: Request):
    try:
        sentences = subtitle_translation.sentences_from(await request.json())
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="字幕请求无效，请分批提交非空字幕。")
    provider = provider_settings()["translation_provider"]
    if provider == "gemini" and not gemini_client:
        return {"status": "error", "message": "翻译服务未配置，请检查设置。"}
    if provider == "qwen" and not aliyun.api_keys():
        return {"status": "error", "message": "翻译服务未配置，请检查设置。"}
    try:
        prompt = subtitle_translation.prompt_for(sentences)
        if provider == "qwen":
            text = await asyncio.to_thread(qwen_completion, prompt, True)
        else:
            response = await asyncio.wait_for(gemini_client.aio.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
                contents=prompt, config={"response_mime_type": "application/json"},
            ), timeout=150)
            text = response.text or ""
        translated = subtitle_translation.translations_from(text, sentences)
        return {"status": "success", "provider": provider, "translations": translated}
    except ValueError as error:
        return {"status": "error", "message": str(error)}
    except Exception:
        return {"status": "error", "message": "字幕翻译失败，请检查网络和翻译模型设置后点击双语字幕重试。"}


def qwen_completion(prompt, json_output=True):
    payload = {"model": "qwen-flash", "enable_thinking": False,
               "messages": [{"role": "user", "content": prompt}]}
    if json_output:
        payload["response_format"] = {"type": "json_object"}
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(aliyun.root() + "/compatible-mode/v1/chat/completions",
            headers={"Authorization": "Bearer " + aliyun.api_keys()[0]},
            json=payload, timeout=(15, 120))
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


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
                "feedData": feed_data,
                "feed": feed_data,
                "meta": {
                    "collectionId": item.get("collectionId"),
                    "artistName": item.get("artistName"),
                    "collectionName": item.get("collectionName"),
                    "artworkUrl600": upgrade_to_hd_image(item.get("artworkUrl600") or item.get("artworkUrl100")),
                }
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
