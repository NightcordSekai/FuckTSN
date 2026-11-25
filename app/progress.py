#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""学期进度报告（只读，不改任何服务端状态）。

移植自老仓库 ``progress.py``，硬编码的学校/账号走 :mod:`config`，其余口径不变：

一次登录，拉齐 ``sumExerciseRecord`` / ``statisticsExerciseRecord`` /
全部记录（合格 + 不合格），输出

1. 阳光跑还差几次、累计多少公里、按当前节奏什么时候能跑完；
2. 已计入（合格）记录清单：日期 / 里程 / 步数 / 配速；
3. 被处罚 / 无效记录清单：日期 / 原因；
4. 与 ``plan.json`` 交叉核对：计划里哪几天服务端其实已经有记录（防重复上传）。

用法::

    python progress.py
    python progress.py --json      # 同时落盘 logs/progress.json

只读：本脚本不会调用任何写接口（不加人脸、不建记录）。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 只用 reconfigure()：`sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 会接管底层
# buffer，wrapper 被回收时关闭捕获流（pytest 下报 `ValueError: I/O operation on closed file`）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        except (ValueError, OSError):
            pass

from app import backfill  # noqa: E402
from app import config  # noqa: E402

NEED = 20  # 阳光跑学期要求（sumExerciseRecord.validitySunRun 实测 = 20）


def _rows(pages) -> list[dict]:
    """服务端对 datePageIndex 不敏感（每页都回同一批），必须按 id 去重。"""
    out, seen = [], set()
    for page in pages or []:
        if isinstance(page, list):          # 扁平数组形态（listExerciseRecord 实测）
            rows = page
        elif isinstance(page, dict):
            rows = page.get('records') or page.get('list') or []
        else:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get('id') or (row.get('startTime'), row.get('sportRange'))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def _fmt(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ts) / 1000).strftime('%Y-%m-%d')
    except (TypeError, ValueError, OSError):
        return str(ts)


async def collect(tsn) -> dict:
    data = {}
    data['sum'] = await tsn.sumExerciseRecord()
    try:
        data['stat'] = await tsn.statisticsExerciseRecord()
    except Exception as exc:  # noqa: BLE001
        data['stat'] = {'__error__': f'{type(exc).__name__}: {exc}'}
    for status in (1, 0):
        pages = []
        for idx in (1, 2, 3, 4, 5):
            try:
                page = await tsn.listExerciseRecord(runStatus=status, datePageIndex=idx)
            except Exception as exc:  # noqa: BLE001
                pages.append({'__error__': f'{type(exc).__name__}: {exc}'})
                break
            pages.append(page)
            if not isinstance(page, dict) or not (page.get('records') or page.get('list')):
                break
        data[f'r{status}'] = pages
    return data


def _plan_days(path: str) -> list[dt.date]:
    """计划文件既可能是 ["2026-11-01", ...]，也可能是 {'records': [{'day': ...}]}。"""
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get('records') or data.get('days') or []
    out = []
    for item in data or []:
        raw = item.get('day') if isinstance(item, dict) else item
        try:
            out.append(dt.date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    return sorted(set(out))


async def report(args) -> int:
    acct = config.get('account_file') or 'account.txt'
    if not os.path.exists(acct):
        print(f'⚠️ 缺少账号文件 {acct}（第一行账号、第二行密码），无法登录。')
        print('   这一步会停在「读账号文件」，没有发出任何网络请求。')
        print('   准备方式：在 config.json 里把 account_file 指向账号文件，'
              '或用 TSN_CONFIG 指定另一份配置。')
        return 2

    try:
        tsn = await backfill.make_client()
    except Exception as exc:  # noqa: BLE001
        print(f'⚠️ 登录失败，停在 make_client：{type(exc).__name__}: {str(exc)[:120]}')
        return 2

    data = await collect(tsn)
    sun = (data.get('sum') or {}).get('sunRun') or {}
    succeed = int(sun.get('succeed') or 0)
    km = float(sun.get('runSportRange') or 0)
    ok_rows = _rows(data.get('r1'))
    bad_rows = _rows(data.get('r0'))

    print('=' * 68)
    print(f'阳光跑进度：{succeed} / {NEED} 次    累计 {km:.2f} km    '
          f'还需 {max(0, NEED - succeed)} 次')
    print('=' * 68)

    print('\n【已计入】')
    for row in sorted(ok_rows, key=lambda x: str(x.get('startTime'))):
        print(f'  {_fmt(row.get("startTime"))}  {row.get("sportRange")!s:>6} km  '
              f'步数 {str(row.get("step")):>5}  {row.get("speed")}  {row.get("remark") or ""}')
    if not ok_rows:
        print('  （空）')

    print('\n【被处罚 / 无效】')
    for row in sorted(bad_rows, key=lambda x: str(x.get('startTime'))):
        print(f'  {_fmt(row.get("startTime"))}  {row.get("sportRange")!s:>6} km  '
              f'{row.get("remark") or ""}')
    if not bad_rows:
        print('  （空）')

    plan = _plan_days(args.plan)
    if plan:
        have = {_fmt(row.get('startTime')) for row in ok_rows + bad_rows}
        clash = [day.isoformat() for day in plan if day.isoformat() in have]
        print(f'\n计划 {len(plan)} 天，其中 {len(clash)} 天服务端已有记录：{clash or "无"}')
        if clash:
            print('  ⚠️ 这些日期先别提，重复上传可能被判「不允许重复上传」')
        left = max(0, NEED - succeed)
        if left:
            today = dt.date.today()
            print(f'\n还需 {left} 次。若从明天起每天 1 次，最早 '
                  f'{today + dt.timedelta(days=left)} 完成；'
                  f'若隔天 1 次，最早 {today + dt.timedelta(days=left * 2)} 完成。')

    if args.json:
        os.makedirs('logs', exist_ok=True)
        with open(os.path.join('logs', 'progress.json'), 'w', encoding='utf-8') as fh:
            json.dump({'sum': data.get('sum'), 'ok': ok_rows, 'bad': bad_rows},
                      fh, ensure_ascii=False, indent=1)
        print('\n已写出 logs/progress.json')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog='progress.py', description='学期进度报告（只读）')
    ap.add_argument('--json', action='store_true', help='同时落盘 logs/progress.json')
    ap.add_argument('--plan', default='', help='计划文件（默认取 config.json 的 plan_file）')
    args = ap.parse_args()
    args.plan = args.plan or (config.get('plan_file') or 'plan.json')
    return asyncio.run(report(args))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
