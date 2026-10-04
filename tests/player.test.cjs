const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function player(storage = new Map()) {
    const elements = new Map();
    const element = () => ({style: {}, classList: {add() {}, remove() {}, toggle() {}}, scrollIntoView() {}, appendChild() {}, remove() {}, innerHTML: '', textContent: '', value: ''});
    const audio = {paused: true, currentTime: 0, duration: 1200, playbackRate: 1, playCount: 0,
        pause() { this.paused = true; },
        play() { this.paused = false; this.playCount++; return Promise.resolve(); }};
    elements.set('ga', audio);
    const streams = [];
    class EventSource {
        constructor(url) { this.url = url; streams.push(this); }
        close() { this.closed = true; }
        emit(data) { this.onmessage({data: JSON.stringify(data)}); }
    }
    const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
        querySelectorAll() { return []; }, addEventListener() {}, createElement: element};
    const timers = [], intervals = [], fetchCalls = [];
    let clock = 0;
    class TestDate extends Date { static now() { return clock; } }
    const context = vm.createContext({document, window: {}, EventSource, console, Date: TestDate, AbortController,
        setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; },
        clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; },
        setInterval(fn, ms) { intervals.push({fn, ms}); return intervals.length; },
        clearInterval(id) { if (intervals[id - 1]) intervals[id - 1].cleared = true; },
        fetch: async (...args) => { fetchCalls.push(args); return {json: async () => args[0] === '/api/retranscribe_sentence'
            ? {status:'success',cue:{start:.2,end:19.8,text:'Corrected sentence.',source_start:0,source_end:20}}
            : {status:'success', answer:'Answer'}}; },
        localStorage: {getItem(key) { return storage.get(key) ?? null; },setItem(key,value) {storage.set(key,value);}}});
    const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
    vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
    vm.runInContext('toast = () => {}', context);
    const run = (code) => vm.runInContext(code, context);
    const start = (url = 'https://example.com/one.mp3') => run(`startPlay(${JSON.stringify(url)}, 'Episode', 'Show', '', '', '')`);
    const retry = () => { const timer = timers.find(t => !t.cleared); assert.ok(timer); timer.cleared = true; timer.fn(); };
    const advance = (ms) => { clock += ms; intervals.filter(i => !i.cleared).forEach(i => i.fn()); };
    return {audio, streams, run, start, retry, advance, timers, elements, fetchCalls};
}

const cue = (start, end) => ({status: 'cue', cue: {start, end, text: 'Hello world'}});

test('completion renders the final sentence and stops all recovery and status updates', () => {
    const p=player();p.start();const stream=p.streams[0];
    stream.emit(cue(0,20));stream.emit({status:'chunk_ready',until:30});
    stream.emit({status:'finishing'});
    assert.match(p.elements.get('refreshBtn').innerHTML,/收尾中/);
    stream.emit({status:'cue',cue:{start:30,end:32,text:'Final sentence.'}});
    stream.emit({status:'done'});
    assert.equal(p.run('isTranscribing'),false);
    assert.equal(p.run('subtitlesComplete'),true);
    assert.match(p.elements.get('subScroll').innerHTML,/id="cue-1"/);
    assert.match(p.elements.get('subScroll').innerHTML,/Final/);
    assert.doesNotMatch(p.elements.get('refreshBtn').innerHTML,/转写中|收尾中/);
    assert.equal(stream.closed,true);
    stream.emit({status:'progress',detail:'Stale progress'});stream.onerror();p.advance(200000);
    assert.equal(p.timers.filter(t=>!t.cleared).length,0);
    assert.equal(p.streams.length,1);
});

test('switching episodes explicitly cancels both the stream and sentence cloud jobs', async () => {
    const p=player();p.start();const old=p.streams[0];old.emit(cue(0,20));
    const id=new URL(old.url,'http://localhost').searchParams.get('request_id');
    p.run("fetch=(url,options)=>{if(url==='/api/retranscribe_sentence'){window.sentenceBody=JSON.parse(options.body);return new Promise(resolve=>window.completeSentence=resolve)}globalThis.cancelledIds??=[];cancelledIds.push(JSON.parse(options.body).request_id);return Promise.resolve({json:async()=>({status:'cancelled'})})}");
    const pending=p.run('regenerateSentence({stopPropagation(){}},0)');
    p.start('https://example.com/next.mp3');
    assert.equal(old.closed,true);
    assert.ok(p.run('cancelledIds').includes(id));
    assert.ok(p.run('cancelledIds.includes(window.sentenceBody.request_id)'));
    p.run("window.completeSentence({json:async()=>({status:'success',cue:{start:0,end:20,text:'Stale'}})})");
    await pending;assert.equal(p.run('cues.length'),0);
});

