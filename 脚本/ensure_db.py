#!/usr/bin/env python3
"""按需构建 charter.db 查询缓存。

设计
----
Markdown 是唯一权威源；charter.db 是可丢弃、可重建的缓存。

缓存是否有效，由「源指纹」判定：
    指纹 = SHA-256(构建器版本 + 每个源文件的相对路径 + 内容，按路径排序)
源未变化 → 直接复用缓存，不重建；
源已变化或缓存缺失 → 全量重建，并以原子替换（先写 .tmp 再 rename）落盘。

这样既保留了 SQLite 的哈希索引、UNIQUE 约束与查询能力，又不让二进制
快照进入 git 历史。

用法
----
    python3 脚本/ensure_db.py            # 检查，必要时重建
    python3 脚本/ensure_db.py --status   # 只报告状态，不重建
    python3 脚本/ensure_db.py --force    # 强制重建
"""

import datetime
import hashlib
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import import_charter as ic          # noqa: E402  复用解析与 UPSERT
import build_disciplinary as bd      # noqa: E402  复用处罚规则初始数据

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'charter.db')

# 构建器版本：schema 或数据语义变更时递增，使旧缓存自动失效
BUILDER_VERSION = '1'

# 构建脚本本身也纳入指纹：解析逻辑变化时缓存自动失效
BUILDER_FILES = [
    os.path.join('脚本', 'import_charter.py'),
    os.path.join('脚本', 'build_disciplinary.py'),
    os.path.join('脚本', 'ensure_db.py'),
]

# 进入数据库的源目录（规范性文件；对外文书不入库）
SOURCE_DIRS = [
    os.path.join('文档', '规范性文件', '章程'),
    os.path.join('文档', '规范性文件', '规则'),
]

SCHEMA = """
CREATE TABLE articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    chapter     TEXT,
    article_num NOT NULL,
    article_cn  TEXT,
    content     TEXT,
    file_name   TEXT NOT NULL DEFAULT '',
    version     TEXT NOT NULL DEFAULT '严谨',
    hash        TEXT,
    UNIQUE(source, article_num, file_name, version)
);

CREATE TABLE disciplinary_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT    NOT NULL,
    behavior        TEXT    NOT NULL UNIQUE,
    severity        TEXT    NOT NULL,
    penalty_type    TEXT    NOT NULL,
    penalty_detail  TEXT,
    penalty_duration TEXT,
    deciding_body   TEXT    NOT NULL,
    procedure       TEXT,
    appeal_body     TEXT,
    appeal_deadline TEXT,
    charter_basis   TEXT,
    notes           TEXT
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ─────────────────────────────────────────────
#  源文件与指纹
# ─────────────────────────────────────────────

def source_files():
    """返回排序后的源 Markdown 绝对路径列表。"""
    files = []
    for rel_dir in SOURCE_DIRS:
        base = os.path.join(ROOT, rel_dir)
        for dirpath, _dirnames, names in os.walk(base):
            for name in names:
                if name.endswith('.md'):
                    files.append(os.path.join(dirpath, name))
    return sorted(files)


def fingerprint(files):
    """源指纹：源文件或构建脚本任一变化都会改变它。"""
    digest = hashlib.sha256()
    digest.update(BUILDER_VERSION.encode('utf-8'))
    for builder in BUILDER_FILES:
        path = os.path.join(ROOT, builder)
        if os.path.exists(path):
            with open(path, 'rb') as f:
                digest.update(f.read())
        digest.update(b'\0')
    for path in files:
        rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
        digest.update(rel.encode('utf-8'))
        digest.update(b'\0')
        with open(path, 'rb') as f:
            digest.update(f.read())
        digest.update(b'\0')
    return digest.hexdigest()


def infer_source_version(path):
    """按目录判定 source，按文件名判定 version。"""
    parts = os.path.relpath(path, ROOT).split(os.sep)
    if '章程' in parts:
        source = '章程'
    elif '规则' in parts:
        source = '规则'
    else:
        source = '章程'
    version = '通俗' if '通俗' in os.path.basename(path) else '严谨'
    return source, version


# ─────────────────────────────────────────────
#  缓存读取与重建
# ─────────────────────────────────────────────

def read_meta(db_path):
    """读取缓存元数据；不存在、损坏或旧格式时返回 None。"""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not has_meta:
            conn.close()
            return None
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        conn.close()
        return meta
    except sqlite3.DatabaseError:
        return None


def rebuild(files, fp):
    """全量重建缓存，成功后原子替换。返回耗时（秒）。"""
    started = time.time()
    tmp_path = DB_PATH + '.tmp'
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(SCHEMA)

        article_count = 0
        for path in files:
            rows = ic.parse_file(path)
            source, version = infer_source_version(path)
            ic.upsert(conn, rows, source, os.path.basename(path), version,
                      dry_run=False, quiet=True)
            article_count += len(rows)

        for rule in bd.INITIAL_RULES:
            bd.upsert_rule(conn, rule)

        rule_count = conn.execute(
            'SELECT COUNT(*) FROM disciplinary_rules').fetchone()[0]

        conn.executemany(
            'INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)',
            [
                ('source_hash', fp),
                ('builder_version', BUILDER_VERSION),
                ('built_at', datetime.datetime.now().isoformat(timespec='seconds')),
                ('source_files', str(len(files))),
                ('article_count', str(article_count)),
                ('rule_count', str(rule_count)),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, DB_PATH)
    return time.time() - started


# ─────────────────────────────────────────────
#  入口
# ─────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    files = source_files()
    if not files:
        print('❌ 未找到任何源文件（检查 SOURCE_DIRS）')
        return 1

    fp = fingerprint(files)
    meta = read_meta(DB_PATH)
    cached = meta.get('source_hash') if meta else None
    hit = (cached == fp)

    if '--status' in args:
        print(f'源文件：{len(files)} 个')
        print(f'当前指纹：{fp[:16]}…')
        if hit:
            print(f'缓存状态：命中（构建于 {meta.get("built_at", "未知")}，'
                  f'{meta.get("article_count", "?")} 条条文）')
            print(f'缓存文件：{os.path.relpath(DB_PATH, ROOT)} '
                  f'（{os.path.getsize(DB_PATH) / 1024:.0f} KB）')
        elif meta is None:
            print('缓存状态：未命中（无缓存或格式过期）')
        else:
            print(f'缓存状态：未命中（源已变化，缓存指纹 {cached[:16]}…）')
        return 0

    if hit and '--force' not in args:
        print(f'✅ 缓存命中，源未变化：{meta.get("article_count", "?")} 条条文，'
              f'构建于 {meta.get("built_at", "未知")}')
        return 0

    if meta is None:
        print('ℹ️  无可用缓存，开始全量构建…')
    else:
        print(f'🔄 源已变化（{cached[:8]} → {fp[:8]}），重建缓存…')

    elapsed = rebuild(files, fp)

    meta = read_meta(DB_PATH) or {}
    size_kb = os.path.getsize(DB_PATH) / 1024
    print(f'✅ 缓存已重建：{meta.get("article_count", "?")} 条条文、'
          f'{meta.get("rule_count", "?")} 条处罚规则，'
          f'{size_kb:.0f} KB，耗时 {elapsed:.2f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
