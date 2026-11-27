#!/usr/bin/env python3
"""Resumable uploads with SHA-256 chunks and serialized, split-file GitHub backups."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
STORE = ROOT / '.cache' / 'video-upload'
STORE.mkdir(parents=True, exist_ok=True)
os.chmod(STORE, 0o700)
STATE = STORE / 'state.json'
CHUNK = 4 * 1024 * 1024
MAX_FILE = 2 * 1024**3 - 1
# Explicit opt-in for this temporary, public-media upload session.
OPEN_ACCESS = os.environ.get('UPLOAD_OPEN_ACCESS') == '1'
OPEN_MAX_FILES = 16
OPEN_MAX_BYTES = 5 * 1024**3
PART_SIZE = 48 * 1024**2
BATCH_SIZE = 4
LOCK = threading.RLock()
FILE_LOCKS = {}
JOBS = queue.Queue()
PENDING = set()


def command(*args, timeout=120):
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or 'Command failed')[-1800:])
    return p.stdout.strip()


BRANCH = command('git', 'branch', '--show-current')
REPO = command('gh', 'repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner')
if STATE.exists():
    state = json.loads(STATE.read_text())
    if state['branch'] != BRANCH or state['repo'] != REPO:
        raise RuntimeError('Upload state belongs to another branch or repository')
else:
    state = {'token': secrets.token_urlsafe(32), 'branch': BRANCH, 'repo': REPO,
             'files': {}}
TOKEN = state['token']


def save():
    tmp = STATE.with_suffix('.tmp')
    with tmp.open('w') as stream:
        stream.write(json.dumps(state, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, STATE)
    fd = os.open(STORE, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def update(fid, **fields):
    with LOCK:
        state['files'][fid].update(fields)
        save()


def file_lock(fid):
    with LOCK:
        return FILE_LOCKS.setdefault(fid, threading.Lock())


def public_file(f):
    return {k: v for k, v in f.items() if k not in ('path',)}


def enqueue(fid):
    with LOCK:
        if fid in PENDING:
            return
        PENDING.add(fid)
        JOBS.put(fid)


def push_paths(paths, message):
    command('git', 'add', '--', *paths)
    # --only prevents unrelated staged changes from entering automatic commits.
    if command('git', 'diff', '--cached', '--name-only', '--', *paths):
        command('git', '-c', 'gc.auto=0', 'commit', '--only', '-m', message, '--', *paths,
                timeout=600)
    command('git', 'push', 'origin', BRANCH, timeout=1800)
    head = command('git', 'rev-parse', 'HEAD')
    remote = command('git', 'ls-remote', 'origin', 'refs/heads/' + BRANCH)
    if not remote or remote.split()[0] != head:
        raise RuntimeError('Remote branch verification failed; retry backup')
    return head


def backup(fid):
    with LOCK:
        f = dict(state['files'][fid])
    if not f.get('public_backup_confirmed'):
        raise RuntimeError('Explicit consent to public Git backup is required')
    if command('git', 'branch', '--show-current') != BRANCH:
        raise RuntimeError('Current branch changed; refusing to commit or push')
    update(fid, status='backing_up', error='')
    path = STORE / (fid + '.part')
    parts, batch, digest = [], [], hashlib.sha256()
    directory = ROOT / 'media' / fid
    directory.mkdir(parents=True, exist_ok=True)
    with path.open('rb') as stream:
        index = 0
        while True:
            block = stream.read(PART_SIZE)
            if not block:
                break
            digest.update(block)
            part_hash = hashlib.sha256(block).hexdigest()
            relative = 'media/' + fid + '/part-' + str(index).zfill(5)
            dest = ROOT / relative
            if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != part_hash:
                tmp = dest.with_suffix('.tmp')
                with tmp.open('wb') as out:
                    out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, dest)
            parts.append({'path': relative, 'size': len(block), 'sha256': part_hash})
            batch.append(relative)
            index += 1
            if len(batch) == BATCH_SIZE:
                push_paths(batch, 'Back up video parts: ' + f['name'])
                update(fid, parts_pushed=index)
                batch = []
    if batch:
        push_paths(batch, 'Back up video parts: ' + f['name'])
        update(fid, parts_pushed=len(parts))
    full_hash = digest.hexdigest()
    if sum(p['size'] for p in parts) != f['size']:
        raise RuntimeError('Local file size mismatch')
    update(fid, status='committing', sha256=full_hash)
    manifest = {'original_name': f['name'], 'size': f['size'], 'sha256': full_hash,
                'parts': parts, 'repository': REPO, 'upload_id': fid,
                'public_backup_confirmed': True}
    relative = 'videos/' + fid + '.json'
    dest = ROOT / relative
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    commit = push_paths([relative], 'Complete video backup: ' + f['name'])
    update(fid, status='saved', error='', commit=commit,
           saved_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


def worker():
    while True:
        fid = JOBS.get()
        try:
            backup(fid)
        except Exception as exc:
            with LOCK:
                PENDING.discard(fid)
                update(fid, status='error', error=str(exc)[-2000:])
        else:
            with LOCK:
                PENDING.discard(fid)
        finally:
            JOBS.task_done()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Do not log tokens, request bodies or filenames.
        pass

    def reply(self, code, value, content_type='application/json; charset=utf-8'):
        body = json.dumps(value, ensure_ascii=False).encode() if not isinstance(value, bytes) else value
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if OPEN_ACCESS:
            return True
        supplied = self.headers.get('X-Upload-Token', '')
        valid = hmac.compare_digest(supplied, TOKEN) if supplied else hmac.compare_digest(
            self.headers.get('Authorization', ''), 'Bearer ' + TOKEN)
        if not valid:
            self.close_connection = True
            self.reply(401, {'error': 'Нужна ссылка с ключом доступа.'})
            return False
        return True

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == '/':
            page = (ROOT / 'upload.html').read_text().replace(
                'const openAccess = false; // SERVER_OPEN_ACCESS',
                'const openAccess = ' + str(OPEN_ACCESS).lower() + ';')
            self.reply(200, page.encode(), 'text/html; charset=utf-8')
        elif route == '/api/files' and self.authorized():
            with LOCK:
                self.reply(200, {'files': [public_file(f) for f in state['files'].values()],
                                 'chunk': CHUNK, 'repo': REPO, 'branch': BRANCH,
                                 'free': shutil.disk_usage(STORE).free})
        elif route != '/api/files':
            self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            self.socket_timeout()
            length = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= length <= CHUNK:
                self.close_connection = True
                self.reply(413, {'error': 'Максимальный размер части — 4 МиБ.'})
                return
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError('Неполная часть. Повторите отправку.')
            parsed = urlsplit(self.path)
            if parsed.path == '/api/import-export':
                self.import_export(json.loads(data))
            elif parsed.path == '/api/init':
                self.init_upload(json.loads(data))
            elif parsed.path == '/api/chunk':
                q = parse_qs(parsed.query)
                self.chunk(q.get('id', [''])[0], int(q.get('offset', ['-1'])[0]),
                           q.get('sha256', [''])[0], data)
            elif parsed.path in ('/api/finish', '/api/retry'):
                fid = json.loads(data)['id']
                with file_lock(fid), LOCK:
                    f = state['files'][fid]
                    if f['offset'] != f['size']:
                        raise ValueError('Файл ещё не принят полностью.')
                    if f['status'] not in ('saved', 'backing_up', 'committing'):
                        f.update(status='queued', error='')
                        save()
                        enqueue(fid)
                    self.reply(200, public_file(f))
            else:
                self.reply(404, {'error': 'Not found'})
        except (ValueError, KeyError, TypeError) as exc:
            self.reply(400, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            self.reply(500, {'error': str(exc)})

    def socket_timeout(self):
        self.connection.settimeout(180)

    def init_upload(self, obj):
        if obj.get('public_backup_confirmed') is not True:
            raise ValueError('Подтвердите согласие на публикацию видео в GitHub.')
        fid, name, size = obj['id'], obj['name'], obj['size']
        if not isinstance(fid, str) or not re.fullmatch('[a-f0-9]{64}', fid):
            raise ValueError('Неверный идентификатор.')
        if not isinstance(name, str) or not name or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise ValueError('Недопустимое имя файла.')
        if type(size) is not int or not 0 < size <= MAX_FILE:
            raise ValueError('Каждый файл должен быть меньше 2 ГиБ.')
        with file_lock(fid), LOCK:
            f = state['files'].get(fid)
            if f:
                if f['size'] != size or f['name'] != name:
                    raise ValueError('Идентификатор уже занят другим файлом.')
            else:
                if OPEN_ACCESS and (len(state['files']) >= OPEN_MAX_FILES or
                        sum(x['size'] for x in state['files'].values()) + size > OPEN_MAX_BYTES):
                    raise ValueError('Лимит открытой загрузки: 16 файлов и 5 ГиБ суммарно.')
                # Reserve space for unfinished uploads already admitted.
                reserved = sum(x['size'] - x['offset'] for x in state['files'].values())
                if shutil.disk_usage(STORE).free - 3 * reserved < 3 * size + 256 * 1024**2:
                    raise ValueError('Недостаточно места на сервере.')
                f = {'id': fid, 'name': name, 'size': size, 'offset': 0, 'status': 'receiving', 'error': '',
                     'public_backup_confirmed': True}
                (STORE / (fid + '.part')).touch(exist_ok=False)
                state['files'][fid] = f
                save()
            self.reply(200, public_file(f))

    def import_export(self, obj):
        # Never available to anonymous browser clients, even in open-upload mode.
        if not hmac.compare_digest(self.headers.get('X-Internal-Import-Key', ''), TOKEN):
            self.reply(403, {'error': 'Internal export import requires its own key.'})
            return
        name, digest, size = obj.get('name'), obj.get('sha256'), obj.get('size')
        if obj.get('public_backup_confirmed') is not True:
            raise ValueError('Explicit public backup consent required')
        if not isinstance(name, str) or not re.fullmatch(r'ppnm_[A-Za-z0-9_-]{1,100}\.mp4', name):
            raise ValueError('Invalid export filename')
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('Invalid export checksum')
        if type(size) is not int or not 0 < size <= MAX_FILE:
            raise ValueError('Invalid export size')
        directory = ROOT / '.cache/exports'
        src = directory / name
        if src.is_symlink() or src.resolve().parent != directory.resolve() or not src.is_file():
            raise ValueError('Export must be a regular file inside .cache/exports')
        hasher = hashlib.sha256()
        with src.open('rb') as stream:
            for block in iter(lambda: stream.read(CHUNK), b''):
                hasher.update(block)
        if src.stat().st_size != size or hasher.hexdigest() != digest:
            raise ValueError('Export checksum or size mismatch')
        fid = hashlib.sha256(('local-export:' + digest + ':' + name).encode()).hexdigest()
        with file_lock(fid), LOCK:
            f = state['files'].get(fid)
            if f is None:
                if OPEN_ACCESS and (len(state['files']) >= OPEN_MAX_FILES or
                        sum(x['size'] for x in state['files'].values()) + size > OPEN_MAX_BYTES):
                    raise ValueError('Open upload quota exceeded')
                reserved = sum(x['size'] - x['offset'] for x in state['files'].values())
                if shutil.disk_usage(STORE).free - 3 * reserved < 2 * size + 256 * 1024**2:
                    raise ValueError('Not enough disk space for Git backup')
                dest = STORE / (fid + '.part')
                if dest.exists():
                    if not os.path.samefile(src, dest):
                        raise ValueError('Conflicting local export data')
                else:
                    os.link(src, dest)
                f = {'id': fid, 'name': name, 'size': size, 'offset': size, 'status': 'queued',
                     'error': '', 'sha256': digest, 'public_backup_confirmed': True,
                     'import_method': 'verified_local_hardlink'}
                state['files'][fid] = f
                save()
            if f['offset'] != size or f['name'] != name or f.get('sha256') != digest:
                raise ValueError('Conflicting export state')
            if f['status'] not in ('saved', 'backing_up', 'committing'):
                f.update(status='queued', error='')
                save()
                enqueue(fid)
            self.reply(200, public_file(f))

    def chunk(self, fid, offset, digest, data):
        if not data or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
            raise ValueError('Контрольная сумма части не совпала.')
        with file_lock(fid):
            with LOCK:
                f = state['files'][fid]
                if offset != f['offset']:
                    self.reply(409, {'error': 'Сверьте позицию и продолжите.', 'offset': f['offset']})
                    return
                if f['status'] != 'receiving' or offset + len(data) > f['size']:
                    raise ValueError('Неверный размер или состояние файла.')
            path = STORE / (fid + '.part')
            with path.open('r+b') as stream:
                stream.seek(offset)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            update(fid, offset=offset + len(data))
            self.reply(200, {'offset': offset + len(data)})


if __name__ == '__main__':
    with LOCK:
        # Bytes beyond the last acknowledged offset may come from an interrupted request.
        for fid, f in state['files'].items():
            path = STORE / (fid + '.part')
            if not path.exists() or path.stat().st_size < f['offset']:
                raise RuntimeError('Local upload data missing: ' + fid)
            with path.open('r+b') as stream:
                stream.truncate(f['offset'])
            if f['status'] in ('queued', 'backing_up', 'committing'):
                f['status'] = 'queued'
                enqueue(fid)
        save()
    threading.Thread(target=worker, daemon=True).start()
    print('Upload URL: ' + ('/ (OPEN ACCESS: max 16 files / 5 GiB)' if OPEN_ACCESS else '/#' + TOKEN), flush=True)
    print('Repository: ' + REPO + ' | branch: ' + BRANCH, flush=True)
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
