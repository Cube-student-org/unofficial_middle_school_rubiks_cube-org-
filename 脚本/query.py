#!/usr/bin/env python3
"""charter.tsv 的查询入口。

设计
----
库就是 charter.tsv 这一个文件：一条记录一行，八个字段以制表符分隔。
本脚本把整份文件读进内存并建字典索引，之后所有查询都是内存操作 ——
344 条的量级下，全量加载约 1 ms，单次哈希查找约 0.4 μs。

不需要 SQLite，不需要任何第三方库。不装 Python 也能查：
    grep '民主集中制' charter.tsv
    awk -F'\\t' '$7=="9059f8f6"' charter.tsv

用法
----
    python3 脚本/query.py                          # 概览
    python3 脚本/query.py --hash 9059f8f6          # 按内容哈希定位
    python3 脚本/query.py --article 第五十条        # 按中文条号
    python3 脚本/query.py --num 50 --doc 章程       # 按数字条号
    python3 脚本/query.py --grep 民主集中制         # 全文检索
    python3 脚本/query.py --refs 9059f8f6          # 查谁引用了这条
    python3 脚本/query.py --list --doc 规则 --version 严谨
    python3 脚本/query.py --rules --severity 重度   # 处罚规则
    python3 脚本/query.py --export md -o 导出.md    # 还原为 Markdown
"""

import argparse
import csv
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_tsv          # noqa: E402  转义、反转义、源解析
import ensure_db          # noqa: E402  仅用于取 ROOT

ROOT = ensure_db.ROOT
TSV_PATH = os.path.join(ROOT, 'charter.tsv')

COLS = build_tsv.COLS
SRC, FILE, VER, CHAP, NUM, LABEL, HASH, CONTENT = range(8)

REF_RE = re.compile(r'第[一二三四五六七八九十百零\d]+条(?:之[一二三四五六七八九十]+)?')


# ─────────────────────────────────────────────
#  加载
# ─────────────────────────────────────────────

def load(path=TSV_PATH):
    """读入整份 TSV，返回记录列表（保持文件顺序）。"""
    if not os.path.exists(path):
        print(f'ℹ️  {os.path.relpath(path, ROOT)} 不存在，正在生成…', file=sys.stderr)
        build_tsv.main()

    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.rstrip('\n').split('\t')
            if len(parts) != len(COLS):
                raise ValueError(f'第 {lineno} 行字段数 {len(parts)}，应为 {len(COLS)}')
            rows.append(tuple(build_tsv.unesc(x) for x in parts))
    return rows


def build_index(rows):
    """建两张字典：哈希 → 记录列表，条号 → 记录列表。"""
    by_hash, by_label = {}, {}
    for r in rows:
        if r[HASH]:
            by_hash.setdefault(r[HASH], []).append(r)
        by_label.setdefault(r[LABEL], []).append(r)
    return by_hash, by_label


# ─────────────────────────────────────────────
#  输出
# ─────────────────────────────────────────────

def show(rows, full=False):
    if not rows:
        print('（无结果）')
        return
    for r in rows:
        head = r[LABEL] or r[NUM]
        meta = f'{r[SRC]}·{r[VER]}'
        if r[CHAP]:
            meta = f'{r[CHAP]} / {meta}'
        print(f'\n{head}  [{meta}]  #{r[HASH] or "--------"}  {r[FILE]}')
        body = r[CONTENT]
        if not full and len(body) > 600:
            body = body[:600] + '\n…（已截断，--full 查看全文）'
        for line in body.split('\n'):
            print(f'  {line}')


def brief(r):
    preview = r[CONTENT].replace('\n', ' ')
    if len(preview) > 48:
        preview = preview[:48] + '…'
    return f'{r[LABEL]:<14} {r[SRC]}·{r[VER]:<3} #{r[HASH] or "--------"}  {preview}'


def filt(rows, doc, version):
    return [r for r in rows
            if (not doc or r[SRC] == doc) and (not version or r[VER] == version)]


# ─────────────────────────────────────────────
#  子命令
# ─────────────────────────────────────────────

def cmd_overview(rows, by_hash):
    print('charter.tsv 概览')
    print(f'  条文总数    {len(rows)}')
    print(f'  唯一哈希    {len(by_hash)}')
    print(f'  文件体积    {os.path.getsize(TSV_PATH) / 1024:.0f} KB')

    groups = {}
    for r in rows:
        groups[(r[FILE], r[VER])] = groups.get((r[FILE], r[VER]), 0) + 1
    print('\n  按文件分布：')
    for (fname, ver), n in sorted(groups.items()):
        print(f'    {fname:<28} {ver}  {n:>4} 条')

    dual = sum(1 for rs in by_hash.values() if len({r[VER] for r in rs}) > 1)
    print(f'\n  双版本对齐的哈希  {dual} 个')


def cmd_hash(by_hash, hv, full):
    hv = hv.lower().lstrip('#')
    hits = by_hash.get(hv, [])
    print(f'哈希 {hv}：{len(hits)} 条')
    show(hits, full)


def cmd_article(rows, by_label, cn, doc, version, full):
    hits = by_label.get(cn) or [r for r in rows if cn in r[LABEL]]
    hits = filt(hits, doc, version)
    print(f'{cn}：{len(hits)} 条')
    show(hits, full)


def cmd_num(rows, num, doc, version, full):
    target = str(int(num)) if float(num).is_integer() else str(num)
    hits = filt([r for r in rows if r[NUM] == target], doc, version)
    print(f'第 {target} 条：{len(hits)} 条')
    show(hits, full)


def cmd_grep(rows, kw, doc, full):
    hits = filt([r for r in rows if kw in r[CONTENT]], doc, None)
    print(f'含「{kw}」：{len(hits)} 条')
    if full:
        show(hits, True)
    else:
        for r in hits:
            print('  ' + brief(r))


