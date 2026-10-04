"""Sentence overrides survive background transcription, reconnects and cached playback."""
import json
import math
from pathlib import Path


def path_for(cache_dir, audio_url):
    from transcription import cache_paths
    return Path(cache_dir) / (cache_paths(cache_dir, audio_url)[0] + '-aliyun-corrections.json')


def identity(cue):
    return (round(cue.get('source_start', cue['start']), 3), round(cue.get('source_end', cue['end']), 3))


def load(path):
    try:
        records = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(records, list): return []
        return [c for c in records if isinstance(c, dict) and isinstance(c.get('text'), str)
                and all(isinstance(c.get(k), (float, int)) and math.isfinite(c[k]) for k in ('start', 'end', 'source_start', 'source_end'))
                and 0 <= c['source_start'] <= c['start'] < c['end'] <= c['source_end']]
    except (OSError, ValueError, TypeError):
        return []


def apply(cues, records):
    replacements = {identity(c): c for c in records}
    return [dict(replacements.get(identity(c), c)) for c in cues]


def apply_vtt(content, records):
    import re
    from transcription import to_vtt
    def seconds(value):
        parts = value.split(':')
        return sum(float(part) * 60**index for index, part in enumerate(reversed(parts)))
    cues = []
    for block in re.split(r'\r?\n\s*\r?\n', content.strip()):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r'(\d{2,}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s+-->\s+(\d{2,}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})', line)
            if match:
                cues.append({'start': seconds(match[1]), 'end': seconds(match[2]), 'text': '\n'.join(lines[index+1:])})
                break
    return to_vtt(apply(cues, records)) if cues and records else content


def save(path, cue):
    from transcription import atomic_write
    records = [c for c in load(path) if identity(c) != identity(cue)]
    records.append(cue)
    atomic_write(path, json.dumps(records, ensure_ascii=False))
