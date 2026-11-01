#!/usr/bin/env python3
"""统计各文件在 .git 目录中的实际占用。

口径说明
--------
1. 遍历指定范围内的全部提交（默认 --all），收集每个路径在历史上出现过的
   所有 blob 对象。
2. 按 blob SHA 去重：同一内容版本在多个提交中重复出现，只计一次。
3. 占用取 `git cat-file --batch-check` 的 %(objectsize:disk)，
   即该对象在 .git 中的实际存储字节数（已压缩；delta 对象为 delta 大小）。
4. 按路径汇总后排序。

不含的部分
----------
tree / commit / tag 对象、pack 索引（.idx）、pack 头等元数据不计入
单文件，故「各文件合计」会小于 `du -sh .git`。

若两个路径共享同一 blob（内容完全相同），该 blob 会在两行各计一次；
表末另给出「全库唯一 blob」的真实合计。

用法
----
    python3 脚本/git_file_usage.py                # 全部引用，按实际占用排序
    python3 脚本/git_file_usage.py --head         # 只看当前 HEAD 可达历史
    python3 脚本/git_file_usage.py --detail       # 附解压后累计大小
    python3 脚本/git_file_usage.py --csv out.csv  # 导出 CSV
"""

import argparse
import os
import subprocess
import sys
import unicodedata
from collections import defaultdict


# ─────────────────────────────────────────────
#  git 调用
# ─────────────────────────────────────────────

def git(*args, check=True):
    """在仓库根目录执行 git 命令，返回 stdout（文本）。

    固定 core.quotePath=false，避免中文路径被转义为 \\344\\270\\264 形式。
    """
    proc = subprocess.run(
        ['git', '-c', 'core.quotePath=false', *args],
        capture_output=True, text=True, check=False,
        cwd=REPO_ROOT,
    )
    if check and proc.returncode != 0:
        sys.exit(f'git {" ".join(args)} 失败：\n{proc.stderr.strip()}')
    return proc.stdout


