const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function player() {
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
    const context = vm.createContext({document, window: {}, EventSource, console, Date: TestDate,
        setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; },
        clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; },
        setInterval(fn, ms) { intervals.push({fn, ms}); return intervals.length; },
        clearInterval(id) { if (intervals[id - 1]) intervals[id - 1].cleared = true; },
        fetch: async (...args) => { fetchCalls.push(args); return {json: async () => ({status:'success', answer:'Answer'})}; },
        localStorage: {getItem() { return null; }}});
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

test('chat is blocked before complete subtitles, then sends the whole transcript', async () => {
    const p = player(); p.start();
    await p.run('sendAiQuestion()');
    assert.equal(p.fetchCalls.length, 0);
    const stream = p.streams[0];
    stream.emit(cue(0,20)); stream.emit({status:'chunk_ready',until:30});
    await p.run('sendAiQuestion()');
    assert.equal(p.fetchCalls.length, 0);
    stream.emit({status:'done'});
    assert.equal(p.elements.get('aiSendBtn').disabled, false);
    p.run("document.getElementById('aiInput').value = 'Summarize'");
    await p.run('sendAiQuestion()');
    const body = JSON.parse(p.fetchCalls[0][1].body);
    assert.equal(body.transcript_complete, true);
    assert.equal(body.full_transcript, 'Hello world');
    p.start('https://example.com/next.mp3');
    assert.equal(p.elements.get('aiSendBtn').disabled, true);
});
