"""Explicit connected-device check using a local silent WAV; no cloud calls or settings changes.

Usage: python tests/android_playback_smoke.py DEVICE_SERIAL
Requires a debug build, ADB, and websocket-client. Launches/recreates/stops the app.
"""
import base64
import io
import subprocess
import sys
import time
import wave
import json
import urllib.request
import websocket

PACKAGE = 'com.podcastlearner.tablet'


def main(serial):
    def adb(*args):
        return subprocess.check_output(['adb', '-s', serial, *args], text=True).strip()

    def launch():
        adb('shell', 'am', 'start', '-n', PACKAGE + '/.MainActivity')

    def connect():
        for _ in range(30):
            pid = subprocess.run(['adb','-s',serial,'shell','pidof',PACKAGE],text=True,capture_output=True).stdout.strip()
            if pid:
                adb('forward', 'tcp:9224', 'localabstract:webview_devtools_remote_' + pid)
                try:
                    pages=json.load(urllib.request.urlopen('http://127.0.0.1:9224/json'))
                    socket=websocket.create_connection(pages[0]['webSocketDebuggerUrl'],timeout=10,suppress_origin=True)
                    if evaluate(socket,"typeof wantsPlayback !== 'undefined' && window.androidNativeRuntime"):
                        return socket
                    socket.close()
                except Exception:
                    pass
            time.sleep(.5)
        raise RuntimeError('Debug WebView unavailable: ' + serial)

    def evaluate(socket,expression):
        socket.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True,'awaitPromise':True}}))
        while True:
            result=json.loads(socket.recv())
            if result.get('id')==1:
                assert 'exceptionDetails' not in result.get('result',{}), result
                return result['result']['result'].get('value')

    data = io.BytesIO()
    with wave.open(data, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\0\0' * 8000 * 60)
    source = 'data:audio/wav;base64,' + base64.b64encode(data.getvalue()).decode()
    launch()
    socket=connect()
    assert evaluate(socket,'nowPlaying===null && audio.paused'), 'Expected cold home before smoke test'
    evaluate(socket,"window.playbackSmokeMarker='retained';subtitlesComplete=true;wantsPlayback=true;playbackEnded=false;audio.src="+json.dumps(source)+";audio.play()")
    time.sleep(2)
    before=evaluate(socket,'audio.currentTime')
    assert before>1
    adb('shell','input','keyevent','KEYCODE_HOME')
    time.sleep(5)
    assert evaluate(socket,'!audio.paused && audio.currentTime')>before+2, 'Background playback stopped'
    # CLEAR_TOP without SINGLE_TOP destroys the current Activity and creates a replacement.
    adb('shell','am','start','-f','0x14000000','-n',PACKAGE+'/.MainActivity')
    time.sleep(3)
    assert evaluate(socket,"window.playbackSmokeMarker==='retained' && !audio.paused && audio.currentTime")>before+5, 'Activity recreation lost player'
    evaluate(socket,'wantsPlayback=false;audio.pause();audio.removeAttribute("src");audio.load()')
    socket.close()
    adb('shell','am','force-stop',PACKAGE)
    launch()
    socket=connect()
    assert evaluate(socket,"nowPlaying===null && audio.paused && window.playbackSmokeMarker===undefined"), 'Cold launch restored player'
    socket.close()
    adb('forward', '--remove', 'tcp:9224')
    print(serial + ': PASS home/background audio, Activity recreation retains player, cold launch stays home')


if __name__ == '__main__':
    main(sys.argv[1])