def find_repo_root():
    proc = subprocess.run(
        ['git', 'rev-parse', '--show-toplevel'],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit('当前目录不在 git 仓库内')
    return proc.stdout.strip()


REPO_ROOT = find_repo_root()


# ─────────────────────────────────────────────
#  数据采集
# ─────────────────────────────────────────────

def load_blob_sizes():
    """返回 {blob_sha: (原始字节数, .git 中实际字节数)}，覆盖仓库全部对象。"""
    out = git(
        'cat-file', '--batch-all-objects',
        '--batch-check=%(objectname) %(objecttype) %(objectsize) %(objectsize:disk)',
    )
    info = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        sha, obj_type, size, disk = parts
        if obj_type == 'blob':
            info[sha] = (int(size), int(disk))
    return info


def collect_paths(rev_range):
    """遍历范围内所有提交的树，返回 {path: {'blobs': set, 'commits': set}}。"""
    commits = git('rev-list', rev_range).split()
    if not commits:
        sys.exit(f'范围 {rev_range} 内没有提交')

    paths = defaultdict(lambda: {'blobs': set(), 'commits': set()})

    for sha in commits:
        # -r 递归、-l 显示对象大小；路径可能含空格，故只按第一个 Tab 切分
        out = git('ls-tree', '-r', '-l', sha)
        for line in out.splitlines():
            if '\t' not in line:
                continue
            meta, path = line.split('\t', 1)
            parts = meta.split()
            if len(parts) < 4 or parts[1] != 'blob':
                continue
            blob = parts[2]
            paths[path]['blobs'].add(blob)
            paths[path]['commits'].add(sha)

    return paths, commits


def head_sizes():
    """返回 {path: 原始字节数}，仅当前 HEAD 的快照。"""
    sizes = {}
    for line in git('ls-tree', '-r', '-l', 'HEAD').splitlines():
        if '\t' not in line:
            continue
        meta, path = line.split('\t', 1)
        parts = meta.split()
        if len(parts) >= 4 and parts[1] == 'blob' and parts[3].isdigit():
            sizes[path] = int(parts[3])
    return sizes


def dir_disk_usage(path):
    """du -sb 的实际字节数（含目录自身开销）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                pass
    return total


def dir_block_usage(path):
    """du 的磁盘块占用（块对齐后的真实占用）。"""
    proc = subprocess.run(
        ['du', '-s', '--block-size=1', path],
        capture_output=True, text=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return int(proc.stdout.split()[0])
    return 0


# ─────────────────────────────────────────────
#  格式化
# ─────────────────────────────────────────────

def human(n):
    """人类可读字节数。"""
    n = float(n)
    if n < 1024:
        return f'{n:.0f} B'
    for unit in ('KiB', 'MiB', 'GiB', 'TiB'):
        n /= 1024
        if n < 1024 or unit == 'TiB':
            return f'{n:.2f} {unit}'


def display_width(text):
    """终端显示宽度：东亚宽字符按 2 计。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
    return width


def pad(text, width, align='left'):
    gap = max(0, width - display_width(text))
    if align == 'right':
        return ' ' * gap + text
    return text + ' ' * gap


def build_rows(paths, blob_info, head):
    """聚合每个路径的占用。

    共享 blob（多个路径指向同一内容）按引用它的路径数均摊，
    使「各文件合计」严格等于「全库唯一 blob 合计」，即 .git 中的真实存储。
    """
    blob_refs = defaultdict(set)
    for path, data in paths.items():
        for blob in data['blobs']:
            blob_refs[blob].add(path)

    rows = []
    for path, data in paths.items():
        blobs = data['blobs']
        raw = sum(blob_info[b][0] for b in blobs if b in blob_info)
        disk = sum(blob_info[b][1] / len(blob_refs[b])
                   for b in blobs if b in blob_info)
        exclusive = sum(blob_info[b][1] for b in blobs
                        if b in blob_info and len(blob_refs[b]) == 1)
        rows.append({
            'path': path,
            'versions': len(blobs),
            'commits': len(data['commits']),
            'raw': raw,
            'disk': disk,
            'exclusive': exclusive,
            'head': head.get(path, 0),
            'alive': path in head,
        })
    rows.sort(key=lambda r: r['disk'], reverse=True)
    return rows


# ─────────────────────────────────────────────
#  主流程
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='统计各文件在 .git 目录中的实际占用（按历史版本去重）')
    parser.add_argument('--head', action='store_true',
                        help='只统计当前 HEAD 可达的历史（默认 --all）')
    parser.add_argument('--detail', action='store_true',
                        help='附加解压后累计大小与当前快照大小')
    parser.add_argument('--csv', metavar='FILE',
                        help='导出 CSV 到指定文件')
    parser.add_argument('--top', type=int, default=0, metavar='N',
                        help='只显示占用最大的 N 个路径（默认全部）')
    parser.add_argument('--alive', action='store_true',
                        help='只显示当前 HEAD 中仍存在的路径')
    args = parser.parse_args()

    rev_range = 'HEAD' if args.head else '--all'
    blob_info = load_blob_sizes()
    paths, commits = collect_paths(rev_range)
    head = head_sizes()
    rows = build_rows(paths, blob_info, head)

    total_disk = sum(r['disk'] for r in rows)
    total_raw = sum(r['raw'] for r in rows)
    total_versions = sum(r['versions'] for r in rows)

    # 全库唯一 blob 的真实合计（消除共享 blob 的重复计入）
    unique_blobs = set()
    for data in paths.values():
        unique_blobs |= data['blobs']
    unique_disk = sum(blob_info[b][1] for b in unique_blobs if b in blob_info)
    unique_raw = sum(blob_info[b][0] for b in unique_blobs if b in blob_info)

    # ── CSV ──
    if args.csv:
        import csv
        with open(args.csv, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['路径', '历史版本数', '涉及提交数', '解压累计字节',
                             '实际占用字节（分摊后）', '独占占用字节',
                             '当前大小字节', '当前是否存在'])
            for r in rows:
                writer.writerow([r['path'], r['versions'], r['commits'],
                                 r['raw'], round(r['disk'], 1), r['exclusive'],
                                 r['head'], '是' if r['alive'] else '否'])
            writer.writerow(['合计（共享已分摊）', total_versions, '—',
                             total_raw, round(total_disk, 1), '—', '—', '—'])
        print(f'已导出：{args.csv}')

    # ── 表格 ──
    head_label = 'HEAD 可达' if args.head else '全部引用'
    print(f'仓库：{REPO_ROOT}')
    print(f'范围：{head_label}（{len(commits)} 个提交）')
    print(f'口径：blob 在 .git 中的实际字节数；同一版本仅计一次，'
          f'多路径共享的内容按路径数均摊\n')

    display_rows = [r for r in rows if r['alive']] if args.alive else rows
    if args.top > 0:
        display_rows = display_rows[:args.top]

    path_width = min(60, max(12, max((display_width(r['path']) for r in display_rows),
                                    default=12)))
    headers = ['文件', '版本', '实际占用', '占比']
    if args.detail:
        headers += ['独占占用', '解压累计', '当前大小']
    widths = [10, 10, 10]
    if args.detail:
        widths += [10, 10, 10]

    def header_line():
        line = (pad(headers[0], path_width) + '  ' +
                pad(headers[1], 6, 'right') + '  ' +
                pad(headers[2], 10, 'right') + '  ' +
                pad(headers[3], 7, 'right'))
        for i in range(4, len(headers)):
            line += '  ' + pad(headers[i], widths[i - 3], 'right')
        return line

    total_width = path_width + 2 + 6 + 2 + 10 + 2 + 7 + sum(
        2 + w for w in widths[:len(headers) - 3])

    print(header_line())
    print('─' * total_width)

    for r in display_rows:
        shown = r['path']
        if display_width(shown) > path_width:
            while display_width('…' + shown) > path_width:
                shown = shown[1:]
            shown = '…' + shown

        pct = (r['disk'] / unique_disk * 100) if unique_disk else 0
        line = (
            pad(shown, path_width) + '  ' +
            pad(str(r['versions']), 6, 'right') + '  ' +
            pad(human(r['disk']), 10, 'right') + '  ' +
            pad(f'{pct:5.1f}%', 7, 'right')
        )
        if args.detail:
            line += ('  ' + pad(human(r['exclusive']), 10, 'right') +
                     '  ' + pad(human(r['raw']), 10, 'right') +
                     '  ' + pad(human(r['head']) if r['head'] else '—', 10, 'right'))
        print(line)

    print('─' * total_width)
    if len(display_rows) != len(rows):
        print(f'（已筛选显示 {len(display_rows)} / {len(rows)} 个路径）')
    print(f'{len(rows)} 个路径，{total_versions} 个路径内版本'
          f'（全库 {len(unique_blobs)} 个唯一 blob）')
    print(f'各文件合计（共享 blob 已按引用路径均摊）：{human(total_disk)}')
    print(f'全库唯一 blob（去重后）：{human(unique_disk)}'
          f'（解压后 {human(unique_raw)}）')

    git_dir = os.path.join(REPO_ROOT, '.git')
    if os.path.isdir(git_dir):
        actual = dir_disk_usage(git_dir)
        overhead = actual - unique_disk
        print(f'.git 逻辑字节数：{human(actual)}')
        print(f'  其中 blob：{human(unique_disk)}；'
              f'tree/commit/tag/索引等：约 {human(overhead)}')
        block = dir_block_usage(git_dir)
        if block:
            print(f'.git 磁盘块占用：{human(block)}'
                  f'（loose 小文件块对齐会额外占用）')

    # 已删除但仍占空间的文件提示
    dead = [r for r in rows if not r['alive'] and r['disk'] > 0]
    if dead:
        dead_disk = sum(r['disk'] for r in dead)
        print(f'\n历史中存在、当前已删除的路径：{len(dead)} 个，'
              f'仍占用 {human(dead_disk)}')


if __name__ == '__main__':
    main()
