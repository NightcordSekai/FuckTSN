#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按省份批量接管：一所一所学校各自独立跑完整条提交链。

为什么需要它
------------
`tools/sandbox_run.py` 一次只认 `config.json` 里的**一所**学校
（`backfill.make_client()` 只读一份 `school_id`/`school_code`）。
想验证「广东省 106 所」就必须按学校逐校装配：每校一个独立 `Sandbox`
（各自的人脸记录、各自的去重表、各自的配额），互不干扰。

线路从哪来
----------
仓库里 `reference.json` 只有一条真实线路（广东从化一带，1134 点），
所以全省 106 所都复用这条线路。这不影响判定：服务端只看
sportRange / step / sportTime / 人脸 / identify / environment，
不看点在哪个省。真要一所校一条线，把该校的 `reference.json` 放进
`references/<school_code>.json` 就会自动优先使用（见 `school_reference()`）。

用法
----
    cd work
    python tools/sandbox_province.py --province 广东省            # 全省 106 所
    python tools/sandbox_province.py --province 广东省 --count 1   # 每校 1 条，跑得快
    python tools/sandbox_province.py --codes gzccc,sgu,scnu        # 指定几所
    python tools/sandbox_province.py --province 广东省 --limit 10  # 先试 10 所
    python tools/sandbox_province.py --province 广东省 --reject-demo

产物
----
    logs/sandbox_province.json   每校汇总：条数 / 里程 / 驳回原因 / 收包计数
    logs/province/<code>.json    单校明细（= sandbox_run.py 的 report 格式）
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import random
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select                                # noqa: E402

from app import backfill                                     # noqa: E402
from app import config                                       # noqa: E402
from app.database import get_db, init_db                     # noqa: E402
from app.device import deviceModel                           # noqa: E402
from app.models import TsnSchool_Model                       # noqa: E402
from tools.tsn_sandbox import Sandbox, make_face_jpeg        # noqa: E402

SANDBOX_USER = 'sandbox-user'
SANDBOX_PASS = 'sandbox-pass'      # 沙盘不校验密码，只要非空
REFERENCE_DIR = 'references'


# --------------------------------------------------------------------------- #
# 学校清单
# --------------------------------------------------------------------------- #

async def load_schools(province: str = '', codes: str = '',
                       sys_type: int = 0, limit: int = 0) -> list[dict]:
    """从本地库 `tsn_school` 取学校；不联网。"""
    await init_db()
    rows: list[dict] = []
    async for db in get_db():
        stmt = select(TsnSchool_Model)
        if province:
            stmt = stmt.where(TsnSchool_Model.province_name == province)
        if sys_type:
            stmt = stmt.where(TsnSchool_Model.sys_type == sys_type)
        if codes:
            wanted = {c.strip() for c in codes.split(',') if c.strip()}
            stmt = stmt.where(TsnSchool_Model.school_code.in_(wanted))
        stmt = stmt.order_by(TsnSchool_Model.school_id)
        for s in (await db.execute(stmt)).scalars().all():
            rows.append({'school_id': s.school_id, 'school_code': s.school_code,
                         'school_name': s.school_name, 'sys_type': s.sys_type,
                         'lan_url': s.lan_url, 'province_name': s.province_name})
        break
    if limit > 0:
        rows = rows[:limit]
    return rows


def school_reference(code: str) -> str:
    """该校专属参考线路优先；没有就回落到仓库里的 reference.json。"""
    local = Path(REFERENCE_DIR) / f'{code}.json'
    if local.is_file():
        return str(local)
    return str(Path(backfill.REFERENCE_FILE))


# --------------------------------------------------------------------------- #
# 单校跑批
# --------------------------------------------------------------------------- #

def build_setting_for(school: dict, points: list, ref_rec: dict | None = None) -> tuple[dict, list]:
    """该校的 (setting, judge_fence)：合成围栏与判定围栏必须分开。

    这里踩过一个自指的坑：`build_run → _pick_path` 会用
    `setting['geofence']` 当**合成环**铺路，而合成器会按环外扩约
    1.09~1.18 倍。若拿同一个圆既当合成环又当判定环，就必然有
    15%~18% 的点被判围栏外（>10% 是致命项），无论半径取多少都成立。

    实测（reference.json 真实 4 环，20 个随机种子）：
        合成环 = 圆90m  → 判定用真实环：合格 10/20，外部比 0.28
        合成环 = 圆110m → 判定用真实环：合格  0/20，外部比 0.62
        合成环 = 真实环 → 判定用圆200m：合格 20/20，外部比 0.00
        合成环 = 真实环 → 判定用圆300m：合格 20/20，外部比 0.00

    所以：合成用真实环（96.6m 外接半径，与仓库里的线路同源），
    判定/提交用线路外接半径 ×3（下限 200m）的大圆。
    """
    setting = dict(backfill.DEFAULT_SETTING)
    setting.update({'totalRange': '2.000', 'startQualifiedTime': 6,
                    'endQualifiedTime': 21, 'stepSetting': 1000})
    lat, lon = _route_center(points)

    synthesis = backfill._norm_fence((ref_rec or {}).get('geofence'))
    setting['geofence'] = synthesis or backfill._circle_fence(
        lat, lon, _route_radius(points))

    judge = backfill._circle_fence(lat, lon, _judge_radius(points))
    return setting, judge


