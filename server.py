#!/usr/bin/env python3
"""Datafly 本机补丁工具。Python 3.7+，仅使用标准库。"""
import argparse
import base64
import copy
import datetime
import fcntl
import getpass
import hashlib
import hmac
import http.cookies
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import zipfile
import tempfile
from frontend import check_frontend, extract_frontend, tree_manifest, normalize_permissions, sync_directory

ROOT = Path(__file__).resolve().parent


def now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds')


def digest(path):
    value = hashlib.sha256()
    with open(str(path), 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def atomic_json(path, value):
    temp = path.with_name(path.name + '.tmp')
    with open(str(temp), 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temp), str(path))


def check_jar(path, max_unpacked):
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise ValueError('补丁文件不存在、为空或不是普通文件。')
    try:
        with zipfile.ZipFile(str(path)) as archive:
            infos = archive.infolist()
            if sum(item.file_size for item in infos) > max_unpacked or len(infos) > 200000:
                raise ValueError('JAR 解压后体积或文件数量超过限制。')
            if 'META-INF/MANIFEST.MF' not in archive.namelist():
                raise ValueError('JAR 缺少 META-INF/MANIFEST.MF。')
            if archive.getinfo('META-INF/MANIFEST.MF').file_size > 1024 * 1024:
                raise ValueError('JAR Manifest 体积异常。')
            manifest = archive.read('META-INF/MANIFEST.MF').decode('utf-8', errors='replace')
            if not re.search(r'^Main-Class:\s*\S+', manifest, re.MULTILINE | re.IGNORECASE):
                raise ValueError('JAR 缺少 Main-Class，不能通过 java -jar 启动。')
            if archive.testzip():
                raise ValueError('JAR 完整性检查失败。')
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError('无法读取 JAR：' + str(exc))


