import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = all(importlib.util.find_spec(name) for name in ('av', 'imageio_ffmpeg', 'PIL'))


@unittest.skipUnless(AVAILABLE, 'Run with .venv/bin/python after installing film dependencies')
class FilmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('film_test', ROOT / 'tools/render_film.py')
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_edit_covers_seven_sources_and_keeps_mannequin(self):
        sources = [s for s in self.m.PLAN['segments'] if s['type'] == 'source']
        self.assertEqual(len(set(s['source'] for s in sources)), 7)
        location = next(s for s in sources if s['source'] == 'IMG_4682.MOV')
        self.assertLessEqual(location['start'], 264)
        self.assertGreaterEqual(location['end'], 294)
        self.assertAlmostEqual(self.m.TOTAL, 833.4)
        for s in sources:
            self.assertLess(s['start'], s['end'])
            for key in ('start', 'end'):
                self.assertAlmostEqual(s[key] * 30, round(s[key] * 30))

    def test_title_image(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / 'title.png'
            self.m.title_image(self.m.PLAN['segments'][0], dest)
            with Image.open(dest) as image:
                self.assertEqual(image.size, (1920, 1080))

    def test_loudnorm_allows_non_numeric_metadata(self):
        m = self.m
        measurement = {'input_i': '-20.1', 'input_tp': '-2.0', 'input_lra': '8.0',
                       'input_thresh': '-32.0', 'target_offset': '0.1', 'normalization_type': 'dynamic'}
        segment = {'id': 'test', 'start': 0, 'end': 3}
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'WORK', Path(temp)), patch.object(m, 'run', return_value=json.dumps(measurement)):
            result = m.audio_filter(segment, Path('test.mov'))
            self.assertIn('measured_I=-20.1', result)
            self.assertNotIn('normalization_type', result)

    def test_stream_ranges_and_download(self):
        m = self.m
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / 'video.mp4'
            dest.write_bytes(b'0123456789')
            with patch.object(m, 'OUTPUT', dest), patch.dict(m.PROGRESS, {'film_ready': True}):
                server = m.ThreadingHTTPServer(('127.0.0.1', 0), m.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def request(path, headers=None):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=10)
                    conn.request('GET', path, headers=headers or {})
                    response = conn.getresponse()
                    result = response.status, dict(response.getheaders()), response.read()
                    conn.close()
                    return result
                try:
                    code, headers, data = request('/film.mp4', {'Range': 'bytes=2-5'})
                    self.assertEqual((code, data), (206, b'2345'))
                    self.assertEqual(headers['Content-Range'], 'bytes 2-5/10')
                    self.assertEqual(request('/film.mp4', {'Range': 'bytes=-3'})[2], b'789')
                    self.assertEqual(request('/film.mp4', {'Range': 'bytes=10-'})[0], 416)
                    code, headers, data = request('/film.mp4?download=1')
                    self.assertEqual((code, data), (200, b'0123456789'))
                    self.assertIn('attachment', headers['Content-Disposition'])
                    self.assertTrue(json.loads(request('/status')[2])['film_ready'])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


if __name__ == '__main__':
    unittest.main()
