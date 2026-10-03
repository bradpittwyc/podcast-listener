const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function player() {
    const elements = new Map();
    const element = () => ({style: {}, classList: {add() {}, remove() {}, toggle() {}}, scrollIntoView() {}, innerHTML: '', textContent: ''});
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
    const context = vm.createContext({document, window: {}, EventSource, console, setTimeout() {}, localStorage: {getItem() { return null; }}});
    const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
    vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
    vm.runInContext('toast = () => {}', context);
    const run = (code) => vm.runInContext(code, context);
    const start = (url = 'https://example.com/one.mp3') => run(`startPlay(${JSON.stringify(url)}, 'Episode', 'Show', '', '', '')`);
    return {audio, streams, run, start};
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
