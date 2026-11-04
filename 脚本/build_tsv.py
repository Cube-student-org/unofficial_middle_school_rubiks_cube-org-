#!/usr/bin/env python3
"""从 Markdown 源生成单文件条文库 charter.tsv。

为什么是 TSV 而不是 CSV
----------------------
条文正文是 Markdown，含大量换行。RFC 4180 的 CSV 允许引号内换行，
结果是「一条记录 ≠ 一行」，grep / awk / wc -l / git diff 全部失效。
TSV 把正文内的 换行/制表符/反斜杠 转义为字面量，保证一条记录严格一行。

转义约定（三对，可逆）
    \\   → 反斜杠
    \t   → 制表符
    \n   → 换行

解码必须单遍扫描，不能用 replace 链：
    原文 `\n`（反斜杠+n）转义后是 `\\n`，用 replace('\\n','\n') 会先把它
    吃成「反斜杠 + 换行」，信息就丢了。

用法
----
    python3 脚本/build_tsv.py            # 生成 charter.tsv
    python3 脚本/build_tsv.py -o x.tsv   # 指定输出
    python3 脚本/build_tsv.py --check    # 只校验现有文件是否与源一致
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ensure_db                     # noqa: E402
import import_charter as ic          # noqa: E402

ROOT = ensure_db.ROOT
OUT = os.path.join(ROOT, 'charter.tsv')

COLS = ['source', 'file', 'version', 'chapter', 'num', 'label', 'hash', 'content']

HEADER = '''\
# charter.tsv —— 单文件条文库（由 脚本/build_tsv.py 从 文档/规范性文件/ 派生）
# 源指纹 {fp}  共 {n} 条
# 列：{cols}
# 约定：content 内换行写作字面量 \\n，制表符写作 \\t，反斜杠写作 \\\\；一条记录严格占一行
# 读法（无需任何依赖）：
#   python3 -c "rows=[l.rstrip('\\n').split('\\t') for l in open('charter.tsv',encoding='utf-8') if not l.startswith('#')];print(len(rows))"
'''


def esc(s):
    """原文 → 单行安全文本。顺序不可调换：反斜杠必须最先处理。"""
    return (s.replace('\\', '\\\\')
             .replace('\t', '\\t')
             .replace('\r', '')
             .replace('\n', '\\n'))


def unesc(s):
    """单行安全文本 → 原文。单遍扫描，避免 `\\\\n` 被误拆。"""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt == 'n':
                out.append('\n'); i += 2; continue
            if nxt == 't':
                out.append('\t'); i += 2; continue
            if nxt == '\\':
                out.append('\\'); i += 2; continue
        out.append(c)
        i += 1
    return ''.join(out)


def collect():
    """从全部 Markdown 源解析条文，返回按 (source, version, num) 排序的行。"""
    rows = []
    for path in ensure_db.source_files():
        source, version = ensure_db.infer_source_version(path)
        fname = os.path.basename(path)
        for chapter, num, label, hv, content in ic.parse_file(path):
            rows.append((source, fname, version, chapter,
                         str(num), label, hv or '', content))
    # 排序：先按文件（章/规则 → 具体文件），再版本（严谨在前），最后条号
    rows.sort(key=lambda r: (r[0], r[1], r[2] != '严谨', _numkey(r[4])))
    return rows


def _numkey(num):
    try:
        return float(num)
    except ValueError:
        return 0.0


def render(rows, fp):
    lines = [HEADER.format(fp=fp, n=len(rows), cols='  '.join(COLS)).rstrip('\n')]
    for r in rows:
        lines.append('\t'.join(esc(str(x)) for x in r))
    return '\n'.join(lines) + '\n'


def main():
    args = sys.argv[1:]
    out = OUT
    if '-o' in args:
        out = args[args.index('-o') + 1]

    files = ensure_db.source_files()
    if not files:
        print('❌ 未找到源文件')
        return 1
    fp = ensure_db.fingerprint(files)
    rows = collect()
    text = render(rows, fp)

    if '--check' in args:
        if not os.path.exists(out):
            print(f'❌ {os.path.relpath(out, ROOT)} 不存在')
            return 1
        current = open(out, 'r', encoding='utf-8').read()
        if current == text:
            print(f'✅ 与源一致：{len(rows)} 条')
            return 0
        print('⚠️  与源不一致，需要重新生成')
        return 1

    with open(out, 'w', encoding='utf-8', newline='') as f:
        f.write(text)

    size_kb = len(text.encode('utf-8')) / 1024
    print(f'✅ 已生成 {os.path.relpath(out, ROOT)}：{len(rows)} 条，{size_kb:.0f} KB')
    print(f'   源指纹 {fp[:16]}…')
    return 0


if __name__ == '__main__':
    sys.exit(main())
