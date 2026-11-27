import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        shutil.copy(Path(__file__).resolve().parents[1] / 'upload_server.py', root)
        (root / 'upload.html').write_text('upload page')
        spec = importlib.util.spec_from_file_location('uploader_test', root / 'upload_server.py')
        self.m = importlib.util.module_from_spec(spec)
        def startup(*args, **kwargs):
            out = 'arena/test' if args[0][0] == 'git' else 'owner/repo'
            return type('Result', (), {'returncode': 0, 'stdout': out, 'stderr': ''})()
        with patch('subprocess.run', side_effect=startup):
            spec.loader.exec_module(self.m)
        self.m.save()
        self.server = self.m.ThreadingHTTPServer(('127.0.0.1', 0), self.m.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.fid = 'a' * 64

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, body=None, auth=True, raw=False, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        headers = headers if headers is not None else ({'Authorization': 'Bearer ' + self.m.TOKEN} if auth else {})
        if body is not None and not raw:
            body = json.dumps(body).encode()
        conn.request('GET' if body is None else 'POST', path, body, headers)
        res = conn.getresponse()
        payload = json.loads(res.read())
        conn.close()
        return res.status, payload

    def init(self, size=6):
        return self.request('/api/init', {'id': self.fid, 'name': 'Крым.mp4', 'size': size, 'public_backup_confirmed': True})

    def chunk(self, data, offset=0, digest=None):
        return self.request('/api/chunk?id=' + self.fid + '&offset=' + str(offset) + '&sha256=' +
                            (digest or hashlib.sha256(data).hexdigest()), data, raw=True)

    def test_auth_required(self):
        self.assertEqual(self.request('/api/files', auth=False)[0], 401)
        self.assertEqual(self.request('/api/init', {}, auth=False)[0], 401)
        self.assertNotIn('token', self.request('/api/files')[1])

    def test_proxy_safe_upload_header(self):
        self.assertEqual(self.request('/api/files', headers={'X-Upload-Token': self.m.TOKEN})[0], 200)
        self.assertEqual(self.request('/api/files', headers={'X-Upload-Token': 'wrong'})[0], 401)
        self.assertEqual(self.request('/api/files', headers={'X-Upload-Token': self.m.TOKEN,
                         'Authorization': 'Bearer proxy-credential'})[0], 200)

    def test_chunks_resume_hash_and_finish(self):
        self.assertEqual(self.init()[0], 200)
        self.assertEqual(self.chunk(b'abc', digest='wrong')[0], 400)
        self.assertEqual(self.init()[1]['offset'], 0)
        self.assertEqual(self.chunk(b'abc')[1]['offset'], 3)
        self.assertEqual(self.chunk(b'abc')[0], 409)
        self.assertEqual(self.init()[1]['offset'], 3)
        self.assertEqual(self.request('/api/finish', {'id': self.fid})[0], 400)
        self.assertEqual(self.chunk(b'def', 3)[0], 200)
        self.assertEqual(self.request('/api/finish', {'id': self.fid})[1]['status'], 'queued')
        self.request('/api/finish', {'id': self.fid})
        self.assertEqual(self.m.JOBS.qsize(), 1)
        self.assertEqual((self.m.STORE / (self.fid + '.part')).read_bytes(), b'abcdef')
        self.assertEqual(json.loads(self.m.STATE.read_text())['files'][self.fid]['offset'], 6)

    def test_validation(self):
        self.assertEqual(self.init(2 * 1024**3)[0], 400)
        self.assertEqual(self.request('/api/init', {'id': '../../x', 'name': 'a', 'size': 5})[0], 400)
        self.assertEqual(self.init()[0], 200)
        self.assertEqual(self.init(7)[0], 400)
        self.assertEqual(self.chunk(b'1234567')[0], 400)

    def complete(self):
        self.init()
        self.chunk(b'abcdef')
        self.request('/api/finish', {'id': self.fid})

    def backup_commands(self, fail_push=False, wrong_remote=False):
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if args[:3] == ('git', 'branch', '--show-current'):
                return 'arena/test'
            if args[:3] == ('git', 'diff', '--cached'):
                return 'changed'
            if args[:3] == ('git', 'rev-parse', 'HEAD'):
                return 'f' * 40
            if args[:2] == ('git', 'ls-remote'):
                return ('a' if wrong_remote else 'f') * 40 + '\trefs/heads/arena/test'
            if args[:2] == ('git', 'push') and fail_push:
                raise RuntimeError('push failed')
            return ''
        return calls, run

    def test_backup_parts_commits_and_remote_verification(self):
        self.complete()
        calls, run = self.backup_commands()
        with patch.object(self.m, 'command', side_effect=run), patch.object(self.m, 'PART_SIZE', 2), patch.object(self.m, 'BATCH_SIZE', 2):
            self.m.backup(self.fid)
        f = self.m.state['files'][self.fid]
        self.assertEqual(f['status'], 'saved')
        manifest = json.loads((self.m.ROOT / 'videos' / (self.fid + '.json')).read_text())
        self.assertEqual(manifest['sha256'], hashlib.sha256(b'abcdef').hexdigest())
        self.assertEqual(len(manifest['parts']), 3)
        restored = b''.join((self.m.ROOT / p['path']).read_bytes() for p in manifest['parts'])
        self.assertEqual(restored, b'abcdef')
        self.assertTrue(manifest['public_backup_confirmed'])
        self.assertEqual(calls.count(('git', 'push', 'origin', 'arena/test')), 3)
        commits = [c for c in calls if 'commit' in c]
        self.assertTrue(all('--only' in c for c in commits))
        import restore_video
        with patch.object(restore_video, 'ROOT', self.m.ROOT):
            manifest_path = self.m.ROOT / 'videos' / (self.fid + '.json')
            output = self.m.ROOT / 'restored'
            restored_path = restore_video.restore(manifest_path, output)
            self.assertEqual(restored_path.read_bytes(), b'abcdef')
            with self.assertRaises(FileExistsError):
                restore_video.restore(manifest_path, output)
            restored_path.unlink()
            (self.m.ROOT / manifest['parts'][0]['path']).write_bytes(b'xx')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                restore_video.restore(manifest_path, output)
            self.assertFalse(restored_path.exists())

    def test_failed_push_never_marked_saved(self):
        self.complete()
        _, run = self.backup_commands(fail_push=True)
        with patch.object(self.m, 'command', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'push failed'):
                self.m.backup(self.fid)
        self.assertNotEqual(self.m.state['files'][self.fid]['status'], 'saved')

    def test_remote_mismatch_never_marked_saved(self):
        self.complete()
        _, run = self.backup_commands(wrong_remote=True)
        with patch.object(self.m, 'command', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'Remote branch verification failed'):
                self.m.backup(self.fid)
        self.assertNotEqual(self.m.state['files'][self.fid]['status'], 'saved')

    def test_explicit_public_consent_required(self):
        self.assertEqual(self.request('/api/init', {'id': self.fid, 'name': 'private.mp4', 'size': 5})[0], 400)
        self.assertFalse(self.m.state['files'])

    def test_branch_change_refused(self):
        self.complete()
        with patch.object(self.m, 'command', return_value='different-branch'):
            with self.assertRaisesRegex(RuntimeError, 'branch changed'):
                self.m.backup(self.fid)


if __name__ == '__main__':
    unittest.main()
