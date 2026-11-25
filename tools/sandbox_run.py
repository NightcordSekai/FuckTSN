#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""沙盘端到端跑批：不需要账号密码，把 `app/backfill.py` 整条链路真跑一遍。

它和 `python run.py backfill ...` 的差别只有一个：
`make_client()` 里那个会真正登录的 SDK 被换成了「先挂沙盘、再塞沙盘 token」的版本。
其余全部是仓库原有代码——`build_run`、`compliance.check_submission`、`submit`、
台账、`plan.json` 一个字都没改。

用法
----
    cd work
    python tools/sandbox_run.py                        # 3 条，写 logs/ + plan.json
    python tools/sandbox_run.py --count 5 --dates 2026-10-02,2026-10-05
    python tools/sandbox_run.py --reject-demo          # 额外证明"判定真的在判"

产物
----
    logs/sandbox_report.json   沙盘侧台账（收到的每个请求 + 每条记录判定）
    logs/backfill_done.json    客户端台账（只有合格记录才写）
    plan.json                  客户端规划（每条点串 + 步数分段 + meta）
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import backfill                                    # noqa: E402
from app import config                                      # noqa: E402
from app.device import deviceModel                          # noqa: E402
from sdk.public import TiShiNengSdkPublic                   # noqa: E402
from tools.tsn_sandbox import Sandbox, make_face_jpeg       # noqa: E402

SANDBOX_USER = 'sandbox-user'
SANDBOX_PASS = 'sandbox-pass'


def build_sandbox() -> Sandbox:
    """把服务端那套 setting 一次性备好（含围栏，避免客户端自己兜底）。"""
    lat, lng = backfill._cfg_center()
    fence = backfill._circle_fence(lat, lng, backfill.DEFAULT_FENCE_RADIUS_M)
    setting = dict(backfill.DEFAULT_SETTING)
    setting.update({'totalRange': '2.000', 'startQualifiedTime': 6, 'endQualifiedTime': 21})
    return Sandbox(setting, fence=fence)


def patch_make_client(box: Sandbox) -> None:
    """替换 `backfill.make_client()`：不读 account.txt、不联网，只装配沙盘。"""

    async def make_client() -> TiShiNengSdkPublic:
        cfg = config.load()
        tsn = TiShiNengSdkPublic(
            1, int(cfg.get('school_id') or 0), str(cfg.get('school_code') or ''), '',
            str(uuid.uuid4()), deviceModel.brand, deviceModel.model,
            deviceModel.osver, '', deviceModel.a_list)
        box.attach(tsn)                      # 必须在第一个请求之前
        tsn.setCloudUrl(str(cfg.get('base_url') or ''))
        tok = await tsn.getAccessToken(SANDBOX_USER, SANDBOX_PASS)
        return backfill._apply_token(tsn, tok)

    backfill.make_client = make_client


RUN_LOG: list[dict] = []


def patch_build_run() -> None:
    """顺手记下每一次 `build_run` 的产物。

    `app/backfill.py` 只在 `--dry-run` 分支里累计 `planned` 并写 `plan.json`；
    走 `--submit` 时那条分支被 `continue` 跳过了。这里包一层，用**同一份**
    `build_run` 结果落盘，保证 `plan.json` 与真正提交出去的点串逐字节一致。
    """

    original = backfill.build_run

    def build_run(day, ref, setting, rng):          # noqa: ANN001, ANN202
        run = original(day, ref, setting, rng)
        RUN_LOG.append({'day': day.isoformat(), 'points': run['points'],
                        'stepNumbers': run['segs'], 'meta': run['meta']})
        return run

    backfill.build_run = build_run


def write_plan() -> str | None:
    """把本轮所有 run 写成 `plan.json`（与 `--dry-run` 产物同构）。"""
    if not RUN_LOG:
        return None
    out = Path(config.load().get('plan_file') or 'plan.json')
    payload = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'generator': 'tools/sandbox_run.py',
        'records': sorted(RUN_LOG, key=lambda r: r['day']),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return str(out.resolve())


def run_progress() -> None:
    """用同一个沙盘跑 `progress.py`，证明只读链路也只吃到沙盘数据。"""
    import argparse as _argparse
    from app import progress
    print('\n--- progress.py（同一沙盘）---')
    ns = _argparse.Namespace(json=True,
                             plan=str(config.load().get('plan_file') or 'plan.json'))
    try:
        asyncio.run(progress.report(ns))
    except SystemExit:
        raise
    except Exception as exc:                     # noqa: BLE001
        print(f'progress 中断：{type(exc).__name__}: {exc}')