test('intermittent progress cannot reset the total automatic retry limit', () => {
    const p=player();p.start();
    for(let i=0;i<20;i++){
        p.streams.at(-1).emit({status:'chunk_ready',until:(i+1)*30});
        p.streams.at(-1).onerror();p.retry();
    }
    p.streams.at(-1).emit({status:'chunk_ready',until:630});p.streams.at(-1).onerror();
    assert.equal(p.timers.filter(t=>!t.cleared).length,0);
    assert.equal(p.run('isTranscribing'),false);
});

test('subtitle exports preserve corrected text and millisecond timing', () => {
    const p = player();
    const snapshot = {episode:{title:'Episode',author:'Author'},complete:false,cues:[{start:59.9996,end:62.25,text:'Corrected sentence.'}]};
    p.run(`globalThis.exportSample=${JSON.stringify(snapshot)}`);
    assert.match(p.run("transcriptExportText(exportSample,'srt')"), /00:01:00,000 --> 00:01:02,250/);
    assert.match(p.run("transcriptExportText(exportSample,'vtt')"), /^WEBVTT\n\n1\n00:01:00\.000/);
    assert.match(p.run("transcriptExportText(exportSample,'txt')"), /部分字幕.*\n\n\[00:01:00\.000\] Corrected sentence/s);
});

test('Android sharing sends the complete document without format selection', () => {
    const p=player();let result;
    p.run("nowPlaying={title:'Episode',author:'Author'};cues=[{start:0,end:1,text:'Corrected sentence.'}];subtitlesComplete=true");
    p.run('window.PodcastAndroid={shareSubtitle:(title,text)=>{globalThis.sharedResult={title,text}}}');
    p.run('exportSubtitles()');
    result=p.run('sharedResult');
    assert.equal(result.title,'Episode');
    assert.match(result.text,/完整字幕/);
    assert.match(result.text,/Corrected sentence/);
    assert.doesNotMatch(result.text,/WEBVTT|-->/);
});

test('Android sharing blocks incomplete subtitles and enables only after completion', () => {
    const p=player();
    p.run("nowPlaying={title:'Episode'};cues=[{start:0,end:1,text:'First sentence.'}];subtitlesComplete=false;window.PodcastAndroid={shareSubtitle:()=>{globalThis.shared=true}}");
    p.run('updateAiAvailability();exportSubtitles()');
    assert.equal(p.elements.get('subtitleShareBtn').disabled,true);
    assert.equal(p.run('globalThis.shared'),undefined);
    p.run('subtitlesComplete=true;updateAiAvailability()');
    assert.equal(p.elements.get('subtitleShareBtn').disabled,false);
    p.run('shareSubtitleTranscript()');
    assert.equal(p.run('globalThis.shared'),true);
    p.run('subtitlesComplete=false;updateAiAvailability()');
    assert.equal(p.elements.get('subtitleShareBtn').disabled,true);
});

