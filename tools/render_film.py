#!/usr/bin/env python3
"""Render a disclosed educational edit, serve progress, then queue its verified Git backup.
Run: .venv/bin/python tools/render_film.py (requires imageio-ffmpeg, Pillow, av).
The temporary preview is intentionally public, like the explicitly authorized uploader.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen

import av
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'edit/plan.json').read_text())
WORK = ROOT / '.cache/film-render'
WORK.mkdir(parents=True, exist_ok=True)
OUTPUT = ROOT / PLAN['output']
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
FF = imageio_ffmpeg.get_ffmpeg_exe()
FONT_DIR = Path('/usr/share/fonts/truetype/dejavu')
GUARD = threading.Lock()
PROGRESS = {'phase': 'starting', 'percent': 0, 'message': 'Подготовка исходников',
            'film_ready': False, 'backup': 'pending', 'started_at': time.time()}
DURATIONS = [s['duration'] if s['type'] == 'card' else s['end'] - s['start'] for s in PLAN['segments']]
TOTAL = sum(DURATIONS)
CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def update(**values):
    with GUARD:
        PROGRESS.update(values)
        tmp = WORK / 'status.tmp'
        tmp.write_text(json.dumps(PROGRESS, ensure_ascii=False, indent=2))
        os.replace(tmp, WORK / 'status.json')


def run(args, log_name):
    p = subprocess.run([FF, '-hide_banner', '-nostdin', '-nostats', *args],
                       cwd=ROOT, capture_output=True, text=True)
    (WORK / log_name).write_text(p.stderr)
    if p.returncode:
        raise RuntimeError(p.stderr[-2500:])
    return p.stderr


def encode(args, dest, base, duration):
    log = dest.with_suffix('.log')
    with log.open('w') as stderr:
        p = subprocess.Popen([FF, '-hide_banner', '-nostdin', '-nostats', '-progress', 'pipe:1',
                              '-threads', '2', '-filter_threads', '2', *args, '-y', str(dest)],
                             cwd=ROOT, stdout=subprocess.PIPE, stderr=stderr, text=True)
        for line in p.stdout:
            if line.startswith('out_time_us='):
                try:
                    seconds = max(0, min(duration, int(line.strip().split('=')[1]) / 1e6))
                    update(percent=round(85 * (base + seconds) / TOTAL, 1))
                except ValueError:
                    pass
        code = p.wait()
    if code:
        raise RuntimeError(log.read_text()[-2500:])


def encoder_settings():
    v = PLAN['video']
    return ['-c:v', 'libx264', '-preset', v['preset'], '-crf', str(v['crf']), '-threads', '2',
            '-pix_fmt', 'yuv420p', '-r', '30', '-video_track_timescale', '30000',
            '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
            '-map_metadata', '-1', '-map_chapters', '-1', '-movflags', '+faststart']


def title_image(segment, path):
    image = Image.new('RGB', (1920, 1080), '#101c28')
    draw = ImageDraw.Draw(image)
    serif = 'DejaVuSerif.ttf'
    size = 64 if segment.get('small_heading') else 100
    font = ImageFont.truetype(str(FONT_DIR / serif), size)
    sub = ImageFont.truetype(str(FONT_DIR / 'DejaVuSans.ttf'), 32)
    label = ImageFont.truetype(str(FONT_DIR / 'DejaVuSans.ttf'), 24)
    lines = segment['heading']
    while any(draw.textbbox((0, 0), text, font=font)[2] > 1690 for text in lines):
        size -= 2
        font = ImageFont.truetype(str(FONT_DIR / serif), size)
    line_height = size * 1.28
    y = 470 - (len(lines) - 1) * line_height / 2
    draw.line((850, 275, 1070, 275), fill='#c8af7c', width=3)
    draw.text((960, 320), segment['footer'], font=label, fill='#c8af7c', anchor='mm')
    for text in lines:
        draw.text((960, y), text, font=font, fill='#f2efe9', anchor='mm')
        y += line_height
    draw.text((960, 730), segment['subtitle'], font=sub, fill='#adbaca', anchor='mm')
    image.save(path)


def manifest_for(name):
    for path in (ROOT / 'videos').glob('*.json'):
        m = json.loads(path.read_text())
        if m['original_name'] == name:
            return m
    raise FileNotFoundError(name)


def source_path(m):
    path = ROOT / '.cache/video-upload' / (m['upload_id'] + '.part')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix('.restoring')
        with tmp.open('wb') as out:
            for part in m['parts']:
                data = (ROOT / part['path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != part['sha256']:
                    raise ValueError('Git part checksum failed')
                out.write(data)
        os.replace(tmp, path)
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            h.update(block)
    if h.hexdigest() != m['sha256'] or path.stat().st_size != m['size']:
        raise ValueError('Source checksum failed: ' + m['original_name'])
    return path


def audio_filter(segment, src):
    key_hash = hashlib.sha256(json.dumps([segment, str(src)], sort_keys=True).encode()).hexdigest()[:16]
    key = WORK / (segment['id'] + '-' + key_hash + '-loudness.json')
    if key.exists():
        measurement = json.loads(key.read_text())
    else:
        log = run(['-ss', str(segment['start']), '-i', str(src), '-t', str(segment['end'] - segment['start']),
                   '-map', '0:a:0', '-vn', '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json',
                   '-f', 'null', '-'], segment['id'] + '-loudness.log')
        match = re.search(r'\{\s*"input_i".*?\}', log, re.S)
        if not match:
            raise RuntimeError('No loudness measurement')
        measurement = json.loads(match.group())
        key.write_text(json.dumps(measurement, indent=2))
    numeric_keys = ('input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset')
    if not all(math.isfinite(float(measurement[k])) for k in numeric_keys):
        raise ValueError('Non-finite audio loudness measurement')
    filt = ('loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:' +
            ':'.join(a + '=' + measurement[b] for a, b in [
                ('measured_I', 'input_i'), ('measured_TP', 'input_tp'),
                ('measured_LRA', 'input_lra'), ('measured_thresh', 'input_thresh'), ('offset', 'target_offset')]))
    duration = segment['end'] - segment['start']
    return filt + f',afade=t=in:d=0.015,afade=t=out:st={duration - 0.015}:d=0.015,aresample=48000'


def api(path, body=None, raw=False):
    headers = {}
    if body is not None:
        headers['Content-Type'] = 'application/octet-stream' if raw else 'application/json'
        if not raw:
            body = json.dumps(body).encode()
    with urlopen(Request('http://127.0.0.1:8080' + path, data=body, headers=headers), timeout=180) as r:
        return json.load(r)


def publish_backup(digest):
    fid = hashlib.sha256(('educational-export-v1:' + digest).encode()).hexdigest()
    metadata = {'id': fid, 'name': OUTPUT.name, 'size': OUTPUT.stat().st_size, 'public_backup_confirmed': True}
    update(backup='uploading', backup_id=fid)
    failures = 0
    while True:
        try:
            current = api('/api/init', metadata)
            with OUTPUT.open('rb') as stream:
                stream.seek(current['offset'])
                while current['offset'] < metadata['size']:
                    block = stream.read(4 * 1024**2)
                    checksum = hashlib.sha256(block).hexdigest()
                    current = api(f'/api/chunk?id={fid}&offset={current["offset"]}&sha256={checksum}', block, True)
            api('/api/finish', {'id': fid})
            break
        except Exception:
            failures += 1
            if failures >= 5:
                raise
            time.sleep(3)
    while True:
        current = next(f for f in api('/api/files')['files'] if f['id'] == fid)
        update(backup=current['status'], backup_error=current.get('error', ''))
        if current['status'] == 'saved':
            update(backup_commit=current.get('commit'))
            return
        if current['status'] == 'error':
            return
        time.sleep(5)


def render():
    try:
        sources = {}
        for s in PLAN['segments']:
            if s['type'] == 'source' and s['source'] not in sources:
                sources[s['source']] = source_path(manifest_for(s['source']))
        done, parts, ledger = 0.0, [], []
        for index, (segment, duration) in enumerate(zip(PLAN['segments'], DURATIONS), 1):
            label = segment.get('source') or segment['id']
            update(phase='rendering', message=f'Фрагмент {index}/{len(DURATIONS)} · {label}',
                   segment=index, segments=len(DURATIONS))
            print('RENDER', index, label, duration, flush=True)
            dest = WORK / (str(index).zfill(2) + '-' + segment['id'] + '.mp4')
            signature = hashlib.sha256(json.dumps([segment, PLAN['video'], PLAN['audio'], CODE_HASH], sort_keys=True).encode()).hexdigest()
            checkpoint = dest.with_suffix('.done')
            if not (dest.exists() and checkpoint.exists() and checkpoint.read_text() == signature):
                if segment['type'] == 'card':
                    png = dest.with_suffix('.png')
                    title_image(segment, png)
                    args = ['-loop', '1', '-framerate', '30', '-i', str(png), '-f', 'lavfi', '-i',
                            'anullsrc=channel_layout=stereo:sample_rate=48000', '-t', str(duration),
                            '-vf', 'format=yuv420p,setsar=1', '-map', '0:v:0', '-map', '1:a:0']
                else:
                    src = sources[segment['source']]
                    # Frame-rate conversion precedes expensive pointwise HDR conversion.
                    vf = ('fps=30,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
                          'tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv:d=error_diffusion,'
                          'format=yuv420p,setsar=1,sidedata=delete')
                    args = ['-ss', str(segment['start']), '-i', str(src), '-t', str(duration),
                            '-map', '0:v:0', '-map', '0:a:0', '-vf', vf, '-af', audio_filter(segment, src)]
                encode(args + encoder_settings(), dest, done, duration)
                checkpoint.write_text(signature)
            parts.append(dest)
            ledger.append({**segment, 'timeline_start': round(done, 6), 'timeline_end': round(done + duration, 6)})
            done += duration
        (WORK / 'edit-ledger.json').write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
        update(phase='joining', percent=86, message='Сборка единого MP4')
        concat = WORK / 'concat.txt'
        concat.write_text('\n'.join("file '" + str(p).replace("'", "'\\''") + "'" for p in parts))
        run(['-f', 'concat', '-safe', '0', '-i', str(concat), '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy',
             '-map_metadata', '-1', '-map_chapters', '-1', '-metadata', 'title=Проверка показаний на месте — учебная монтажная версия',
             '-metadata', 'comment=Edited educational dramatization; not a continuous evidentiary recording.',
             '-movflags', '+faststart', '-y', str(OUTPUT)], 'join.log')
        update(phase='checking', percent=89, message='Полная проверка итогового видео и звука')
        run(['-v', 'error', '-xerror', '-threads', '2', '-i', str(OUTPUT), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'], 'final-decode.log')
        with av.open(str(OUTPUT)) as c:
            v = c.streams.video[0]
            if v.width != 1920 or v.height != 1080 or v.codec_context.name != 'h264':
                raise ValueError('Unexpected export video format')
            if abs(c.duration / av.time_base - TOTAL) > 0.5:
                raise ValueError('Unexpected export duration')
            if any('location' in k.lower() for k in c.metadata):
                raise ValueError('Original location metadata leaked')
            details = {'duration': c.duration / av.time_base, 'width': v.width, 'height': v.height,
                       'video_codec': v.codec_context.name, 'metadata': dict(c.metadata)}
        digest = hashlib.sha256()
        with OUTPUT.open('rb') as stream:
            for block in iter(lambda: stream.read(4 * 1024**2), b''):
                digest.update(block)
        details.update(sha256=digest.hexdigest(), size=OUTPUT.stat().st_size, full_decode_ok=True)
        (WORK / 'export-result.json').write_text(json.dumps(details, ensure_ascii=False, indent=2))
        update(phase='ready', percent=100, message='Видео готово. Отдельно выполняется сохранение в GitHub.',
               film_ready=True, **details)
        print('FILM READY', OUTPUT, flush=True)
        try:
            publish_backup(details['sha256'])
            print('BACKUP', PROGRESS.get('backup'), flush=True)
        except Exception as exc:
            update(backup='error', backup_error=str(exc))
            print('BACKUP ERROR', str(exc), flush=True)
    except Exception as exc:
        update(phase='error', message=str(exc))
        print('RENDER ERROR', str(exc), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def response(self, body, kind):
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == '/':
            self.response((ROOT / 'edit/progress.html').read_bytes(), 'text/html; charset=utf-8')
        elif route == '/status':
            with GUARD:
                body = json.dumps(PROGRESS, ensure_ascii=False).encode()
            self.response(body, 'application/json; charset=utf-8')
        elif route == '/edit-plan.json':
            self.response(json.dumps(PLAN, ensure_ascii=False, indent=2).encode(), 'application/json; charset=utf-8')
        elif route == '/export-result.json' and PROGRESS['film_ready']:
            self.response((WORK / 'export-result.json').read_bytes(), 'application/json; charset=utf-8')
        elif route == '/film.mp4' and PROGRESS['film_ready']:
            self.video()
        else:
            self.send_error(404)

    def video(self):
        size = OUTPUT.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get('Range')
        if header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
            if not match or not any(match.groups()):
                self.send_error(416)
                return
            a, b = match.groups()
            if not a:
                start = max(0, size - int(b))
            else:
                start = int(a)
                end = min(size - 1, int(b)) if b else size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        self.send_response(206 if header else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if header:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if parse_qs(urlsplit(self.path).query).get('download'):
            self.send_header('Content-Disposition', 'attachment; filename="ppnm_1080p_educational.mp4"')
        self.end_headers()
        try:
            with OUTPUT.open('rb') as stream:
                stream.seek(start)
                left = end - start + 1
                while left:
                    block = stream.read(min(left, 1024**2))
                    if not block:
                        break
                    self.wfile.write(block)
                    left -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == '__main__':
    threading.Thread(target=render, daemon=True).start()
    print('Educational film preview: port ' + os.environ.get('FILM_PORT', '8081'), flush=True)
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('FILM_PORT', '8081'))), Handler).serve_forever()