def ensure_face() -> str:
    """face_images/ 为空时合成一张，避免 `--no-face` 被判定链路判成不完整。"""
    root = Path(backfill.FACE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    for suffix in ('.jpg', '.jpeg', '.png'):
        hit = sorted(root.glob(f'*{suffix}'))
        if hit:
            return str(hit[0])
    return make_face_jpeg(str(root / 'face.jpg'))


def run_backfill(extra: list[str]) -> int:
    argv = sys.argv
    sys.argv = [str(ROOT / 'app' / 'backfill.py')] + extra
    try:
        asyncio.run(backfill.main())
        return 0
    finally:
        sys.argv = argv


def main() -> int:
    parser = argparse.ArgumentParser(description='TSN 本地沙盘端到端跑批')
    parser.add_argument('--count', type=int, default=3, help='生成的记录条数')
    parser.add_argument('--dates', default='', help='逗号分隔的 YYYY-MM-DD')
    parser.add_argument('--start', default='', help='日期范围起点（含）')
    parser.add_argument('--end', default='', help='日期范围终点（含）')
    parser.add_argument('--seed', type=int, default=20261125, help='固定随机种子，结果可复现')
    parser.add_argument('--no-face', action='store_true', help='不上传人脸（沙盘会判不合格）')
    parser.add_argument('--reject-demo', action='store_true',
                        help='额外跑一轮故意违规的提交，证明判定链路真的在判')
    parser.add_argument('--report', default='logs/sandbox_report.json')
    parser.add_argument('--no-progress', action='store_true',
                        help='跳过 progress.py 只读盘点演示')
    args = parser.parse_args()

    os.chdir(ROOT)
    os.environ.setdefault(backfill.NO_WAIT_ENV, '1')      # 跳过窗口等待，跑得快
    face_path = ensure_face()
    print(f'沙盘：人脸素材 {face_path}')

    box = build_sandbox()
    patch_make_client(box)
    patch_build_run()
    RUN_LOG.clear()

    extra: list[str] = ['--submit', '--count', str(args.count), '--seed', str(args.seed),
                        '--force']
    if args.dates:
        extra += ['--dates', args.dates]
    if args.start:
        extra += ['--start', args.start]
    if args.end:
        extra += ['--end', args.end]
    if args.no_face:
        extra += ['--no-face']

    code = run_backfill(extra)
    plan_path = write_plan()
    report = box.dump(str(ROOT / args.report))
    print(f"\n沙盘收包：{report['counters']}")
    print(f"沙盘判定：{report['quota']['records']} 条 / {report['quota']['range_km']} km")
    if plan_path:
        print(f'计划已写出 {plan_path}（{len(RUN_LOG)} 天，与提交点串逐字节一致）')
    if not args.no_progress:
        run_progress()
    if args.reject_demo:
        reject_demo(box)
    return code


def reject_demo(box: Sandbox) -> None:
    """用**同一个**沙盘服务端跑必拒用例，证明判定不是摆设。"""
    from tools.tsn_sandbox import Judge
    judge = Judge(box.setting)
    env_json = _real_env()
    good = {
        'identify': str(uuid.uuid4()),
        'sportRange': '2.160', 'gpsDistance': '2.160', 'step': 2200,
        'sportTime': 980, 'speed': '7′32″', 'environment': env_json,
    }
    cases = [
        ('里程贴线 2.000（真实被拒）', dict(good, sportRange='2.000', gpsDistance='2.000')),
        ('步数 999（真实被拒）', dict(good, step=999)),
        ('identify 不是 UUID（真实被拒）', dict(good, identify='1')),
        ('时长 22 分钟（超窗）', dict(good, sportTime=1320)),
        ('配速格式异常', dict(good, speed='10.44')),
        ('环境画像缺失', dict(good, environment='MISSING')),
        ('缺人脸 type=3', None),
    ]
    print('\n=== 判定链路反证（同一沙盘服务端）===')
    for title, fields in cases:
        if fields is None:
            verdict = judge.judge(good, {1, 2})
        else:
            verdict = judge.judge(fields, {1, 2, 3})
        print(f"  {title:<26} → status={verdict['status']} "
              f"reachRange={verdict['reachRange']} remark={verdict['remark']}")
    ok = judge.judge(good, {1, 2, 3})
    print(f"  {'合规样本（对照）':<26} → status={ok['status']} "
          f"reachRange={ok['reachRange']} remark={ok['remark']}")
    _diagnose_upper_bounds(judge, good)


def _diagnose_upper_bounds(judge, good: dict) -> None:
    """两条上限 + 用真实合格记录回放，证明本地闸门既不过严也不过宽。"""
    from tools.tsn_sandbox import Judge
    print('\n=== 真实「合格」记录回放（服务器判合格 → 沙盘必须也放行）===')
    real = _real_qualified_samples()
    for cid, fields, setting in real:
        row_judge = Judge(dict(judge.setting, **setting)) if setting else judge
        v = row_judge.judge(fields, {1, 2, 3})
        ck = v.get('checks') or {}
        flag = 'OK ' if not v['reject'] else 'BAD'
        print(f"  [{flag}] {cid:<20} chain/step={ck.get('chain_per_step')} "
              f"60s 最差={ck.get('worst_window_per_step')} → {v['remark']}")
    print('\n=== 上限两条：只诊断，不拒（真实合格记录全部违反它们）===')
    v = judge.judge(dict(good, step=1201, gitudeLatitude=_window_stride_pts()), {1, 2, 3})
    ck = v.get('checks') or {}
    print(f"  合成极端样本 chain/step={ck.get('chain_per_step')} "
          f"60s 最差={ck.get('worst_window_per_step')} "
          f">2? {ck.get('chain_over_2x_steps')} >2.5? {ck.get('worst_window_over_limit')}"
          f" → {v['remark']}（诊断值拉满但不拦）")


def _real_env() -> str:
    from tsn import env as tsn_env
    return json.dumps(tsn_env.build_environment_payload('sandbox', tsn_env.clean_device_signals(),
                                                         ['arm64-v8a']), ensure_ascii=False)


def _real_qualified_samples() -> list:
    """`tests/fixtures/records_qualified.json` 里 5 条服务器判「合格」的真实记录。

    沙盘判定必须能吃下它们 —— 任何一条被拒，说明本地闸门加错了。
    每条记录自带当时的 setting（`rangeSetting`/`stepSetting`/`startQualifiedTimeSetting`
    /`endQualifiedTimeSetting`），回放时要用**它自己的**，不能套今天的沙盘 setting。
    """
    path = ROOT / 'tests' / 'fixtures' / 'records_qualified.json'
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding='utf-8'))
    rows = raw.values() if isinstance(raw, dict) else raw
    env_json = _real_env()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        points = row.get('gitudeLatitude')
        if not isinstance(points, str):
            points = json.dumps(points or [], ensure_ascii=False)
        setting = {}
        for key in ('rangeSetting', 'stepSetting', 'totalRange', 'isStepRecursion'):
            if row.get(key) is not None:
                setting[key] = row[key]
        # 记录里回显的是 `startQualifiedTimeSetting` / `endQualifiedTimeSetting`，
        # 而 Judge 读的是 `startQualifiedTime` / `endQualifiedTime`。
        # 取证：9156722380507752559 的 sportTime=1347s（22.45 分钟）> 21，但它 eT=41 ——
        # 服务端确实按超圈比例把窗口放大过（21 × 4.01/2.0 ≈ 42），所以这里必须用记录自带的。
        for src, dst in (('startQualifiedTimeSetting', 'startQualifiedTime'),
                         ('endQualifiedTimeSetting', 'endQualifiedTime')):
            if row.get(src) is not None:
                setting[dst] = row[src]
        out.append((str(row.get('id') or ''), {
            'identify': str(row.get('identify') or uuid.uuid4()),
            'sportRange': str(row.get('sportRange') or '0'),
            'gpsDistance': str(row.get('gpsDistance') or row.get('sportRange') or '0'),
            'step': int(float(row.get('step') or 0)),
            'sportTime': int(float(row.get('sportTime') or 0)),
            'speed': str(row.get('speed') or ''),
            'gitudeLatitude': points,
            'environment': env_json,
        }, setting))
    return out