def _route_center(points: list) -> tuple[float, float]:
    lats = [p[1] for p in points]
    lons = [p[0] for p in points]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _route_radius(points: list) -> float:
    """线路外接半径（米）：质心到最远参考点的距离。"""
    import math
    lat0, lon0 = _route_center(points)
    kx = backfill._M_PER_DEG_LAT * math.cos(math.radians(lat0))
    worst = 0.0
    for lon, lat in points:
        dy = (lat - lat0) * backfill._M_PER_DEG_LAT
        dx = (lon - lon0) * kx
        worst = max(worst, math.hypot(dx, dy))
    return worst


def _judge_radius(points: list) -> float:
    """判定/提交围栏半径（米）：必须显著大于合成轨迹的外扩半径。

    合成环跑出来的点最远约 112m（线路外接 96.6m），判定圆取
    外接半径 ×3（下限 200m）实测 0 点越界、20/20 合格。
    """
    return max(200.0, _route_radius(points) * 3.0)


def patch_make_client(box: Sandbox, school: dict):
    """把 `backfill.make_client()` 钉死到这一所学校 + 这个沙盘。"""

    async def make_client():
        from sdk.public import TiShiNengSdkPublic
        cfg = config.load()
        tsn = TiShiNengSdkPublic(
            1, int(school['school_id']), str(school['school_code']), '',
            str(uuid.uuid4()), deviceModel.brand, deviceModel.model,
            deviceModel.osver, '', deviceModel.a_list)
        box.attach(tsn)
        # 每所学校在真实网络里是不同的 netloc，这里保留该形态（沙盘只看 path）
        tsn.setCloudUrl(school.get('lan_url') or str(cfg.get('base_url') or ''))
        tok = await tsn.getAccessToken(SANDBOX_USER, SANDBOX_PASS)
        return backfill._apply_token(tsn, tok)

    backfill.make_client = make_client


def school_rng(seed: int, school: dict) -> random.Random:
    """每校独立的 RNG：种子必须混入 school_code/school_id。

    只写 `random.Random(seed)` 的话，106 所学校会跑出**同一份轨迹**
    （实测全省 106 条提交的点串指纹只有 1 个）。混入学校标识后仍然
    完全可复现：同一 `--seed` + 同一学校 = 同一条记录。
    """
    return random.Random(f'{seed}:{school["school_code"]}:{school["school_id"]}')


async def run_school(school: dict, count: int, seed: int, no_face: bool,
                     dry_run: bool) -> dict:
    """一所学校跑完 `build_run → 门禁 → 人脸 → 提交 → 回读`。"""
    ref_points, ref_rec = backfill.load_reference()
    setting, fence = build_setting_for(school, ref_points, ref_rec)
    box = Sandbox(setting, fence=fence, verbose=False)
    patch_make_client(box, school)

    # 每校的种子要混入 school_code/school_id（见 school_rng 的说明），
    # 否则 106 所会跑出**同一份轨迹**。混入后仍然完全可复现。
    rng = school_rng(seed, school)
    today = datetime.date.today()
    days = backfill.plan_dates(count, None, None, True, 1)
    del today

    out = {'school_id': school['school_id'], 'school_code': school['school_code'],
           'school_name': school['school_name'], 'netloc': school.get('lan_url'),
           'ok': 0, 'fail': 0, 'km': 0.0, 'rows': [], 'counters': None,
           'error': ''}
    try:
        tsn = await backfill.make_client()
        for day in days:
            run = backfill.build_run(day, ref_points, setting, rng)
            report = backfill.compliance.check_submission(
                run['points'], run['segs'], run['meta'], fence=fence, history=[])
            if not report.ok:
                out['fail'] += 1
                out['rows'].append({'day': day.isoformat(), 'ok': False,
                                    'reason': '; '.join(i.code for i in report.errors)})
                continue
            if dry_run:
                out['ok'] += 1
                out['km'] += run['dist'] / 1000.0
                out['rows'].append({'day': day.isoformat(), 'ok': True, 'dry_run': True,
                                    'reach': None})
                continue
            res = await backfill.submit(tsn, run, setting, fence, not no_face)
            # 注意键名：`submit()` 回写的是 `sportStatus`（驼峰），不是 `sport_status`
            good = (str(res.get('sportStatus') or '') == '1'
                    and '黑名单' not in str(res.get('remark') or '')
                    and res.get('face_ok'))
            if good:
                out['ok'] += 1
                out['km'] += run['dist'] / 1000.0
            else:
                out['fail'] += 1
            out['rows'].append({'day': day.isoformat(), 'ok': good,
                                'reach': res.get('sportRange'), 'remark': res.get('remark'),
                                'status': res.get('status'),
                                'sportStatus': res.get('sportStatus')})
    except Exception as exc:                       # noqa: BLE001
        out['error'] = f'{type(exc).__name__}: {exc}'[:160]
    finally:
        out['counters'] = dict(box.counters)
    out['km'] = round(out['km'], 2)
    return {'summary': out, 'box': box}