def cmd_refs(rows, by_hash, hv):
    hv = hv.lower().lstrip('#')
    hits = by_hash.get(hv, [])
    if not hits:
        print(f'（未找到哈希 {hv}）')
        return
    label = hits[0][LABEL]

    found = []
    for r in rows:
        if r[HASH] == hv:
            continue
        cited = f'#{hv}' in r[CONTENT]
        if not cited:
            cited = label in {m.group(0) for m in REF_RE.finditer(r[CONTENT])}
        if cited:
            found.append(r)

    print(f'{label}（#{hv}）被引用：{len(found)} 处')
    for r in found:
        print('  ' + brief(r))


def cmd_list(rows, doc, version):
    hits = filt(rows, doc, version)
    print(f'共 {len(hits)} 条\n')
    chapter = None
    for r in hits:
        if r[CHAP] != chapter:
            chapter = r[CHAP]
            print(f'\n【{chapter or "（无章节）"}】')
        print('  ' + brief(r))


def cmd_rules(severity):
    import build_disciplinary as bd
    rules = bd.INITIAL_RULES
    if severity:
        rules = [r for r in rules if r.get('severity') == severity]
    print(f'处罚规则 {len(rules)} 条\n')
    for i, r in enumerate(rules, 1):
        print(f'{i:>3}. [{r.get("category")}/{r.get("severity")}] {r.get("behavior")}')
        print(f'     {r.get("penalty_type")}：{r.get("penalty_detail") or "—"}')
        print(f'     决定：{r.get("deciding_body")}   依据：{r.get("charter_basis") or "—"}')


def cmd_export(rows, fmt, out):
    """把库还原成可供外部消费的文本（交换用，权威源仍是 Markdown）。"""
    if fmt == 'md':
        buf, chapter, last_file = [], None, None
        for r in rows:
            if (r[FILE], r[VER]) != last_file:
                last_file = (r[FILE], r[VER])
                title = r[FILE].rsplit('.', 1)[0]
                buf.append(f'\n\n# {title}（{r[VER]}）\n')
                chapter = None
            if r[CHAP] != chapter:
                chapter = r[CHAP]
                buf.append(f'\n## {chapter}\n')
            tag = f'**{r[LABEL]}' + (f'#{r[HASH]}' if r[HASH] else '') + '**'
            buf.append(f'\n{tag}{r[CONTENT]}\n')
        text = ''.join(buf).lstrip('\n')

    elif fmt == 'csv':
        sio = io.StringIO()
        w = csv.writer(sio, lineterminator='\n')
        w.writerow(['source', 'file', 'version', 'chapter',
                    'num', 'label', 'hash', 'content'])
        for r in rows:
            w.writerow([r[SRC], r[FILE], r[VER], r[CHAP],
                        r[NUM], r[LABEL], r[HASH], r[CONTENT]])
        text = sio.getvalue()

    elif fmt == 'jsonl':
        lines = []
        for r in rows:
            lines.append(json.dumps({
                'source': r[SRC], 'file': r[FILE], 'version': r[VER],
                'chapter': r[CHAP], 'num': r[NUM], 'label': r[LABEL],
                'hash': r[HASH], 'content': r[CONTENT],
            }, ensure_ascii=False, separators=(',', ':')))
        text = '\n'.join(lines) + '\n'

    else:
        print(f'❌ 不支持的格式：{fmt}（可选 md / csv / jsonl）')
        return 1

    if out:
        with open(out, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        print(f'✅ 已导出 {len(rows)} 条 → {out}（{len(text.encode()) / 1024:.0f} KB）')
    else:
        sys.stdout.write(text)
    return 0


# ─────────────────────────────────────────────
#  入口
# ─────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description='charter.tsv 查询入口（只读，权威源仍为 Markdown）')
    p.add_argument('--hash', help='按内容哈希定位（8 位十六进制）')
    p.add_argument('--article', help='按中文条号，如 第五十条 / 第一条之一')
    p.add_argument('--num', type=float, help='按数字条号，如 50 / 12.1')
    p.add_argument('--grep', help='正文全文检索')
    p.add_argument('--refs', help='查谁引用了该哈希对应的条文')
    p.add_argument('--list', action='store_true', help='列目录')
    p.add_argument('--rules', action='store_true', help='列处罚规则')
    p.add_argument('--severity', help='配合 --rules，按严重程度过滤')
    p.add_argument('--doc', choices=['章程', '规则'], help='限定文件类型')
    p.add_argument('--version', choices=['严谨', '通俗'], help='限定版本')
    p.add_argument('--export', choices=['md', 'csv', 'jsonl'], help='导出为文本')
    p.add_argument('-o', '--out', help='导出目标路径（缺省打到 stdout）')
    p.add_argument('--full', action='store_true', help='显示完整正文')
    a = p.parse_args()

    rows = load()
    by_hash, by_label = build_index(rows)

    if a.export:
        return cmd_export(rows, a.export, a.out)
    if a.hash:
        cmd_hash(by_hash, a.hash, a.full)
    elif a.article:
        cmd_article(rows, by_label, a.article, a.doc, a.version, a.full)
    elif a.num is not None:
        cmd_num(rows, a.num, a.doc, a.version, a.full)
    elif a.grep:
        cmd_grep(rows, a.grep, a.doc, a.full)
    elif a.refs:
        cmd_refs(rows, by_hash, a.refs)
    elif a.list:
        cmd_list(rows, a.doc, a.version)
    elif a.rules:
        cmd_rules(a.severity)
    else:
        cmd_overview(rows, by_hash)
    return 0


if __name__ == '__main__':
    sys.exit(main())
