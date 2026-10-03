"""Install on the attached Android tablet, provision local keys without putting them in the APK."""
import json
import subprocess
import sys
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'com.podcastlearner.tablet'
SERIAL = sys.argv[1] if len(sys.argv) > 1 else None

def adb(*arguments, **kwargs):
    return subprocess.run(['adb', *(['-s', SERIAL] if SERIAL else []), *arguments], check=True, **kwargs)

if __name__ == '__main__':
    # Older builds used a USB reverse tunnel on this port. Release it for the device-local server.
    subprocess.run(['adb', *(['-s', SERIAL] if SERIAL else []), 'reverse', '--remove', 'tcp:8557'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    adb('install', '-r', str(ROOT / 'android/app/build/outputs/apk/debug/app-debug.apk'))
    config = dotenv_values(ROOT / '.env')
    fields = {'groq_key_1': config.get('GROQ_API_KEY_1') or config.get('GROQ_API_KEY'),
              'groq_key_2': config.get('GROQ_API_KEY_2'), 'gemini_key': config.get('GEMINI_API_KEY')}
    payload = json.dumps({name: value for name, value in fields.items() if value}).encode()
    adb('shell', f'run-as {PACKAGE} mkdir -p files')
    adb('shell', f"run-as {PACKAGE} sh -c 'umask 077; cat > files/provision.json'", input=payload, stdout=subprocess.DEVNULL)
    adb('shell', 'am', 'force-stop', PACKAGE)
    adb('shell', 'am', 'start', '-n', PACKAGE + '/.MainActivity')
    print('Installed and launched; credentials will be encrypted and the import file removed.')
