import contextlib
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

SPEC = importlib.util.spec_from_file_location('patch_server', Path(__file__).resolve().parents[1] / 'server.py')
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def jar(path, version):
    with zipfile.ZipFile(str(path), 'w') as archive:
        archive.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\nMain-Class: example.Main\n')
        archive.writestr('version', version)


class FakeManager(server.Manager):
    """隔离进程/端口，不调用真实 Java 或服务。其余部署逻辑与正式版相同。"""
    def __init__(self, config):
        super().__init__(config)
        self.running = True
        self.fail_start = False
        self.keep_port = False
        self.actions = []
        self.stop_hook = None

    def pids(self):
        return [4242] if self.running else []

    def port_open(self):
        return self.running or self.keep_port

    def command(self, action, timeout, job):
        self.actions.append(action)
        if action == 'stop':
            if self.stop_hook:
                self.stop_hook()
            self.running = False
        else:
            self.running = not self.fail_start


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        jar(self.app / 'datafly-admin.jar', 'old')
        (self.app / 'datafly.sh').write_text('#!/bin/bash\ntrue\n')
        self.config = server.defaults(self.root / 'config.json')
        self.config.update(app_dir=str(self.app), data_dir=str(self.root / 'runtime'),
                           bind='127.0.0.1', stable_seconds=0, start_timeout=0.05,
                           salt='00' * 16, password_hash=hashlib.pbkdf2_hmac('sha256', b'test-password-123', bytes(16), 260000).hex())
        self.manager = FakeManager(self.config)

    def tearDown(self):
        deadline = time.time() + 15
        while self.manager.active and time.time() < deadline:
            time.sleep(0.01)
        self.temp.cleanup()

    def upload(self):
        uid = 'a' * 32
        path = self.manager.data / 'uploads' / (uid + '.jar')
        jar(path, 'new')
        server.atomic_json(path.with_suffix('.json'), {'id': uid, 'sha256': server.digest(path), 'name': 'test.jar', 'size': path.stat().st_size, 'created': server.now()})
        return uid

    def wait(self, job_id):
        deadline = time.time() + 15
        while self.manager.active and time.time() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.manager.active, '测试任务没有结束')
        return self.manager.jobs[job_id]

    def test_update_then_rollback_preserves_both_versions(self):
        self.config['app_port'] = 8888
        old = server.digest(self.manager.jar)
        uid = self.upload()
        new = server.digest(self.manager.data / 'uploads' / (uid + '.jar'))
        first = self.wait(self.manager.submit('update', uid))
        self.assertEqual(first['state'], 'succeeded')
        self.assertTrue(any('8888' in event['message'] for event in first['events']))
        self.assertEqual(server.digest(self.manager.jar), new)
        self.assertEqual(server.digest(self.manager.backups / first['backup']), old)
        second = self.wait(self.manager.submit('rollback', first['backup']))
        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(server.digest(self.manager.jar), old)
        self.assertEqual(server.digest(self.manager.backups / second['backup']), new)

    def test_start_failure_keeps_backup_and_does_not_auto_restart(self):
        self.manager.fail_start = True
        old = server.digest(self.manager.jar)
        job = self.wait(self.manager.submit('update', self.upload()))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(self.manager.actions, ['stop', 'start'])
        self.assertEqual(server.digest(self.manager.backups / job['backup']), old)

    def test_tampered_upload_never_stops_service(self):
        uid = self.upload()
        jar(self.manager.data / 'uploads' / (uid + '.jar'), 'tampered')
        old = server.digest(self.manager.jar)
        job = self.wait(self.manager.submit('update', uid))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(self.manager.actions, [])
        self.assertEqual(server.digest(self.manager.jar), old)

    def test_lock_blocks_second_update(self):
        entered, release = threading.Event(), threading.Event()
        self.manager.stop_hook = lambda: (entered.set(), release.wait(5))
        uid = self.upload()
        first = self.manager.submit('update', uid)
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(ValueError, '已有更新'):
                self.manager.submit('update', uid)
            with self.assertRaisesRegex(ValueError, '不能删除'):
                self.manager.remove_upload(uid)
        finally:
            release.set()
        self.assertEqual(self.wait(first)['state'], 'succeeded')

    def test_stop_failure_does_not_replace(self):
        old = server.digest(self.manager.jar)
        def failed_stop():
            raise RuntimeError('停止超时')
        self.manager.stop_hook = failed_stop
        job = self.wait(self.manager.submit('update', self.upload()))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(server.digest(self.manager.jar), old)
        self.assertEqual(self.manager.actions, ['stop'])

    def test_known_bad_start_command_is_blocked_before_stop(self):
        (self.app / 'datafly.sh').write_text('nohup java -jar -Dproperty=value datafly-admin.jar &\n')
        with self.assertRaisesRegex(ValueError, '-D 参数'):
            self.manager.submit('update', self.upload())
        self.assertEqual(self.manager.actions, [])

    def test_bad_jar_is_rejected(self):
        path = self.root / 'broken.jar'
        path.write_text('not a jar')
        with self.assertRaises(ValueError):
            server.check_jar(path, self.config['max_unpacked'])

    def test_process_detection_supports_legacy_args_and_checks_directory(self):
        proc = self.root / 'proc'
        entry = proc / '382155'
        entry.mkdir(parents=True)
        (entry / 'exe').symlink_to('/usr/lib/jvm/jdk-1.8-oracle-x64/bin/java')
        (entry / 'cwd').symlink_to(self.app)
        for args in (['java', '-jar', 'datafly-admin.jar'],
                     ['java', '-jar', '-Djava.util.Arrays.useLegacyMergesort=true', 'datafly-admin.jar']):
            (entry / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in args) + b'\0')
            with mock.patch.object(server, 'Path', side_effect=lambda value: proc if value == '/proc' else Path(value)):
                self.assertEqual(server.Manager.pids(self.manager), [382155])
        (entry / 'cwd').unlink()
        (entry / 'cwd').symlink_to(self.root)
        with mock.patch.object(server, 'Path', side_effect=lambda value: proc if value == '/proc' else Path(value)):
            self.assertEqual(server.Manager.pids(self.manager), [])

    def test_restart_marks_unfinished_job_interrupted(self):
        job = {'id':'test', 'state':'running', 'events':[]}
        server.atomic_json(self.manager.data / 'jobs' / 'test.json', job)
        resumed = FakeManager(self.config)
        self.assertEqual(resumed.jobs['test']['state'], 'interrupted')
        self.assertEqual(resumed.actions, [])

    @contextlib.contextmanager
    def http_server(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.manager = self.manager
        httpd.upload_lock = threading.Lock()
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd.server_address[1]
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    def request(self, port, route, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', port)
        hdrs = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body)
            hdrs['Content-Type'] = 'application/json'
        conn.request('GET' if body is None else 'POST', '/patch-tool/' + route, body=body, headers=hdrs)
        response = conn.getresponse()
        value = response.read()
        result = response.status, dict(response.getheaders()), value
        conn.close()
        return result

    def test_http_auth_csrf_upload_and_path_boundary(self):
        with self.http_server() as port:
            self.assertEqual(self.request(port,'api/state')[0], 401)
            status, headers, body = self.request(port,'api/login',{'password':'test-password-123'})
            self.assertEqual(status,200)
            auth = {'Cookie':headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token':json.loads(body)['csrf']}
            self.assertIn('HttpOnly',headers['Set-Cookie'])
            self.assertEqual(self.request(port,'api/state',headers=auth)[0],200)
            self.assertEqual(self.request(port,'api/deploy',{'kind':'update'}, {'Cookie':auth['Cookie']})[0],403)
            file = self.root / 'upload.jar'
            jar(file,'uploaded')
            status, _, body = self.request(port,'api/upload',file.read_bytes(),auth)
            self.assertEqual(status,201)
            meta=json.loads(body)
            self.assertEqual(meta['sha256'],server.digest(file))
            bad = dict(auth, Origin='http://other.example')
            self.assertEqual(self.request(port,'api/remove-upload',{'source':meta['id']},bad)[0],403)
            self.assertEqual(self.request(port,'api/deploy',{'kind':'rollback','source':'../../etc/passwd','confirm':'更新影像'},auth)[0],400)
            self.assertEqual(self.request(port,'api/remove-upload',{'source':meta['id']},auth)[0],200)
            self.assertFalse((self.manager.data / 'uploads' / (meta['id']+'.jar')).exists())
            self.assertEqual(self.request(port,'api/logout',{},auth)[0],200)
            self.assertEqual(self.request(port,'api/state',headers=auth)[0],401)

    def test_real_stop_script_timeout_preserves_java_boundary(self):
        # 只运行临时 shell 的 sleep，不会触碰任何真实服务。
        (self.app / 'datafly.sh').write_text('#!/bin/bash\nsleep 30\n')
        job = {'id':'timeout-test'}
        started=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'超时'):
            server.Manager.command(self.manager, 'stop', 0.1, job)
        self.assertLess(time.monotonic()-started,5)


if __name__ == '__main__':
    unittest.main()
