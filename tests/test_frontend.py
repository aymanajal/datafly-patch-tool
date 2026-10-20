import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

import test_server as backend_tests
server = backend_tests.server
from frontend import check_frontend


def frontend_zip(path, version='new'):
    with zipfile.ZipFile(str(path), 'w') as archive:
        archive.writestr('dist/', '')
        archive.writestr('dist/index.html', '<html>%s</html>' % version)
        archive.writestr('dist/static/app.js', 'version=' + version)
        archive.writestr('__MACOSX/._dist', 'metadata')
        archive.writestr('dist/.DS_Store', 'metadata')


class FrontendTests(unittest.TestCase):
    http_server = backend_tests.ToolTests.http_server
    request = backend_tests.ToolTests.request
    wait = backend_tests.ToolTests.wait

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.front = self.root / 'html' / 'dist'
        self.front.mkdir(parents=True)
        (self.front / 'index.html').write_text('old index')
        (self.front / 'old.js').write_text('old asset')
        self.config = server.defaults(self.root / 'config.json')
        self.config.update(deployment_type='frontend', frontend_dir=str(self.front),
                           data_dir=str(self.root / 'runtime'))
        self.manager = server.FrontendManager(self.config)

    def tearDown(self):
        deadline = time.time() + 10
        while self.manager.active and time.time() < deadline:
            time.sleep(0.01)
        self.temp.cleanup()

    def upload(self):
        uid = 'b' * 32
        path = self.manager.data / 'uploads' / (uid + '.zip')
        frontend_zip(path)
        server.atomic_json(path.with_suffix('.json'), {'id': uid, 'sha256': server.digest(path),
                           'name': 'dist.zip', 'size': path.stat().st_size, 'created': server.now()})
        return uid

    def test_update_rollback_preserves_versions_and_never_restarts_services(self):
        with mock.patch.object(self.manager, 'command', side_effect=AssertionError('不能调用服务命令')):
            first = self.wait(self.manager.submit('update', self.upload()))
            self.assertEqual(first['state'], 'succeeded', first['events'])
            self.assertIn('new', (self.front / 'index.html').read_text())
            self.assertFalse((self.front / 'old.js').exists())
            self.assertFalse((self.front / '.DS_Store').exists())
            self.assertEqual(stat.S_IMODE(self.front.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((self.front / 'index.html').stat().st_mode), 0o644)
            backup = self.front.parent / first['backup']
            self.assertEqual((backup / 'index.html').read_text(), 'old index')
            second = self.wait(self.manager.submit('rollback', first['backup']))
            self.assertEqual(second['state'], 'succeeded', second['events'])
            self.assertEqual((self.front / 'index.html').read_text(), 'old index')
            self.assertTrue((self.front / 'old.js').exists())
            self.assertIn('new', (self.front.parent / second['backup'] / 'index.html').read_text())
            self.assertTrue(backup.exists(), '回退不消耗来源备份')

    def test_existing_manual_backup_can_be_selected(self):
        backup = self.front.parent / 'dist-backup-20260101_120000'
        backup.mkdir()
        (backup / 'index.html').write_text('manual backup')
        self.assertIn(backup.name, [b['id'] for b in self.manager.snapshot()['backups']])
        job = self.wait(self.manager.submit('rollback', backup.name))
        self.assertEqual(job['state'], 'succeeded')
        self.assertEqual((self.front / 'index.html').read_text(), 'manual backup')

    def test_rejects_traversal_absolute_and_windows_paths(self):
        for bad in ('dist/../../escape', '/escape', 'dist/../escape', 'dist\\escape', 'dist/C:/escape'):
            path = self.root / 'bad.zip'
            with zipfile.ZipFile(str(path), 'w') as archive:
                archive.writestr('dist/index.html', 'index')
                archive.writestr(bad, 'danger')
            with self.assertRaises(ValueError, msg=bad):
                check_frontend(path, self.config['max_unpacked'])
        self.assertFalse((self.root / 'escape').exists())

    def test_rejects_symlink_zip_and_missing_index(self):
        path = self.root / 'bad.zip'
        with zipfile.ZipFile(str(path), 'w') as archive:
            archive.writestr('dist/index.html', 'index')
            info = zipfile.ZipInfo('dist/link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, '/etc/passwd')
        with self.assertRaisesRegex(ValueError, '链接'):
            check_frontend(path, self.config['max_unpacked'])
        with zipfile.ZipFile(str(path), 'w') as archive:
            archive.writestr('dist/app.js', 'no index')
        with self.assertRaisesRegex(ValueError, 'index.html'):
            check_frontend(path, self.config['max_unpacked'])

    def test_rejects_unpacked_limit(self):
        path = self.root / 'large.zip'
        frontend_zip(path)
        with self.assertRaisesRegex(ValueError, '体积'):
            check_frontend(path, 1)

    def test_tampering_never_switches(self):
        uid = self.upload()
        frontend_zip(self.manager.data / 'uploads' / (uid + '.zip'), 'tampered')
        job = self.wait(self.manager.submit('update', uid))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual((self.front / 'index.html').read_text(), 'old index')
        self.assertFalse(list(self.front.parent.glob('dist-backup-*')))

    def test_failed_rename_restores_old_directory(self):
        original = Path.rename
        def rename(path, target):
            if path.name == 'dist' and path.parent.name.startswith('.front-stage-'):
                raise OSError('模拟新目录切换失败')
            return original(path, target)
        with mock.patch.object(Path, 'rename', autospec=True, side_effect=rename):
            job = self.wait(self.manager.submit('update', self.upload()))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual((self.front / 'index.html').read_text(), 'old index')
        self.assertIn('已恢复', job['events'][-1]['message'])
        self.assertFalse(list(self.front.parent.glob('.front-stage-*')))

    def test_post_switch_failure_restores_old_directory(self):
        original = server.tree_manifest
        triggered = []
        def manifest(path, limit):
            if path == self.manager.front and list(self.front.parent.glob('dist-backup-*')) and not triggered:
                triggered.append(True)
                raise ValueError('模拟切换后校验失败')
            return original(path, limit)
        with mock.patch.object(server, 'tree_manifest', side_effect=manifest):
            job = self.wait(self.manager.submit('update', self.upload()))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual((self.front / 'index.html').read_text(), 'old index')
        self.assertIn('已恢复', job['events'][-1]['message'])

    def test_lock_and_remove_upload_boundary(self):
        entered, release = threading.Event(), threading.Event()
        original = server.extract_frontend
        def extract(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        uid = self.upload()
        with mock.patch.object(server, 'extract_frontend', side_effect=extract):
            first = self.manager.submit('update', uid)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(ValueError, '已有更新'):
                    self.manager.submit('update', uid)
                with self.assertRaisesRegex(ValueError, '不能删除'):
                    self.manager.remove_upload(uid)
            finally:
                release.set()
            self.assertEqual(self.wait(first)['state'], 'succeeded')
        self.manager.remove_upload(uid)
        self.assertFalse((self.manager.data / 'uploads' / (uid + '.zip')).exists())

    def test_rejects_symlink_live_directory_and_backup_escape(self):
        actual = self.front.with_name('actual')
        self.front.rename(actual)
        self.front.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, '链接'):
            self.manager.preflight()
        with self.assertRaises(ValueError):
            self.manager.submit('rollback', '../../other')

    def test_http_frontend_upload_deploy_and_status(self):
        with self.http_server() as port:
            self.manager.sessions['test'] = {'csrf': 'csrf-test', 'expires': time.time() + 60}
            auth = {'Cookie': 'patch_session=test', 'X-CSRF-Token': 'csrf-test'}
            uid = self.upload()
            path = self.manager.data / 'uploads' / (uid + '.zip')
            status, _, body = self.request(port, 'api/upload', path.read_bytes(), auth)
            self.assertEqual(status, 201, body)
            meta = json.loads(body)
            self.assertTrue((path.parent / (meta['id'] + '.zip')).exists())
            status, _, body = self.request(port, 'api/state', headers=auth)
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertEqual(data['status']['deployment_type'], 'frontend')
            self.assertTrue(data['status']['frontend_ready'])
            status, _, body = self.request(port, 'api/deploy', {'kind': 'update', 'source': meta['id'], 'confirm': '更新影像'}, auth)
            self.assertEqual(status, 202, body)
            self.assertEqual(self.wait(json.loads(body)['id'])['state'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