def _chain_pts(meters: float, steps_from: int, steps_to: int) -> str:
    """造一条 `meters` 米、跨越 `steps_from→steps_to` 步的两点点串（用于上限反证）。"""
    lat0, lon0 = 23.523300, 113.611400
    dlat = meters / 111_320.0
    def point(lat, e, t):
        return {'a': f'{lat:.6f}', 'o': f'{lon0:.6f}', 'e': str(e), 't': str(t),
                'c': '1', 'i': 'true', 'l': '1', 'm': '0', 's': '0'}
    return json.dumps([point(lat0, steps_from, 1700000000000),
                       point(lat0 + dlat, steps_to, 1700000060000)])


def _window_stride_pts() -> str:
    """整条链长只有 50m（远低于 2×步数），但最后 60s 段内 1 步走了 50m。

    对应帮助中心「某一时间段步幅超出正常范围」——整条均值骗得过去，分段骗不过去。
    前 121 点原地踏步（e 每秒 +10），最后 1 点在同一个 60s 窗口里暴走 50m、e 只 +1。
    """
    lat0, lon0 = 23.523300, 113.611400
    base = 1_700_000_000_000
    def point(lat, e, t):
        return {'a': f'{lat:.6f}', 'o': f'{lon0:.6f}', 'e': str(e), 't': str(t),
                'c': '1', 'i': 'true', 'l': '1', 'm': '0', 's': '0'}
    pts = [point(lat0, i * 10, base + i * 1000) for i in range(121)]   # e: 0..1200，原地
    pts.append(point(lat0 + 50.0 / 111_320.0, 1201, base + 121_000))  # 同窗口 (+1 步 / +50m)
    return json.dumps(pts)


if __name__ == '__main__':
    raise SystemExit(main())
