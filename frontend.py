"""静态前端补丁校验与暂存，不执行 Nginx 或 Java 命令。"""
import hashlib
import os
from pathlib import PurePosixPath
import shutil
import stat
import zipfile


def zip_entries(archive, limit):
    infos = archive.infolist()
    if len(infos) > 200000 or sum(item.file_size for item in infos) > limit:
        raise ValueError('ZIP 解压体积或文件数量超过限制。')
    result, seen = [], set()
    for item in infos:
        name = item.filename
        parts = name.rstrip('/').split('/')
        if not name or name.startswith('/') or '\\' in name or any(p in ('', '.', '..') or ':' in p for p in parts):
            raise ValueError('ZIP 包含不安全路径。')
        mode = item.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError('ZIP 不允许包含符号链接或特殊文件。')
        if parts[0] == '__MACOSX' or parts[-1] == '.DS_Store':
            continue
        if parts[0] != 'dist':
            raise ValueError('ZIP 必须只包含 dist/ 前端目录（允许忽略 __MACOSX 元数据）。')
        if len(parts) == 1:
            if not item.is_dir():
                raise ValueError('dist 必须是目录。')
            continue
        relative = PurePosixPath(*parts[1:])
        key = str(relative)
        if key in seen:
            raise ValueError('ZIP 存在重复路径。')
        seen.add(key)
        result.append((item, relative))
    if not any(str(rel) == 'index.html' and not item.is_dir() and item.file_size > 0 for item, rel in result):
        raise ValueError('ZIP 缺少非空的 dist/index.html。')
    return result


def check_frontend(path, limit):
    if not path.is_file() or path.is_symlink():
        raise ValueError('前端 ZIP 不存在或为符号链接。')
    try:
        with zipfile.ZipFile(str(path)) as archive:
            entries = zip_entries(archive, limit)
            if archive.testzip():
                raise ValueError('ZIP 完整性检查失败。')
            return sum(item.file_size for item, _ in entries)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError('无法读取前端 ZIP：' + str(exc))


def extract_frontend(path, target, limit):
    # 目标是工具创建的私有暂存目录，不信任 ZIP 自带的权限及路径。
    target.mkdir(mode=0o755)
    with zipfile.ZipFile(str(path)) as archive:
        for item, relative in zip_entries(archive, limit):
            destination = target.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if item.is_dir():
                destination.mkdir(exist_ok=True, mode=0o755)
            else:
                with archive.open(item) as inp, open(str(destination), 'xb') as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                destination.chmod(0o644)
    normalize_permissions(target)


def tree_manifest(root, limit):
    if root.is_symlink() or not root.is_dir() or not (root / 'index.html').is_file():
        raise ValueError('前端目录不存在、为链接或缺少 index.html。')
    result, total, count = {}, 0, 0
    for folder, dirs, files in os.walk(str(root), followlinks=False):
        from pathlib import Path
        parent = Path(folder)
        for name in dirs + files:
            path = parent / name
            count += 1
            if count > 200000 or path.is_symlink():
                raise ValueError('前端目录文件过多或包含符号链接。')
        for name in files:
            path = parent / name
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('前端目录包含特殊文件。')
            total += path.stat().st_size
            if total > limit:
                raise ValueError('前端目录超过体积限制。')
            sha = hashlib.sha256()
            with open(str(path), 'rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    sha.update(block)
            result[str(path.relative_to(root))] = sha.hexdigest()
    if (root / 'index.html').stat().st_size == 0:
        raise ValueError('index.html 为空。')
    return result, total


def normalize_permissions(root):
    # 必须先验证树不存在链接；只在私有暂存副本中规范化权限。
    for folder, dirs, files in os.walk(str(root)):
        os.chmod(folder, 0o755)
        for name in files:
            os.chmod(os.path.join(folder, name), 0o644)


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