test('print snapshot escapes content and marks incomplete transcripts', () => {
    const p = player();
    p.run(`nowPlaying={title:'<script>bad</script>',author:'A & B',url:'https://example.com',description:'Description'};cues=[{start:0,end:1,text:'<img onerror=bad>'}];subtitlesComplete=false`);
    const html = p.run('buildPrintableTranscript(transcriptSnapshot())');
    assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
    assert.match(html,/&lt;img onerror=bad&gt;/);
    assert.match(html,/部分字幕/);
    assert.match(html,/@page\{size:A4/);
    assert.match(html,/A &amp; B/);
});

test('dictionary provider loads and is sent with settings', async () => {
    const p = player();
    p.run("applyProviderSettings({dictionary_provider:'gemini',aliyun_region:'beijing'})");
    assert.equal(p.elements.get('dictionaryProvider').value, 'gemini');
    p.elements.get('dictionaryProvider').value = 'qwen';
    await p.run('saveApiSettings({preventDefault(){}})');
    assert.equal(JSON.parse(p.fetchCalls[0][1].body).dictionary_provider, 'qwen');
});

test('wait for complete first chunk before playing; audio_ready never starts playback', () => {
    const p = player(); p.start(); const s = p.streams[0];
    s.emit({status: 'audio_ready', local_audio: '/cache/audio.mp3'});
    s.emit(cue(0, 20));
    assert.equal(p.audio.playCount, 0);
    s.emit({status: 'chunk_ready', until: 30});
    assert.equal(p.audio.playCount, 1);
});

test('pause at subtitle frontier and resume when next chunk arrives', () => {
    const p = player(); p.start(); const s = p.streams[0];
    s.emit(cue(0, 20)); s.emit({status: 'chunk_ready', until: 30});
    p.audio.currentTime = 30; p.audio.ontimeupdate();
    assert.equal(p.audio.paused, true);
    s.emit(cue(30, 55)); s.emit({status: 'chunk_ready', until: 60});
    assert.equal(p.audio.paused, false);
    assert.equal(p.audio.currentTime, 30);
});

test('a manual pause remains paused while later subtitles arrive', () => {
    const p = player(); p.start(); const s = p.streams[0];
    s.emit(cue(0, 20)); s.emit({status: 'chunk_ready', until: 30});
    p.run('togglePlay()');
    s.emit(cue(30, 55)); s.emit({status: 'chunk_ready', until: 60});
    assert.equal(p.audio.paused, true);
});

test('old episode stream cannot replace new episode subtitles or audio', () => {
    const p = player(); p.start(); const old = p.streams[0];
    p.start('https://example.com/two.mp3');
    assert.equal(old.closed, true);
    old.emit({status: 'audio_ready', local_audio: '/cache/old.mp3'});
    old.emit(cue(0, 20)); old.emit({status: 'chunk_ready', until: 30});
    assert.equal(p.audio.src, 'https://example.com/two.mp3');
    assert.equal(p.run('cues.length'), 0);
    assert.equal(p.audio.playCount, 0);
});

test('refresh bypasses cache and waits for current position coverage', () => {
    const p = player(); p.start(); const first = p.streams[0];
    first.emit(cue(0, 20)); first.emit({status: 'chunk_ready', until: 30});
    p.audio.currentTime = 40; p.run('triggerAiTranscribe()');
    const refresh = p.streams[1];
    assert.match(refresh.url, /force_refresh=true/);
    refresh.emit(cue(0, 20)); refresh.emit({status: 'chunk_ready', until: 30});
    assert.equal(p.audio.paused, true);
    refresh.emit(cue(30, 55)); refresh.emit({status: 'chunk_ready', until: 60});
    assert.equal(p.audio.paused, false);
    assert.equal(p.audio.currentTime, 40);
});

test('error before subtitles never starts audio and is retryable', () => {
    const p = player(); p.start();
    p.streams[0].emit({status: 'error', detail: 'No API key'});
    assert.equal(p.audio.playCount, 0);
    assert.equal(p.run('isTranscribing'), false);
    p.run('triggerAiTranscribe()');
    assert.equal(p.streams.length, 2);
});

test('streaming source is selected before subtitles; completing the cache never resets playback', () => {
    const p = player(); p.start(); const stream = p.streams[0];
    stream.emit({status: 'audio_source', audio_url: '/api/audio?url=episode'});
    assert.equal(p.audio.src, '/api/audio?url=episode');
    assert.equal(p.audio.playCount, 0);
    assert.equal(p.audio.preload, 'none');
    stream.emit(cue(0, 20)); stream.emit({status: 'chunk_ready', until: 30});
    p.audio.currentTime = 5;
    stream.emit({status: 'audio_ready', local_audio: '/cache/complete.mp3'});
    assert.equal(p.audio.src, '/api/audio?url=episode');
    assert.equal(p.audio.currentTime, 5);
    assert.equal(p.audio.paused, false);
});

test('word apostrophes are escaped and VTT cue settings are parsed', () => {
    const p = player();
    assert.match(p.run(`wordSpans("don't", 2)`), /don\\'t/);
    assert.equal(p.run(`parseVtt('WEBVTT\\n\\n00:00:01.000 --> 00:00:02.000 align:start\\nHello')[0].end`), 2);
});

test('disconnect reconnects from checkpoint without resetting audio or duplicating subtitles', () => {
    const p = player(); p.start(); const first = p.streams[0];
    first.emit(cue(0, 20)); first.emit({status:'chunk_ready', until:30});
    p.audio.currentTime = 30; p.audio.ontimeupdate();
    first.onerror();
    assert.equal(first.closed, true);
    p.retry(); const retry = p.streams[1];
    assert.match(retry.url, /force_refresh=false/);
    assert.equal(p.audio.currentTime, 30);
    assert.equal(p.run('cues.length'), 1);
    retry.emit({status:'resumed', until:30, cues:[{start:0,end:20,text:'Hello world'}]});
    retry.emit(cue(0, 20));
    assert.equal(p.run('cues.length'), 1);
    retry.emit(cue(30, 55)); retry.emit({status:'chunk_ready', until:60});
    assert.equal(p.audio.paused, false);
    assert.equal(p.audio.currentTime, 30);
});

test('retry preserves a manual pause and is cancelled when switching episodes', () => {
    const p = player(); p.start(); const first = p.streams[0];
    first.emit(cue(0,20)); first.emit({status:'chunk_ready',until:30});
    p.run('togglePlay()'); first.onerror(); p.retry();
    p.streams[1].emit({status:'resumed',until:30,cues:[{start:0,end:20,text:'Hello world'}]});
    assert.equal(p.audio.paused, true);
    p.streams[1].onerror();
    const pending = p.timers.find(t => !t.cleared);
    p.start('https://example.com/second.mp3');
    assert.equal(pending.cleared, true);
    pending.fn();
    assert.equal(p.streams.length, 3);
});

test('watchdog recovers silent connection and stops after five consecutive retries', () => {
    const p = player(); p.start(); p.advance(46000);
    assert.equal(p.streams[0].closed, true);
    p.retry();
    for (let i=0;i<4;i++) { p.streams.at(-1).onerror(); p.retry(); }
    p.streams.at(-1).onerror();
    assert.equal(p.streams.length, 6);
    assert.equal(p.timers.filter(t => !t.cleared).length, 0);
});

test('permanent configuration errors do not auto-retry', () => {
    const p = player(); p.start();
    p.streams[0].emit({status:'error',detail:'Missing key',retryable:false});
    assert.equal(p.timers.filter(t => !t.cleared).length, 0);
});

test('chat enables on the first cue and sends all current corrected subtitles, question and selected examples', async () => {
    const p = player(); p.start();
    await p.run('sendAiQuestion()');
    assert.equal(p.fetchCalls.length, 0);
    const stream = p.streams[0];
    stream.emit(cue(0,20));
    assert.equal(p.elements.get('aiSendBtn').disabled, false);
    assert.equal(p.elements.get('subtitleShareBtn').disabled, true);
    stream.emit({status:'cue',cue:{start:20,end:40,text:'Second example.'}});
    p.run("cues[0].text='Corrected first sentence.';selectedCues.add(1);document.getElementById('aiInput').value='Explain the selected example'");
    await p.run('sendAiQuestion()');
    const body = JSON.parse(p.fetchCalls[0][1].body);
    assert.equal(body.transcript_complete, false);
    assert.equal(body.full_transcript, 'Corrected first sentence. Second example.');
    assert.equal(body.question, 'Explain the selected example');
    assert.match(body.selected_text,/Second example\./);
    assert.equal(body.audio_url,'https://example.com/one.mp3');
    stream.emit({status:'chunk_ready',until:40});stream.emit({status:'done'});
    assert.equal(p.elements.get('subtitleShareBtn').disabled,false);
    p.start('https://example.com/next.mp3');
    assert.equal(p.elements.get('aiSendBtn').disabled, true);
});

test('single sentence retry preserves other cues, playback and manual pause', async () => {
    const p=player(); p.start(); const s=p.streams[0];
    s.emit(cue(0,20)); s.emit(cue(30,55)); s.emit({status:'chunk_ready',until:60});
    p.audio.currentTime=35; p.run('togglePlay()');
    await p.run('regenerateSentence({stopPropagation(){}},0)');
    assert.equal(p.run('cues.length'),2);
    assert.equal(p.run('cues[0].text'),'Corrected sentence.');
    assert.equal(p.run('cues[1].text'),'Hello world');
    assert.equal(p.audio.currentTime,35);
    assert.equal(p.audio.paused,true);
    const body=JSON.parse(p.fetchCalls[0][1].body);
    assert.equal(body.start,0);assert.equal(body.end,20);
});

test('late sentence retry cannot modify a newly selected episode', async () => {
    const p=player();p.start();p.streams[0].emit(cue(0,20));
    p.run("fetch=(url)=>url==='/api/cancel_transcription'?Promise.resolve({json:async()=>({status:'cancelled'})}):new Promise(resolve=>window.finishRetry=resolve)");
    const pending=p.run('regenerateSentence({stopPropagation(){}},0)');
    p.start('https://example.com/next.mp3');
    p.run("window.finishRetry({json:async()=>({status:'success',cue:{start:0,end:20,text:'Old episode'}})})");
    await pending;
    assert.equal(p.run('cues.length'),0);
});

test('Android recreation restores episode, position and manual pause from persistent state', () => {
    const storage=new Map();const previous=player(storage);previous.run('window.androidNativeRuntime=true');previous.start();
    previous.streams[0].emit(cue(0,20));previous.streams[0].emit({status:'chunk_ready',until:30});
    previous.audio.currentTime=17;previous.run('togglePlay();saveAndroidPlaybackState()');
    const restored=player(storage);restored.run('window.androidNativeRuntime=true;restoreAndroidPlaybackState()');
    assert.equal(restored.run('nowPlaying.url'),'https://example.com/one.mp3');
    assert.equal(restored.audio.currentTime,17);assert.equal(restored.run('wantsPlayback'),false);
    restored.streams[0].emit({status:'resumed',until:30,cues:[{start:0,end:20,text:'Hello world'}]});
    assert.equal(restored.audio.paused,true);
});

test('quick questions and selected-example-only questions work while transcription continues', async () => {
    const p=player();p.start();p.streams[0].emit(cue(0,20));
    p.run("quickAsk('语法拆解')");
    const quick=JSON.parse(p.fetchCalls[0][1].body);
    assert.equal(quick.transcript_complete,false);
    assert.equal(quick.full_transcript,'Hello world');
    assert.ok(quick.question.length>0);
    await new Promise(resolve=>setImmediate(resolve));
    p.run("selectedCues.add(0);document.getElementById('aiInput').value=''");
    await p.run('sendAiQuestion()');
    const selected=JSON.parse(p.fetchCalls[1][1].body);
    assert.equal(selected.question,'');
    assert.match(selected.selected_text,/Hello world/);
});

test('an in-flight question keeps its subtitle snapshot and prevents duplicate requests', async () => {
    const p=player();p.start();p.streams[0].emit(cue(0,20));
    p.run("document.getElementById('aiInput').value='Explain';fetch=(url,options)=>{globalThis.askSnapshot=JSON.parse(options.body);return new Promise(resolve=>globalThis.finishAsk=resolve)}");
    const pending=p.run('sendAiQuestion()');
    assert.equal(p.elements.get('aiSendBtn').disabled,true);
    p.streams[0].emit({status:'cue',cue:{start:20,end:40,text:'Arrived later.'}});
    await p.run('sendAiQuestion()');
    assert.equal(p.run('askSnapshot.full_transcript'),'Hello world');
    p.run("finishAsk({json:async()=>({status:'success',answer:'Answer'})})");
    await pending;
    assert.equal(p.elements.get('aiSendBtn').disabled,false);
    assert.equal(p.run('cues.length'),2);
});

test('Android restores through the audio proxy before resumed subtitles start playback', () => {
    const storage=new Map([['android_playback_state',JSON.stringify({episode:{url:'https://example.com/one.mp3'},position:17,playing:true,volume:1,speed:1})]]);
    const p=player(storage);p.run('window.androidNativeRuntime=true;restoreAndroidPlaybackState()');
    assert.equal(p.audio.src,'/api/audio?url=https%3A%2F%2Fexample.com%2Fone.mp3');
    assert.equal(p.audio.playCount,0);
    p.streams[0].emit({status:'audio_source',audio_url:p.audio.src});
    p.streams[0].emit({status:'resumed',until:30,cues:[{start:0,end:20,text:'Hello world'}]});
    assert.equal(p.audio.currentTime,17);
    assert.equal(p.audio.paused,false);
    assert.equal(p.audio.playCount,1);
});

test('Android official and cached subtitles play through the proxy without a later source event', () => {
    for(const status of ['official','cached']) {
        const p=player();p.run('window.PodcastAndroid={}');p.start();
        const source='/api/audio?url=https%3A%2F%2Fexample.com%2Fone.mp3';
        assert.equal(p.audio.src,source);
        p.streams[0].emit({status,vtt:'WEBVTT\n\n00:00:00.000 --> 00:00:20.000\nHello world\n'});
        assert.equal(p.audio.src,source);
        assert.equal(p.audio.paused,false);
    }
});

test('an unchanged audio source does not reset a pending automatic play', () => {
    const p=player();p.run('window.androidNativeRuntime=true');p.start();
    let resets=0;const source=p.audio.src;
    p.audio.getAttribute=()=>source;
    Object.defineProperty(p.audio,'src',{get:()=>source,set:()=>resets++});
    p.streams[0].emit({status:'audio_source',audio_url:source});
    assert.equal(resets,0);
});

test('Android foreground recovery resumes SSE without clearing cues or playback position', () => {
    const p=player();p.start();p.streams[0].emit(cue(0,20));p.streams[0].emit({status:'chunk_ready',until:30});
    p.audio.currentTime=12;p.run('togglePlay();resumeAndroidPlayback()');
    assert.equal(p.streams.length,2);assert.match(p.streams[1].url,/force_refresh=false/);
    assert.equal(p.run('cues.length'),1);assert.equal(p.audio.currentTime,12);assert.equal(p.audio.paused,true);
});

test('dismissed tutor welcome stays hidden after reopening', () => {
    const storage=new Map();const first=player(storage);first.run('dismissAiWelcome()');
    assert.equal(storage.get('ai_welcome_dismissed'),'1');
    const second=player(storage);assert.equal(second.elements.get('aiWelcomeCard').style.display,'none');
});

test('catalog failures show the server error instead of treating it as podcast data', async () => {
    const p=player();
    p.run("fetch=async()=>({ok:false,json:async()=>({message:'Secure connection failed'})})");
    await p.run('loadTopCharts()');
    assert.match(p.elements.get('plazaContent').innerHTML,/Secure connection failed/);
    assert.doesNotMatch(p.elements.get('plazaContent').innerHTML,/forEach/);
    await p.run("loadPodcast('1200361736')");
    assert.match(p.elements.get('plazaContent').innerHTML,/Secure connection failed/);
});

test('Android starts with the native proxy before subtitle events, including official subtitles', () => {
    const p = player();
    // The bridge exists before MainActivity sets androidNativeRuntime.
    p.run('window.PodcastAndroid = {}');
    const url = 'https://example.com/episode.mp3?token=a&part=2';
    p.start(url);
    assert.equal(p.audio.src, '/api/audio?url=' + encodeURIComponent(url));
    assert.equal(p.audio.playCount, 0);
    p.streams[0].emit({status:'official',vtt:'WEBVTT\n\n00:00:00.000 --> 00:00:20.000\nHello world\n',local_audio:'/cache/audio.mp3'});
    assert.equal(p.audio.src, '/api/audio?url=' + encodeURIComponent(url));
    assert.equal(p.audio.paused, false);
});


test('Android restored playback keeps its proxy and position when resumed arrives before audio_source', () => {
    const storage = new Map([['android_playback_state', JSON.stringify({
        episode:{url:'https://example.com/one.mp3'},position:17,playing:true,speed:.75,volume:.6
    })]]);
    const p = player(storage);
    let source, assignments = 0;
    // Browser media resets when its source is assigned, even if it is the same URL.
    Object.defineProperty(p.audio, 'src', {get(){return source;},set(value){source=value;assignments++;this.currentTime=0;this.paused=true;}});
    p.run('window.androidNativeRuntime=true;restoreAndroidPlaybackState()');
    assert.equal(source, '/api/audio?url=https%3A%2F%2Fexample.com%2Fone.mp3');
    const stream = p.streams[0];
    stream.emit({status:'resumed',until:30,cues:[{start:0,end:20,text:'Hello world'}]});
    assert.equal(p.audio.currentTime,17);
    assert.equal(p.audio.paused,false);
    stream.emit({status:'audio_source',audio_url:source});
    stream.emit({status:'audio_ready',local_audio:'/cache/audio.mp3'});
    assert.equal(assignments,1);
    assert.equal(p.audio.currentTime,17);
    assert.equal(p.audio.paused,false);
    assert.equal(p.audio.playbackRate,.75);
});


test('Android foreground recovery replaces a legacy direct URL at a nonzero position and preserves manual pause', () => {
    const p = player();
    p.run('window.androidNativeRuntime=true');p.start();
    p.streams[0].emit(cue(0,20));p.streams[0].emit({status:'chunk_ready',until:30});
    p.run('togglePlay()');
    p.audio.src='https://example.com/one.mp3';p.audio.currentTime=12;
    p.run('resumeAndroidPlayback()');
    assert.equal(p.audio.src,'/api/audio?url=https%3A%2F%2Fexample.com%2Fone.mp3');
    assert.equal(p.audio.currentTime,12);
    assert.equal(p.run('wantsPlayback'),false);
    p.streams[1].emit({status:'resumed',until:30,cues:[{start:0,end:20,text:'Hello world'}]});
    assert.equal(p.audio.paused,true);
    assert.equal(p.audio.currentTime,12);
});

const settleTranslation = async () => { for (let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve)); };
function translationPlayer() {
    const p=player();p.start();
    p.run("fetch=async(url,options)=>{globalThis.translationRequests??=[];translationRequests.push(JSON.parse(options.body));return {json:async()=>({status:'success',translations:JSON.parse(options.body).sentences.map(s=>({id:s.id,translation:'中文 '+s.text}))})}}");
    return p;
}

test('subtitle translation is opt-in and keeps all English subtitles and playback state', async () => {
    const p=translationPlayer();p.streams[0].emit(cue(0,20));p.streams[0].emit({status:'chunk_ready',until:30});
    assert.equal(p.run('globalThis.translationRequests'),undefined);
    p.audio.currentTime=8;p.run('togglePlay();toggleSubtitleTranslation()');await settleTranslation();
    assert.match(p.elements.get('subScroll').innerHTML,/Hello/);
    assert.match(p.elements.get('subScroll').innerHTML,/中文 Hello world/);
    assert.equal(p.audio.currentTime,8);assert.equal(p.audio.paused,true);
    assert.equal(p.run('translationRequests.length'),1);
    p.run('toggleSubtitleTranslation()');assert.doesNotMatch(p.elements.get('subScroll').innerHTML,/sub-translation/);
    p.run('toggleSubtitleTranslation()');await settleTranslation();assert.equal(p.run('translationRequests.length'),1);
});

test('streaming sentences and corrected text get translations without resending unchanged sentences', async () => {
    const p=translationPlayer();p.streams[0].emit(cue(0,20));p.streams[0].emit({status:'chunk_ready',until:30});
    p.run('toggleSubtitleTranslation()');await settleTranslation();
    p.streams[0].emit({status:'cue',cue:{start:20,end:40,text:'New sentence.'}});p.streams[0].emit({status:'chunk_ready',until:40});
    await settleTranslation();assert.equal(p.run('translationRequests[1].sentences.length'),1);
    assert.equal(p.run('translationRequests[1].sentences[0].text'),'New sentence.');
    p.run("cues[0].text='Corrected sentence.';renderSubs()");await settleTranslation();
    assert.equal(p.run('translationRequests[2].sentences[0].text'),'Corrected sentence.');
    assert.match(p.elements.get('subScroll').innerHTML,/中文 Corrected sentence/);
    assert.doesNotMatch(p.elements.get('subScroll').innerHTML,/中文 Hello world/);
});

test('translations are batched and HTML in translated text is escaped', async () => {
    const p=translationPlayer();
    p.run("cues=Array.from({length:45},(_,i)=>({start:i,end:i+1,text:'Sentence '+i}));renderSubs();fetch=async(url,options)=>{globalThis.translationRequests??=[];translationRequests.push(JSON.parse(options.body));return {json:async()=>({status:'success',translations:JSON.parse(options.body).sentences.map(s=>({id:s.id,translation:'<img onerror=bad>'}))})}};toggleSubtitleTranslation()");
    await settleTranslation();assert.equal(p.run('translationRequests.length'),3);
    assert.equal(p.run('translationRequests[0].sentences.length'),20);
    assert.equal(p.run('translationRequests[2].sentences.length'),5);
    assert.match(p.elements.get('subScroll').innerHTML,/&lt;img onerror=bad&gt;/);
    assert.doesNotMatch(p.elements.get('subScroll').innerHTML,/<img onerror=bad>/);
});

test('late translation responses are cancelled and cannot affect a different episode', async () => {
    const p=translationPlayer();p.streams[0].emit(cue(0,20));
    p.run("fetch=(url,options)=>{if(url!=='/api/translate_subtitles')return Promise.resolve({json:async()=>({})});globalThis.translationSignal=options.signal;return new Promise(resolve=>globalThis.finishTranslation=resolve)};toggleSubtitleTranslation()");
    p.start('https://example.com/next.mp3');assert.equal(p.run('translationSignal.aborted'),true);
    p.run("finishTranslation({json:async()=>({status:'success',translations:[{id:0,translation:'OLD'}]})})");
    await settleTranslation();assert.equal(p.run('subtitleTranslations.size'),0);
    assert.equal(p.run('subtitleTranslationEnabled'),false);
});

test('translation failure preserves English and retries only after the button is clicked', async () => {
    const p=translationPlayer();p.streams[0].emit(cue(0,20));
    p.run("fetch=async()=>({json:async()=>({status:'success',translations:[]})});toggleSubtitleTranslation()");
    await settleTranslation();assert.equal(p.run('subtitleTranslationFailed'),true);
    assert.equal(p.run('cues[0].text'),'Hello world');
    p.run("fetch=async(url,options)=>({json:async()=>({status:'success',translations:[{id:0,translation:'你好'}]})});toggleSubtitleTranslation()");
    await settleTranslation();assert.equal(p.run('subtitleTranslationFailed'),false);
    assert.match(p.elements.get('subScroll').innerHTML,/你好/);
});

test('refresh discards old translations and retains translation preference', async () => {
    const p=translationPlayer();p.streams[0].emit(cue(0,20));p.run('toggleSubtitleTranslation()');await settleTranslation();
    p.run('triggerAiTranscribe()');assert.equal(p.run('subtitleTranslations.size'),0);
    assert.equal(p.run('subtitleTranslationEnabled'),true);
    p.streams[1].emit({status:'cue',cue:{start:0,end:20,text:'New recognition.'}});p.streams[1].emit({status:'chunk_ready',until:30});
    await settleTranslation();assert.match(p.elements.get('subScroll').innerHTML,/中文 New recognition/);
});


test('translation model is loaded and saved independently of dictionary settings', async () => {
    const p=player();p.run("applyProviderSettings({translation_provider:'qwen',dictionary_provider:'gemini'})");
    assert.equal(p.elements.get('translationProvider').value,'qwen');
    p.elements.get('translationProvider').value='gemini';
    await p.run('saveApiSettings({preventDefault(){}})');
    const body=JSON.parse(p.fetchCalls[0][1].body);
    assert.equal(body.translation_provider,'gemini');assert.equal(body.dictionary_provider,'gemini');
});

test('changing the translation model invalidates existing translations', async () => {
    const p=translationPlayer();p.streams[0].emit(cue(0,20));p.run('toggleSubtitleTranslation()');await settleTranslation();
    const epoch=p.run('subtitleTranslationEpoch');p.run("applyProviderSettings({translation_provider:'qwen'})");
    await settleTranslation();assert.ok(p.run('subtitleTranslationEpoch')>epoch);
    assert.equal(p.run('subtitleTranslationProvider'),'qwen');assert.equal(p.run('translationRequests.length'),2);
});