class Manager:
    upload_suffix = '.jar'

    def validate_upload(self, path):
        check_jar(path, self.config['max_unpacked'])

    def __init__(self, config):
        self.config = config
        self.app = Path(config['app_dir']).resolve()
        self.jar = self.app / 'datafly-admin.jar'
        self.data = Path(config['data_dir']).resolve()
        for name in ('uploads', 'jobs'):
            (self.data / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backups = self.app / 'patch-backups'
        self.mutex = threading.RLock()
        self.jobs = {}
        self.active = None
        self.sessions = {}
        self.attempts = {}
        for path in sorted((self.data / 'jobs').glob('*.json')):
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['state'] == 'running':
                job['state'] = 'interrupted'
                job['events'].append({'time': now(), 'message': '工具重启中断了监控。请核实服务状态，再决定是否回退。'})
                atomic_json(path, job)
            self.jobs[job['id']] = job

    def pids(self):
        # 精确匹配 java 的 -jar 参数和进程工作目录，避免 grep 命中其他应用。
        result = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / 'cmdline').read_bytes().split(b'\0')
                args = [os.fsdecode(arg) for arg in args if arg]
                exe = os.path.basename(os.readlink(str(entry / 'exe')))
                if exe != 'java' or '-jar' not in args:
                    continue
                target_index = args.index('-jar') + 1
                # 兼容现场旧进程参数：-jar 与 JAR 之间可能保留了 -D 属性。
                while target_index < len(args) and args[target_index].startswith('-D'):
                    target_index += 1
                target = args[target_index]
                cwd = Path(os.readlink(str(entry / 'cwd')))
                candidate = Path(target) if os.path.isabs(target) else cwd / target
                if candidate.resolve() == self.jar:
                    result.append(int(entry.name))
            except (OSError, ValueError, IndexError):
                continue
        return sorted(result)

    def port_open(self):
        try:
            with socket.create_connection(('127.0.0.1', self.config['app_port']), timeout=0.5):
                return True
        except OSError:
            return False

    def status(self):
        return {'pids': self.pids(), 'port_open': self.port_open(),
                'host': self.config['target_label'], 'app_dir': str(self.app),
                'port': self.config['app_port'], 'max_upload': self.config['max_upload'],
                'active': self.active, 'deployment_type': 'backend'}

    def snapshot(self):
        with self.mutex:
            jobs = copy.deepcopy(sorted(self.jobs.values(), key=lambda job: job['id'], reverse=True)[:50])
        uploads = []
        for path in sorted((self.data / 'uploads').glob('*.json'), reverse=True):
            item = json.loads(path.read_text(encoding='utf-8'))
            if path.with_suffix(self.upload_suffix).is_file():
                uploads.append(item)
        backups = []
        if self.backups.is_dir():
            for path in sorted(self.backups.glob('datafly-admin-*.jar'), reverse=True):
                if path.is_file() and not path.is_symlink():
                    backups.append({'id': path.name, 'size': path.stat().st_size})
        return {'status': self.status(), 'jobs': jobs, 'uploads': uploads[:100], 'backups': backups}

    def preflight(self):
        if not self.jar.is_file() or self.jar.is_symlink():
            raise ValueError('正式 JAR 不存在或为符号链接。')
        script = self.app / 'datafly.sh'
        content = script.read_text(encoding='utf-8')
        if re.search(r'-jar\s+-D', content):
            raise ValueError('datafly.sh 的 -D 参数位于 -jar 后。请先按说明修正启动行，再更新。')
        if shutil.which('bash') is None:
            raise ValueError('服务器缺少 bash。')
        if not os.access(str(self.app), os.W_OK):
            raise ValueError('没有部署目录写权限。')
        check = subprocess.run(['bash', '-n', str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if check.returncode:
            raise ValueError('datafly.sh 语法检查失败。')

    def event(self, job, message, **updates):
        with self.mutex:
            job.update(updates)
            job['events'].append({'time': now(), 'message': message})
            atomic_json(self.data / 'jobs' / (job['id'] + '.json'), job)

    def submit(self, kind, source_id):
        if kind not in ('update', 'rollback'):
            raise ValueError('无效操作。')
        if kind == 'update':
            if not re.fullmatch(r'[a-f0-9]{32}', source_id):
                raise ValueError('无效补丁编号。')
            source = self.data / 'uploads' / (source_id + '.jar')
            meta = json.loads(source.with_suffix('.json').read_text(encoding='utf-8'))
            expected = meta['sha256']
        else:
            if not re.fullmatch(r'datafly-admin-[0-9T_-]+-[a-f0-9]{8}\.jar', source_id):
                raise ValueError('请选择本工具创建的备份。')
            source = self.backups / source_id
            expected = None
        return self.start_job(kind, source_id, source, expected)

    def start_job(self, kind, source_id, source, expected):
        self.preflight()
        lock = open(str(self.app / '.datafly-update.lock'), 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise ValueError('已有更新任务正在运行，请稍后再试。')
        job_id = datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(4)
        job = {'id': job_id, 'kind': kind, 'source': source_id, 'state': 'running',
               'stage': 'validate', 'created': now(), 'events': [], 'backup': None,
               'deployment_type': self.config.get('deployment_type', 'backend')}
        try:
            with self.mutex:
                if self.active:
                    raise ValueError('已有更新任务正在运行。')
                self.jobs[job_id] = job
                atomic_json(self.data / 'jobs' / (job_id + '.json'), job)
                self.active = job_id
            threading.Thread(target=self.deploy, args=(job, source, expected, lock), daemon=False).start()
        except Exception:
            with self.mutex:
                self.active = None
            lock.close()
            raise
        return job_id

    def remove_upload(self, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
            raise ValueError('无效补丁编号。')
        with self.mutex:
            if self.active:
                raise ValueError('更新期间不能删除上传文件。')
            for suffix in ('.json', self.upload_suffix):
                path = self.data / 'uploads' / (source_id + suffix)
                if path.exists():
                    path.unlink()

    def command(self, action, timeout, job):
        # 只执行固定的 start/stop；不接受用户输入命令。
        log = self.data / 'jobs' / (job['id'] + '.log')
        with open(str(log), 'ab', buffering=0) as output:
            output.write(('\n[%s] datafly.sh %s\n' % (now(), action)).encode('utf-8'))
            proc = subprocess.Popen(['bash', './datafly.sh', action], cwd=str(self.app),
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                    start_new_session=True)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # stop 的 shell 可能在无限等待。仅结束脚本，不强杀 Java。
                if action == 'stop':
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise RuntimeError('%s 命令超时。请查看服务状态；没有强杀 Java。' % action)
            if code:
                raise RuntimeError('%s 命令返回 %s，请查看脚本输出。' % (action, code))

    def deploy(self, job, source, expected, lock):
        stage = self.app / ('.datafly-stage-' + job['id'])
        try:
            self.event(job, '校验补丁完整性并核对 SHA-256。')
            check_jar(source, self.config['max_unpacked'])
            sha = digest(source)
            if expected and not hmac.compare_digest(sha, expected):
                raise ValueError('补丁上传后发生变化，请重新上传。')
            old_size = self.jar.stat().st_size
            if shutil.disk_usage(str(self.app)).free < old_size + source.stat().st_size + 256 * 1024 ** 2:
                raise ValueError('部署目录可用空间不足，需容纳暂存包、旧包备份和 256MB 余量。')
            with open(str(source), 'rb') as inp, open(str(stage), 'xb') as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            if digest(stage) != sha:
                raise ValueError('暂存包校验失败。')
            st = self.jar.stat()
            if (stage.stat().st_uid, stage.stat().st_gid) != (st.st_uid, st.st_gid):
                os.chown(str(stage), st.st_uid, st.st_gid)
            os.chmod(str(stage), st.st_mode & 0o777)
            self.backups.mkdir(mode=0o700, exist_ok=True)
            backup = self.backups / ('datafly-admin-' + job['id'] + '.jar')
            self.event(job, '备份当前正式包。', stage='backup')
            shutil.copy2(str(self.jar), str(backup))
            with open(str(backup), 'rb') as stream:
                os.fsync(stream.fileno())
            if digest(self.jar) != digest(backup):
                raise ValueError('备份校验失败，尚未停止服务。')
            self.event(job, '备份完成：' + backup.name, backup=backup.name, sha256=sha)
            self.event(job, '停止影像后端，等待进程和 %s 端口释放。' % self.config['app_port'], stage='stop')
            self.command('stop', self.config['stop_timeout'], job)
            deadline = time.monotonic() + 10
            while self.pids() or self.port_open():
                if time.monotonic() >= deadline:
                    raise RuntimeError('停止后仍有目标 Java 进程或后端端口占用；未替换正式包。')
                time.sleep(1)
            self.event(job, '进程已退出、端口已释放，替换正式包。', stage='replace')
            os.replace(str(stage), str(self.jar))
            directory_fd = os.open(str(self.app), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.event(job, '启动影像后端。', stage='start')
            self.command('start', 20, job)
            self.event(job, '等待 Java 进程和后端端口连续稳定。', stage='verify')
            deadline = time.monotonic() + self.config['start_timeout']
            stable_since = None
            stable_pids = None
            while time.monotonic() < deadline:
                pids = self.pids()
                ready = bool(pids) and self.port_open()
                if ready:
                    if pids != stable_pids or stable_since is None:
                        stable_pids, stable_since = pids, time.monotonic()
                    if time.monotonic() - stable_since >= self.config['stable_seconds']:
                        self.event(job, '进程和端口检查通过。请继续访问影像功能，确认业务可用。',
                                   state='succeeded', stage='done', finished=now())
                        break
                else:
                    stable_since, stable_pids = None, None
                time.sleep(1)
            else:
                raise RuntimeError('启动检查超时，未确认稳定运行。请查看应用日志，必要时从本次备份回退。')
        except Exception as exc:
            self.event(job, str(exc), state='failed', finished=now())
        finally:
            if stage.exists():
                stage.unlink()
            lock.close()
            with self.mutex:
                self.active = None


class FrontendManager(Manager):
    upload_suffix = '.zip'

    def __init__(self, config):
        front = Path(config.get('frontend_dir', '/usr/local/nginx/html/dist')).absolute()
        if front.name != 'dist' or front.parent.is_symlink():
            raise ValueError('前端目标必须是普通父目录下的 dist 目录。')
        self.front = front.parent.resolve() / 'dist'
        local_config = dict(config, app_dir=str(self.front.parent))
        super().__init__(local_config)
        self.backups = self.front.parent

    def validate_upload(self, path):
        check_frontend(path, self.config['max_unpacked'])

    def status(self):
        available = self.front.is_dir() and not self.front.is_symlink() and (self.front / 'index.html').is_file()
        return {'host': self.config['target_label'], 'app_dir': str(self.front),
                'port': None, 'pids': [], 'port_open': False, 'frontend_ready': available,
                'max_upload': self.config['max_upload'], 'active': self.active,
                'deployment_type': 'frontend'}

    def snapshot(self):
        data = super().snapshot()
        data['backups'] = []
        for path in sorted(self.backups.glob('dist-backup-*'), reverse=True):
            if self.valid_backup(path.name) and path.is_dir() and not path.is_symlink():
                data['backups'].append({'id': path.name, 'size': None})
        return data

    @staticmethod
    def valid_backup(name):
        return isinstance(name, str) and bool(re.fullmatch(r'dist-backup-[0-9T_]+(?:-[a-f0-9]{8})?', name))

    def preflight(self):
        if self.front.is_symlink() or not self.front.is_dir() or not (self.front / 'index.html').is_file():
            raise ValueError('前端 dist 不存在、为链接或缺少 index.html。')
        if not os.access(str(self.front.parent), os.W_OK):
            raise ValueError('没有前端父目录写权限。')
        # 不允许将上传文件或登录配置放进对外提供的静态目录。
        if self.data == self.front or self.front in self.data.parents:
            raise ValueError('工具运行目录不能放在公开的 dist 内。')

    def submit(self, kind, source_id):
        if kind == 'update':
            if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
                raise ValueError('无效前端补丁编号。')
            source = self.data / 'uploads' / (source_id + '.zip')
            meta = json.loads(source.with_suffix('.json').read_text(encoding='utf-8'))
            expected = meta['sha256']
        elif kind == 'rollback' and self.valid_backup(source_id):
            source, expected = self.backups / source_id, None
        else:
            raise ValueError('请选择有效的前端补丁或备份。')
        return self.start_job(kind, source_id, source, expected)

    def deploy(self, job, source, expected, lock):
        stage = None
        backup = self.front.parent / ('dist-backup-' + job['id'])
        moved_old, moved_new = False, False
        recovery_failed = False
        try:
            self.event(job, '检查前端补丁；本操作不调用 Nginx 或 Java 启停命令。')
            old_manifest, _ = tree_manifest(self.front, self.config['max_unpacked'])
            if job['kind'] == 'update':
                size = check_frontend(source, self.config['max_unpacked'])
                sha = digest(source)
                if not hmac.compare_digest(sha, expected):
                    raise ValueError('前端补丁上传后发生变化。')
                source_manifest = None
            else:
                source_manifest, size = tree_manifest(source, self.config['max_unpacked'])
            if shutil.disk_usage(str(self.front.parent)).free < size + 256 * 1024 ** 2:
                raise ValueError('前端暂存空间不足。')
            stage = Path(tempfile.mkdtemp(prefix='.front-stage-', dir=str(self.front.parent)))
            candidate = stage / 'dist'
            self.event(job, '在独立目录准备新 dist，保留当前网页。', stage='extract')
            if job['kind'] == 'update':
                extract_frontend(source, candidate, self.config['max_unpacked'])
            else:
                shutil.copytree(str(source), str(candidate), symlinks=True)
            new_manifest, _ = tree_manifest(candidate, self.config['max_unpacked'])
            if source_manifest is not None and new_manifest != source_manifest:
                raise ValueError('回退暂存副本校验失败。')
            normalize_permissions(candidate)
            # 防止暂存期间其他人手工替换了正式文件。
            if tree_manifest(self.front, self.config['max_unpacked'])[0] != old_manifest:
                raise ValueError('正式前端在准备期间发生变化，已取消切换。')
            if backup.exists():
                raise ValueError('备份目录已存在，取消切换。')
            self.event(job, '即将备份并切换目录，期间可能有短暂访问间隙。', stage='replace', backup=backup.name)
            self.front.rename(backup)
            moved_old = True
            candidate.rename(self.front)
            moved_new = True
            sync_directory(self.front.parent)
            if tree_manifest(self.front, self.config['max_unpacked'])[0] != new_manifest:
                raise ValueError('切换后的文件校验失败。')
            self.event(job, '目录切换与文件校验通过。请强制刷新页面并验证登录和补丁功能。',
                       state='succeeded', stage='done', finished=now())
        except Exception as exc:
            recovery = ''
            if moved_old:
                try:
                    if moved_new:
                        self.front.rename(stage / 'failed-dist')
                    backup.rename(self.front)
                    sync_directory(self.front.parent)
                    recovery = ' 已恢复切换前的 dist。'
                except Exception as restore_exc:
                    recovery_failed = True
                    recovery = ' 自动恢复未完成：%s。旧目录位置：%s；请人工处理。' % (restore_exc, backup)
            self.event(job, str(exc) + recovery, state='failed', finished=now())
        finally:
            # 如果人工恢复仍需暂存文件，保留现场供排查。
            try:
                if not recovery_failed and stage and stage.exists() and (self.front / 'index.html').is_file():
                    shutil.rmtree(str(stage))
            finally:
                lock.close()
                with self.mutex:
                    self.active = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    @property
    def manager(self):
        return self.server.manager

    def setup(self):
        super().setup()
        self.connection.settimeout(300)

    def reply(self, status, body, content='application/json; charset=utf-8', cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        # 失败上传可能仍有未读请求体；每次请求均关闭连接避免复用错位。
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def session(self):
        try:
            cookies = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            sid = cookies['patch_session'].value
        except (KeyError, http.cookies.CookieError):
            return None, None
        with self.manager.mutex:
            session = self.manager.sessions.get(sid)
            if not session or session['expires'] < time.time():
                self.manager.sessions.pop(sid, None)
                return None, None
        return sid, session

    def route(self):
        path = urlsplit(self.path).path
        prefix = self.manager.config['base_path']
        if not path.startswith(prefix):
            raise ValueError('访问路径不正确。')
        return path[len(prefix):]

    def json_body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('请求需要 JSON。')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 8192:
            raise ValueError('无效请求长度。')
        value = json.loads(self.rfile.read(length).decode('utf-8'))
        if not isinstance(value, dict):
            raise ValueError('无效请求。')
        return value

    def do_GET(self):
        try:
            if self.client_address[0] not in self.manager.config['allowed_peers']:
                return self.reply(403, {'error': '此地址不允许访问更新工具。'})
            route = self.route()
            assets = {'': ('index.html', 'text/html; charset=utf-8'),
                      'app.js': ('app.js', 'application/javascript; charset=utf-8'),
                      'style.css': ('style.css', 'text/css; charset=utf-8')}
            if route in assets:
                name, mime = assets[route]
                return self.reply(200, (ROOT / 'web' / name).read_bytes(), mime)
            _, session = self.session()
            if not session:
                return self.reply(401, {'error': '请先登录。'})
            if route == 'api/state':
                data = self.manager.snapshot()
                data['csrf'] = session['csrf']
                return self.reply(200, data)
            if route.startswith('api/log/'):
                job_id = route[len('api/log/'):]
                if job_id not in self.manager.jobs:
                    raise ValueError('记录不存在。')
                log = self.manager.data / 'jobs' / (job_id + '.log')
                text = ''
                if log.is_file():
                    with open(str(log), 'rb') as stream:
                        stream.seek(max(0, log.stat().st_size - 65536))
                        text = stream.read().decode('utf-8', errors='replace')
                return self.reply(200, {'text': text})
            self.reply(404, {'error': '页面不存在。'})
        except (ValueError, OSError) as exc:
            self.reply(400, {'error': str(exc)})

    def do_POST(self):
        try:
            if self.client_address[0] not in self.manager.config['allowed_peers']:
                return self.reply(403, {'error': '此地址不允许访问更新工具。'})
            route = self.route()
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply(403, {'error': '请求来源不匹配。'})
            if route == 'api/login':
                return self.login()
            sid, session = self.session()
            if not session:
                return self.reply(401, {'error': '请先登录。'})
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                return self.reply(403, {'error': '页面已失效，请刷新重试。'})
            if route == 'api/upload':
                return self.upload()
            data = self.json_body()
            if route == 'api/logout':
                with self.manager.mutex:
                    self.manager.sessions.pop(sid, None)
                return self.reply(200, {'ok': True}, cookie=self.cookie('', expire=True))
            if route == 'api/deploy':
                if data.get('confirm') != '更新影像':
                    raise ValueError('请输入“更新影像”确认操作。')
                job_id = self.manager.submit(data.get('kind'), data.get('source', ''))
                return self.reply(202, {'id': job_id})
            if route == 'api/remove-upload':
                self.manager.remove_upload(data.get('source', ''))
                return self.reply(200, {'ok': True})
            self.reply(404, {'error': '接口不存在。'})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.reply(400, {'error': str(exc)})
        except Exception:
            self.log_error('请求处理失败')
            self.reply(500, {'error': '处理失败，请查看更新工具服务日志。'})

    def cookie(self, sid, expire=False):
        secure = '; Secure' if self.manager.config['secure_cookie'] else ''
        return 'patch_session=%s; Path=%s; HttpOnly; SameSite=Strict; Max-Age=%s%s' % (
            sid, self.manager.config['base_path'], 0 if expire else 3600, secure)

    def login(self):
        data = self.json_body()
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 1024:
            raise ValueError('无效密码。')
        # Nginx 代理下统一限制登录失败频率，不信任客户端提供的转发 IP。
        key = self.client_address[0]
        with self.manager.mutex:
            attempts = [t for t in self.manager.attempts.get(key, []) if t > time.time() - 60]
            self.manager.attempts[key] = attempts
            if len(attempts) >= 5:
                return self.reply(429, {'error': '尝试过于频繁，请一分钟后重试。'})
            attempts.append(time.time())
        config = self.manager.config
        value = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(config['salt']), 260000).hex()
        if not hmac.compare_digest(value, config['password_hash']):
            return self.reply(401, {'error': '密码不正确。'})
        sid, csrf = secrets.token_hex(32), secrets.token_hex(32)
        with self.manager.mutex:
            self.manager.attempts.pop(key, None)
            self.manager.sessions = {k: v for k, v in self.manager.sessions.items() if v['expires'] > time.time()}
            self.manager.sessions[sid] = {'csrf': csrf, 'expires': time.time() + 3600}
        self.reply(200, {'csrf': csrf}, cookie=self.cookie(sid))

    def upload(self):
        length = int(self.headers.get('Content-Length', '0'))
        config = self.manager.config
        if self.headers.get('Transfer-Encoding') or not 0 < length <= config['max_upload']:
            raise ValueError('补丁超过上传限制，或上传长度无效。')
        if shutil.disk_usage(str(self.manager.data)).free < length + 256 * 1024 ** 2:
            raise ValueError('上传目录空间不足。')
        # 上传也串行，限制同时校验大文件造成的内存/磁盘压力。
        if not self.server.upload_lock.acquire(False):
            return self.reply(409, {'error': '正在上传另一份补丁，请稍后重试。'})
        upload_id = secrets.token_hex(16)
        path = self.manager.data / 'uploads' / (upload_id + self.manager.upload_suffix)
        try:
            total = sum(item.stat().st_size for item in (self.manager.data / 'uploads').iterdir() if item.suffix in ('.jar', '.zip'))
            if total + length > config['max_stored_uploads']:
                raise ValueError('待更新补丁累计超过 5GB，请先删除不再需要的上传文件。')
            with open(str(path), 'xb') as stream:
                remain = length
                while remain:
                    chunk = self.rfile.read(min(remain, 1024 * 1024))
                    if not chunk:
                        raise ValueError('上传中断，请重新上传。')
                    stream.write(chunk)
                    remain -= len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            self.manager.validate_upload(path)
            default_name = 'dist.zip' if self.manager.upload_suffix == '.zip' else 'datafly-admin.jar'
            name = self.headers.get('X-File-Name', default_name)
            try:
                name = base64.b64decode(name, validate=True).decode('utf-8')
            except (ValueError, UnicodeError):
                name = default_name
            name = name.replace('\\', '/').rsplit('/', 1)[-1][:180]
            meta = {'id': upload_id, 'name': name, 'size': length, 'sha256': digest(path), 'created': now()}
            atomic_json(path.with_suffix('.json'), meta)
            self.reply(201, meta)
        except Exception:
            if path.exists():
                path.unlink()
            raise
        finally:
            self.server.upload_lock.release()


def defaults(config_path):
    return {'app_dir': '/data/datafly', 'data_dir': str(config_path.parent / 'runtime'),
            'bind': '127.0.0.1', 'port': 9081, 'base_path': '/patch-tool/',
            'target_label': '影像后端服务器', 'app_port': 2049,
            'max_upload': 1024 ** 3, 'max_unpacked': 4 * 1024 ** 3,
            'max_stored_uploads': 5 * 1024 ** 3,
            'allowed_peers': ['127.0.0.1'],
            'stop_timeout': 120, 'start_timeout': 300, 'stable_seconds': 10,
            'deployment_type': 'backend', 'frontend_dir': '/usr/local/nginx/html/dist',
            'secure_cookie': False}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'config.json'))
    parser.add_argument('--init', action='store_true', help='交互设置登录密码和默认配置')
    parser.add_argument('--check', action='store_true', help='只检查配置和服务脚本，不停止服务')
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if args.init:
        if config_path.exists():
            raise SystemExit('配置已存在，未覆盖。更改密码请按部署说明操作。')
        password = getpass.getpass('设置网页管理密码（至少 12 位）：')
        if len(password) < 12 or password != getpass.getpass('再次输入：'):
            raise SystemExit('密码长度不足或两次输入不一致。')
        config = defaults(config_path)
        config['salt'] = secrets.token_hex(16)
        config['password_hash'] = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(config['salt']), 260000).hex()
        atomic_json(config_path, config)
        print('配置已生成：' + str(config_path))
        return
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if not config['base_path'].startswith('/') or not config['base_path'].endswith('/'):
        raise SystemExit('base_path 必须以 / 开头和结尾。')
    data_dir = Path(config['data_dir']).resolve()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    instance = open(str(data_dir / '.web-instance.lock'), 'a')
    try:
        fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('同一数据目录已有更新工具运行。')
    mode = config.get('deployment_type', 'backend')
    if mode not in ('backend', 'frontend'):
        raise SystemExit('deployment_type 只能为 backend 或 frontend。')
    manager = FrontendManager(config) if mode == 'frontend' else Manager(config)
    if args.check:
        manager.preflight()
        print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
        return
    server = ThreadingHTTPServer((config['bind'], config['port']), Handler)
    server.manager = manager
    server.upload_lock = threading.Lock()
    print('更新工具监听 %s:%s%s' % (config['bind'], config['port'], config['base_path']), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        instance.close()


if __name__ == '__main__':
    main()