# --------------------------------------------------------------------------- #
# 省份批量
# --------------------------------------------------------------------------- #

async def run_province(schools: list[dict], count: int, seed: int, no_face: bool,
                       dry_run: bool) -> dict:
    results = []
    total = len(schools)
    for idx, school in enumerate(schools, 1):
        packed = await run_school(school, count, seed, no_face, dry_run)
        summary = packed['summary']
        box = packed['box']
        results.append(summary)
        mark = '✅' if summary['ok'] and not summary['error'] else ('⚠️' if summary['ok'] else '❌')
        detail = summary['error'] or f"{summary['ok']}/{summary['ok'] + summary['fail']} 条 {summary['km']} km"
        print(f"[{idx:>3}/{total}] {mark} {school['school_code']:<10} "
              f"{school['school_name'][:16]:<18} {detail}")
        # 单校明细单独落盘，便于对拍。
        # 文件名必须带 school_id：库里 `gdut` 有两条（school_id 338 与 501），
        # 只按 school_code 命名会互相覆盖，106 条记录在盘上只剩 105 份。
        out_dir = Path('logs') / 'province'
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{school['school_code']}-{school['school_id']}"
        (out_dir / f'{stem}.json').write_text(
            json.dumps(box.dump(str(out_dir / f'{stem}.json')),
                       ensure_ascii=False, indent=1), encoding='utf-8')

    ok_schools = [r for r in results if r['ok'] and not r['error']]
    payload = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'province': schools[0].get('province_name') if schools else '',
        'schools': len(schools),
        'schools_ok': len(ok_schools),
        'records_ok': sum(r['ok'] for r in results),
        'records_fail': sum(r['fail'] for r in results),
        'range_km': round(sum(r['km'] for r in results), 2),
        'results': results,
    }
    Path('logs').mkdir(parents=True, exist_ok=True)
    Path('logs/sandbox_province.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description='TSN 沙盘：按省份批量接管所有学校')
    parser.add_argument('--province', default='', help='省份名（库里 tsn_school.province_name）')
    parser.add_argument('--codes', default='', help='逗号分隔的 school_code')
    parser.add_argument('--sys-type', type=int, default=0, help='2=公版 1=私版，0=全部')
    parser.add_argument('--limit', type=int, default=0, help='只跑前 N 所（试跑用）')
    parser.add_argument('--count', type=int, default=1, help='每校提交条数')
    parser.add_argument('--seed', type=int, default=20261125)
    parser.add_argument('--no-face', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='只跑本地门禁，不提交')
    parser.add_argument('--report', default='logs/sandbox_province.json')
    args = parser.parse_args()

    os.chdir(ROOT)
    os.environ.setdefault(backfill.NO_WAIT_ENV, '1')

    schools = asyncio.run(load_schools(args.province, args.codes,
                                       args.sys_type, args.limit))
    if not schools:
        print('本地库里没有匹配的学校。先跑 python run.py fetch-schools --all')
        return 1
    where = args.province or args.codes
    print(f'目标：{len(schools)} 所学校（{where or "全库"}）'
          f'，每校 {args.count} 条，{"dry-run" if args.dry_run else "沙盘提交"}')

    if not args.no_face:
        face = Path(backfill.FACE_DIR) / 'face.jpg'
        if not face.is_file():
            make_face_jpeg(str(face))
            print(f'合成人脸素材 {face}')

    payload = asyncio.run(run_province(schools, args.count, args.seed,
                                       args.no_face, args.dry_run))
    print(f"\n===== 汇总 =====")
    print(f"学校 {payload['schools_ok']}/{payload['schools']} 所跑通"
          f"（{payload['schools'] - payload['schools_ok']} 所有问题）")
    print(f"记录 {payload['records_ok']} 条合格 / {payload['records_fail']} 条不合格"
          f"，合计 {payload['range_km']} km")
    print(f"明细已写出 {args.report}")
    return 0 if payload['schools_ok'] == payload['schools'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
