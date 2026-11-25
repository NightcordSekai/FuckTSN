#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量补跑（历史缺跑回填）—— 配置化移植版。

移植自老仓库 ``backfill.py``（最权威的那一版），能力逐条保留：
本地门禁 → 时间窗 → 台账断点 → 禁跑守卫 → 间隔控速 → 失败即停。

与老版的三处差异只在「配置层」，其余行为不变：

1. ``SCHOOL_ID`` / ``SCHOOL_CODE`` / ``BASE_URL`` / 账号文件 / 计划文件 / 台账
   （示例取北京大学：school_id=100、schoolCode=pku、base_url=https://h.tsnkj.com）
   路径全部来自 :mod:`config`，不再写死在源码里；
2. 参考线路不再读 ``_probe/hist.json``（那是个人数据）：优先
   ``reference.json``，缺失则用配置里的围栏合成一条，围栏也缺失时退化到
   「占位中心 + 120 m 圆」并打印警告；
3. 生成本仓库（重构版）已把步幅模型换成真实人类慢跑，因此上报里程与点串
   累加之间按真机方向留出 +1%~+3% 的 GPS 余量，避免「里程过于整齐」被门禁
   判为合成数据。

常用::

    python backfill.py --count 14 --dry-run                # 离线生成计划 + 本地门禁
    python backfill.py --dates 2026-11-01,2026-11-03 --submit
    python backfill.py --watch --submit --resume --per-day 1
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import io
import json
import math
import os
import random
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，🚫/⚠️ 之类符号会直接把脚本打崩。
# 注意：这里只能用 `reconfigure()`，不能 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`
# —— 后者会接管底层 buffer 的所有权，wrapper 一旦被回收就会把捕获流（pytest、重定向）关掉，
# 表现为 `ValueError: I/O operation on closed file`。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        except (ValueError, OSError):  # 已关闭或不可重配的流（如测试捕获）直接跳过
            pass

from tsn import compliance
from app import config
from app.device import deviceModel
from sdk.runpath import genTiShiNengRunPathRepeat
from track.determinism import seeded, seed_from_rng
from tsn import submit as tsn_submit
from sdk.public import TiShiNengSdkPublic

# ---------------------------------------------------------------- 常量

RUN_TYPE_SUN = 1                 # 阳光跑
MIN_STEPS = 1000                 # 服务端实测判线（999 步被判「运动步数不符合」）
NO_WAIT_ENV = 'BACKFILL_NO_WAIT'
REFERENCE_FILE = 'reference.json'
DEFAULT_FENCE_CENTER = (30.0, 120.0)   # 占位中心，不对应任何真实校区
DEFAULT_FENCE_RADIUS_M = 120.0
DEFAULT_SETTING = {
    'rangeSetting': '2.000', 'stepSetting': 1000, 'limitSpeed': '3.33',
    'limitStride': '2.3', 'limitStepFrequency': '300',
}
_M_PER_DEG_LAT = 111320.0

# 蓝本里 face_bytes 从 face_images/ 下取图，这里保持同样的约定
FACE_DIR = 'face_images'


def _no_wait() -> bool:
    return os.environ.get(NO_WAIT_ENV, '') not in ('', '0', 'false', 'False')


async def _sleep(seconds: float) -> None:
    """测试开关：BACKFILL_NO_WAIT=1 时所有等待都塌缩成一次让步。"""
    await asyncio.sleep(0.02 if _no_wait() else seconds)


# ---------------------------------------------------------------- 配置 / 参考线路

def _cfg_center() -> tuple[float, float]:
    """(lat, lon)：config 的 latitude/longitude → reference.json 的线路质心 → 占位中心。

    第二档是关键：`reference.json` 里就是校区真实线路，质心天然落在校区上。
    早期实现直接落到 `DEFAULT_FENCE_CENTER`，那是个 (30, 120) 的纯占位点，
    和参考线路差 959 km，围栏检查（修好 `_ring_points` 之后）会 100% 判围栏外。
    """
    cfg = config.load()
    try:
        return float(cfg.get('latitude')), float(cfg.get('longitude'))
    except (TypeError, ValueError):
        pass

    center = _reference_center()
    if center:
        return center
    return DEFAULT_FENCE_CENTER


def _reference_center() -> tuple[float, float] | None:
    """reference.json 线路的质心；读不到就返回 None。"""
    points, _rec = load_reference()
    if not points:
        return None
    # _parse_points 已经统一成 [[lon, lat], ...]
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _json_maybe(value):
    """服务端把 JSON 塞在字符串里下发，能解就解，不能解原样返回。"""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ('[', '{'):
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def _parse_points(raw) -> list[list[float]]:
    """把 gitudeLatitude 的两种形态统一成 [[lon, lat], ...]。"""
    data = _json_maybe(raw)
    out: list[list[float]] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if isinstance(item, dict):
            try:
                out.append([float(item['o']), float(item['a'])])
            except (KeyError, TypeError, ValueError):
                continue
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                out.append([float(item[0]), float(item[1])])
            except (TypeError, ValueError):
                continue
    return out


def _norm_fence(raw) -> list:
    """围栏统一成 ``[[{'lng':..., 'lat':...}, ...]]``（服务端下发的那种结构）。"""
    data = _json_maybe(raw)
    if isinstance(data, dict):
        data = data.get('coordinates', [])
    if not isinstance(data, list) or not data:
        return []
    rings = data
    if rings and not isinstance(rings[0], (list, dict)):
        rings = [rings]
    out = []
    for ring in rings:
        if isinstance(ring, dict):
            ring = ring.get('coordinates') or []
        if not isinstance(ring, list):
            continue
        verts = []
        for v in ring:
            if isinstance(v, dict):
                try:
                    verts.append({'lng': f'{float(v.get("lng", v.get("o"))):.6f}',
                                  'lat': f'{float(v.get("lat", v.get("a"))):.6f}'})
                except (TypeError, ValueError):
                    continue
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                verts.append({'lng': f'{float(v[0]):.6f}', 'lat': f'{float(v[1]):.6f}'})
        if len(verts) >= 3:
            out.append(verts)
    return out


def _fence_ring(fence) -> list[tuple[float, float]]:
    """取第一圈围栏为 [(lng, lat), ...]。"""
    rings = _norm_fence(fence)
    if not rings:
        return []
    ring = []
    for v in rings[0]:
        try:
            ring.append((float(v['lng']), float(v['lat'])))
        except (KeyError, TypeError, ValueError):
            continue
    return ring


def _circle_fence(lat0: float, lon0: float, radius_m: float, n: int = 48) -> list:
    """以 (lat0, lon0) 为心、半径 radius_m 的圆围栏（占位用）。"""
    dlat = radius_m / _M_PER_DEG_LAT
    dlon = radius_m / (_M_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat0))))
    ring = []
    for i in range(n):
        t = 2 * math.pi * i / n
        ring.append({'lng': f'{lon0 + dlon * math.cos(t):.6f}',
                     'lat': f'{lat0 + dlat * math.sin(t):.6f}'})
    return [ring]


def load_reference() -> tuple[list, dict | None]:
    """参考线路：``reference.json`` 优先；没有就返回 ([], None) 交给合成器。"""
    path = config.get('reference_file') or REFERENCE_FILE
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return [], None

    if isinstance(data, list):            # 允许直接给点串
        return _parse_points(data), None
    if not isinstance(data, dict):
        return [], None

    rec = dict(data)
    points = _parse_points(rec.get('gitudeLatitude') or rec.get('points') or [])
    if not points:
        return [], rec
    return points, rec


def build_setting(rec: dict | None) -> dict:
    """服务端阈值 + 围栏，缺项用默认值补齐。"""
    rec = rec or {}
    setting = {
        'totalRange': float(rec.get('rangeSetting') or 2.0),
        'startQualifiedTime': int(rec.get('startQualifiedTime') or 0),
        'endQualifiedTime': int(rec.get('endQualifiedTime') or 120),
        'step': int(rec.get('stepSetting') or 1000),
        'isStepRecursion': 1,
        'userOpenDevelop': False,
        'limitSpeed': rec.get('limitSpeed') or DEFAULT_SETTING['limitSpeed'],
        'limitStride': rec.get('limitStride') or DEFAULT_SETTING['limitStride'],
        'limitStepFrequency': rec.get('limitStepFrequency') or DEFAULT_SETTING['limitStepFrequency'],
        'geofence': rec.get('geofence'),
    }
    return setting


# ---------------------------------------------------------------- 线路合成

def _lon_scale(lat0: float) -> float:
    return max(1.0, _M_PER_DEG_LAT * math.cos(math.radians(lat0)))


def _ring_xy(ring, lat0: float) -> list[tuple[float, float]]:
    kx = _lon_scale(lat0)
    return [(lng * kx, lat * _M_PER_DEG_LAT) for lng, lat in ring]


def _xy_len(xy) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(xy, xy[1:]))


def _inside_xy(x: float, y: float, ring_xy) -> bool:
    inside = False
    n = len(ring_xy)
    for i in range(n):
        x1, y1 = ring_xy[i]
        x2, y2 = ring_xy[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
            if x < xin:
                inside = not inside
    return inside


def _resample_xy(xy, step_m: float = 4.0) -> list[tuple[float, float]]:
    """按固定间距重采样折线（合成线路用它把采样点压到 1s 一帧的量级）。"""
    if len(xy) < 2:
        return list(xy)
    out = [xy[0]]
    carry = 0.0
    for a, b in zip(xy, xy[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg <= 0:
            continue
        pos = step_m - carry
        while pos <= seg:
            t = pos / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            pos += step_m
        carry = (seg - (pos - step_m)) % step_m
    if out[-1] != xy[-1]:
        out.append(xy[-1])
    return out


def _truncate_xy(xy, target_m: float) -> list[tuple[float, float]]:
    out = [xy[0]]
    acc = 0.0
    for a, b in zip(xy, xy[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        acc += seg
        if acc >= target_m and len(out) >= 40:
            break
        out.append(b)
    return out


def _synthesize_path(ring, rng, target_m: float) -> list[list[float]]:
    """在围栏内部合成一条椭圆套圈线路（关闭式，带车道漂移与反向）。"""
    norm = _fence_ring(ring)
    if len(norm) >= 3:
        ring = norm
    lat0 = sum(p[1] for p in ring) / len(ring)
    xy = _ring_xy(ring, lat0)
    xs = [p[0] for p in xy]
    ys = [p[1] for p in xy]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    hw = max(25.0, (max(xs) - min(xs)) / 2 - 8.0)
    hh = max(25.0, (max(ys) - min(ys)) / 2 - 8.0)
    rx = hw * rng.uniform(0.74, 0.92)
    ry = hh * rng.uniform(0.74, 0.92)
    theta = rng.uniform(-0.35, 0.35)
    ct, st = math.cos(theta), math.sin(theta)
    n = 240

    def lap(lane: float) -> list[tuple[float, float]]:
        pts = []
        for i in range(n + 1):
            t = 2 * math.pi * i / n
            x, y = (rx + lane) * math.cos(t), (ry + lane) * math.sin(t)
            pts.append((cx + x * ct - y * st, cy + x * st + y * ct))
        return pts

    probe = lap(0.0)
    lap_len = max(1.0, _xy_len(probe))
    laps = max(1, int(round(target_m / lap_len)))
    raw: list[tuple[float, float]] = []
    for i in range(laps):
        seg = lap(rng.uniform(-1.6, 1.6))
        if i % 2:
            seg = list(reversed(seg))
        raw.extend(seg[1:] if i else seg)
    raw = _truncate_xy(_resample_xy(raw, 4.0), target_m)

    kx = _lon_scale(lat0)
    out = [[round(x / kx, 7), round(y / _M_PER_DEG_LAT, 7)] for x, y in raw]
    return _clamp_into_ring(out, ring, inset_m=3.5)


def _jitter_ref(ref, rng, sigma_m: float = 2.2) -> list[list[float]]:
    """把参考线路整体加一点 GPS 抖动量级噪偏，做出「同一条路不同一天」的差异。"""
    lat0 = sum(p[1] for p in ref) / len(ref)
    kx = _lon_scale(lat0)
    out = []
    for lon, lat in ref:
        dx = rng.gauss(0.0, sigma_m)
        dy = rng.gauss(0.0, sigma_m)
        out.append([round(lon + dx / kx, 7), round(lat + dy / _M_PER_DEG_LAT, 7)])
    return out


def _clamp_into_ring(points, ring, inset_m: float = 3.5) -> list[list[float]]:
    """把跑出围栏的点按比例往中心收，直到落回围栏内（最多 25 轮）。"""
    if not points or len(ring) < 3:
        return points
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx = _lon_scale(lat0)
    ring_xy = _ring_xy(ring, lat0)
    xs = [p[0] for p in ring_xy]
    ys = [p[1] for p in ring_xy]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    out = []
    for lon, lat in points:
        x, y = lon * kx, lat * _M_PER_DEG_LAT
        for _ in range(25):
            if _inside_xy(x, y, ring_xy):
                break
            x = cx + (x - cx) * 0.94
            y = cy + (y - cy) * 0.94
        out.append([round(x / kx, 7), round(y / _M_PER_DEG_LAT, 7)])
    del inset_m  # 只保留签名里的语义，实际按比例收缩收敛更快
    return out


def _pick_path(ref, setting, target_m: float, rng) -> list[list[float]]:
    """参考线路 / 合成线路二选一；合成器优先用 track_shape（若仓库里有）。"""
    ring = _fence_ring(setting.get('geofence'))
    try:
        from track import shape  # 轨迹形态合成器
    except Exception:  # noqa: BLE001
        shape = None

    if ring and len(ring) >= 3 and shape is not None:
        try:
            return shape.synthesize_path(ring, rng, target_m)
        except Exception as exc:  # noqa: BLE001
            print(f'⚠️ shape.synthesize_path 失败（{type(exc).__name__}），退回内置合成器')

    if ref and len(ref) >= 3:
        path = _jitter_ref(ref, rng)
        return _clamp_into_ring(path, ring) if ring else path
    if len(ring) < 3:
        lat0, lon0 = _cfg_center()
        ring = _circle_fence(lat0, lon0, DEFAULT_FENCE_RADIUS_M)
    return _synthesize_path(ring, rng, target_m)


# ---------------------------------------------------------------- 一次跑步

def face_bytes() -> bytes:
    """人脸图：face_images/ 下任意一张（由 run.py face 抓取，已 gitignore）。"""
    root = FACE_DIR
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith(('.jpg', '.jpeg', '.png')):
                with open(os.path.join(dirpath, name), 'rb') as fh:
                    return fh.read()
    raise SystemExit(f'本地没有人脸图片（{FACE_DIR}/），先执行 python run.py face --account 1')


def steps_from_points(points, bucket_ms: int = 60000) -> list[int]:
    """从点串的累计步数 e 反推 60s 桶内步数（App 就是这么分段的）。"""
    if not points:
        return []
    buckets: dict[int, int] = {}
    order = []
    prev_e = 0
    t0 = None
    for p in points:
        try:
            t = int(float(p.get('t')))
            e = int(float(p.get('e')))
        except (TypeError, ValueError):
            continue
        # 以首点为桶原点：末段自然是被截断的尾巴（App 也只允许末段短）
        if t0 is None:
            t0 = t
        key = (t - t0) // bucket_ms
        if key not in buckets:
            buckets[key] = 0
            order.append(key)
        buckets[key] += max(0, e - prev_e)
        prev_e = e
    return [int(buckets[k]) for k in sorted(order)]


def _max_stride() -> float:
    try:
        from track import metrics
        return float(getattr(metrics, 'STRIDE_MAX_M', 1.20))
    except Exception:  # noqa: BLE001
        return 1.20


def build_run(day: datetime.date, ref, setting, rng) -> dict:
    """生成一条待提交记录（点串 + 步数桶 + meta）。

    外面套一层 :func:`track.determinism.seeded`，是因为 `sdk/runpath.py`
    里有几处随机**不接外部 rng**（模块级 `random.randint` / 无名
    `random.Random()`）。不接管它们的话 `--seed` 只是摆设：同一 seed
    连跑两次，里程与点串都不相同（实测 2.3524 vs 2.3299）。

    种子从调用方 rng **当前状态**派生，所以既不用改任何调用方签名，
    又保留了「rng 前进 → 下一条记录不同」的原意。
    """
    with seeded(seed_from_rng(rng)):
        return _build_run(day, ref, setting, rng)


def _build_run(day: datetime.date, ref, setting, rng) -> dict:
    local_start = datetime.datetime.combine(
        day, datetime.time(hour=rng.randint(6, 8), minute=rng.randint(0, 55),
                           second=rng.randint(5, 55)))
    start_ts = int(local_start.timestamp() * 1000)
    km = round(rng.uniform(2.16, 2.42), 2)
    plan_s = km * rng.uniform(6.2, 8.4) * 60

    last_exc = None
    for _attempt in range(3):
        path = _pick_path(ref, setting, km * 1000.0, rng)
        try:
            points, gen_segs, raw_dist = genTiShiNengRunPathRepeat(
                path, km * 1000, start_ts, plan_s, True)
            break
        except SystemExit as exc:            # validate_track 不认这条线路，换一条
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    else:
        raise SystemExit(f'线路生成连续 3 次失败：{type(last_exc).__name__}: {last_exc}')

    # stepNumbers 必须与点串累计步数严格自洽（App 就是这么算的），因此从 e 反推
    segs = steps_from_points(points)
    last_e = int(float(points[-1].get('e') or 0))
    if not segs or sum(segs) != last_e:
        segs = [int(n) for n in (gen_segs or [])] or segs
    total_steps = sum(segs)

    end_ts = int(points[-1]['t']) + rng.randint(100, 200)
    used = (end_ts - start_ts) // 1000
    # 真机上报里程略小于点串累加（GPS 抖动方向），留 +0.8%~+3.0% 余量。
    # 必须挂在「点串累加」上：生成器返回的 measured 是平滑里程，与带噪点串
    # 的重算值差 +2% 左右，直接拿来当上报值会顶到 +6% 的硬线。
    recomputed = compliance.recompute_distance(points)
    dist = round(recomputed / (1.0 + rng.uniform(0.008, 0.030)), 2)

    _need = max(MIN_STEPS, int(round(recomputed / _max_stride())))
    if total_steps < _need:
        raise SystemExit(f'步数 {total_steps} 低于人类步幅下限要求 {_need}，模型自洽性被破坏')
    pace_s = used / (dist / 1000)
    pace = f'{int(pace_s // 60)}\u2032{int(pace_s % 60):02d}\u2033'
    meta = {
        'sport_range_km': round(dist / 1000, 2),
        'sport_time_s': used,
        'distance_m': dist,
        'total_steps': total_steps,
        'identify': str(uuid.uuid4()).upper(),
        'start_ts_ms': start_ts,
        'end_ts_ms': end_ts,
        'pace': pace,
        'app_version': deviceModel.app_version,
        'brand': deviceModel.brand,
        'model': deviceModel.model,
        'system': deviceModel.system,
        'osver': deviceModel.osver,
        'required_range_m': tsn_submit.per_run_range_meters(setting),
        'start_qualified_min': setting.get('startQualifiedTime'),
        'end_qualified_min': tsn_submit.adaptive_end_qualified_time(setting, dist),
        'sport_type': '1',
        'ok_point_count': 3,
        'step_setting': int(setting.get('stepSetting') or 1000),
        'limit_speed': float(setting.get('limitSpeed') or 3.33),
        'range_setting': float(setting.get('rangeSetting') or 2.0),
        'limit_step_frequency': float(setting.get('limitStepFrequency') or 300.0),
        'face_ok': True,
    }
    return {'day': day, 'points': points, 'segs': segs, 'dist': dist,
            'start_ts': start_ts, 'end_ts': end_ts, 'used': used,
            'steps': total_steps, 'pace': pace, 'meta': meta}


# ---------------------------------------------------------------- 登录 / 提交

def _apply_token(tsn, tok):
    """token 三种形态都要认：字符串 / {'access_token'} / {'data': {...}}。"""
    if isinstance(tok, str) and tok:
        tsn.setToken(tok)
        return tsn
    if isinstance(tok, dict):
        data = tok.get('data')
        if isinstance(data, str) and data:      # 冻结 / 异常时 data 是一句话
            msg = str(tok.get('msg') or tok.get('message') or data)
            raise RuntimeError(f'登录未返回 token：{msg[:80]}')
        token = tok.get('access_token') or (data or {}).get('access_token')
        if isinstance(token, str) and token:
            tsn.setToken(token)
            return tsn
        msg = tok.get('msg') or tok.get('message') or data or tok
        raise RuntimeError(f'登录未返回 token：{str(msg)[:80]}')
    raise RuntimeError('登录返回类型异常')


async def make_client() -> TiShiNengSdkPublic:
    cfg = config.load()
    acct = cfg.get('account_file') or 'account.txt'
    try:
        with open(acct, encoding='utf-8') as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError as exc:
        raise RuntimeError(f'读不到账号文件 {acct}（第一行账号、第二行密码）：{exc}') from exc
    if len(lines) < 2:
        raise RuntimeError(f'账号文件 {acct} 需要两行：第一行账号、第二行密码')
    user, pwd = lines[0], lines[1]

    tsn = TiShiNengSdkPublic(
        1, int(cfg.get('school_id') or 0), str(cfg.get('school_code') or ''), '',
        str(uuid.uuid4()), deviceModel.brand, deviceModel.model,
        deviceModel.osver, '', deviceModel.a_list)
    tsn.setCloudUrl(str(cfg.get('base_url') or ''))
    tok = await tsn.getAccessToken(user, pwd)
    return _apply_token(tsn, tok)


async def ban_status(tsn, lat: float | None = None, lng: float | None = None):
    """(是否禁跑, 说明)。坐标走配置/围栏中心，不再写死某个校区。"""
    if lat is None or lng is None:
        lat, lng = _cfg_center()
    try:
        await tsn.getExerciseSetting(RUN_TYPE_SUN, f'{lng:.6f}', f'{lat:.6f}')
        return False, '未禁跑'
    except Exception as exc:  # noqa: BLE001
        msg = f'{type(exc).__name__}: {exc}'
        return ('黑名单' in msg or '禁跑' in msg), msg


async def connect(retries: int = 0, forever: bool = False):
    """登录 + 禁跑查询。冻结类报错按「N 秒」精确等待，其余按 30*attempt 退避。"""
    attempt = 0
    while True:
        attempt += 1
        try:
            tsn = await make_client()
            banned, msg = await ban_status(tsn)
            print(f'登录成功（第 {attempt} 次尝试）')
            return tsn, banned, msg
        except Exception as exc:  # noqa: BLE001
            text = f'{type(exc).__name__}: {exc}'
            if '冻结' in text:
                hit = re.search(r'(\d+)\s*秒', text)
                wait = (int(hit.group(1)) + 45) if hit else 180
            else:
                wait = min(30 * attempt, 300)
            print(f'连接失败 {attempt} 次：{text[:70]} — {wait}s 后重试')
            if not forever and retries and attempt >= retries:
                raise
            await asyncio.sleep(0.05 if _no_wait() else wait)


async def submit(tsn, run, setting, fence, with_face: bool) -> dict:
    face_ok = False
    if with_face:
        img = face_bytes()
        face_ok = True
        mid = run['points'][len(run['points']) // 2]
        for ftype, pt in ((1, run['points'][0]), (2, mid), (3, run['points'][-1])):
            r = await tsn.exerciseRunningFace(img, f'{pt["o"]},{pt["a"]}',
                                              run['meta']['identify'],
                                              runType=RUN_TYPE_SUN, faceType=ftype)
            if r is not True and str(r).lower() != 'true':
                face_ok = False
                print(f'        ⚠️ 人脸 type={ftype} 返回 {r!r}')
    tsn_resp, rid = await tsn.addExerciseRecord(
        sportType=RUN_TYPE_SUN,
        startTime=run['start_ts'], endTime=run['end_ts'], sportTime=run['used'],
        sportRange=f'{run["dist"] / 1000:.2f}', speed=run['pace'],
        avgSpeed=f'{run["dist"] / 1000 / (run["used"] / 3600.0):.3f}',
        gitudeLatitude=json.dumps(run['points']), stepNumbers=json.dumps(run['segs']),
        isSequencePoint='0', pointList=json.dumps([]), okPointList=json.dumps([]),
        isFaceStatus=1 if with_face else 0, uploadType=0,
        identify=run['meta']['identify'], geofence=json.dumps(fence or []),
        limitSpeed=setting['limitSpeed'], limitStride=setting['limitStride'],
        limitStepFrequency=setting['limitStepFrequency'],
        gpsDistance=f'{run["dist"] / 1000:.2f}',
        setting=setting, trackPoints=run['points'], stepCount=run['steps'],
        elapsedSeconds=run['used'], distanceMeters=run['dist'],
        userOpenDevelop=False, offline=0)
    result = {'resp': tsn_resp, 'id': rid, 'face_ok': face_ok}
    if rid:
        await asyncio.sleep(1.5 if not _no_wait() else 0.05)
        rec = await tsn.getExerciseRecord(rid) or {}
        result.update({k: rec.get(k) for k in
                       ('status', 'sportStatus', 'remark', 'step', 'sportRange', 'speed')})
    return result


# ---------------------------------------------------------------- 日期 / 台账

async def occupied_dates(tsn) -> set[str]:
    """已占用日期：status=1（合格）与 status=0（无效/被罚）都算。"""
    days: set[str] = set()
    for st in (1, 0):
        for idx in range(1, 6):
            try:
                rows = await tsn.listExerciseRecord(st, '', idx)
            except Exception as exc:  # noqa: BLE001
                print(f'⚠️ 拉取已占用记录失败（status={st} page={idx}）：{type(exc).__name__}: {exc}')
                break
            if not rows:
                break
            for row in rows:
                raw = row.get('startTime') or ''
                text = str(raw)
                if len(text) >= 10:
                    days.add(text[:10].replace('/', '-'))
    return days


def plan_dates(count: int, start=None, end=None, weekdays_only: bool = True,
               min_gap: int = 1) -> list[datetime.date]:
    """从过去的日子往回挑 count 天，工作日、彼此间隔 >= min_gap。"""
    today = datetime.date.today()
    end = end or (today - datetime.timedelta(days=1))
    if end >= today:
        end = today - datetime.timedelta(days=1)
    start = start or (end - datetime.timedelta(days=max(30, count * 8 + 7)))
    pool = []
    day = start
    while day <= end:
        if not weekdays_only or day.weekday() < 5:
            pool.append(day)
        day += datetime.timedelta(days=1)
    gap = max(1, int(min_gap))
    picked: list[datetime.date] = []
    for day in reversed(pool):
        if all(abs((day - p).days) >= gap for p in picked):
            picked.append(day)
        if len(picked) >= count:
            break
    return sorted(picked)


def pick_dates(count: int, occupied: set[str], today: datetime.date,
               earliest: datetime.date | None = None) -> list[datetime.date]:
    """从过去未占用的工作日里等距抽样 count 天，越分散越好。"""
    earliest = earliest or datetime.date(today.year - 1, 10, 1)
    pool = []
    day = earliest
    while day < today:
        if day.weekday() < 5 and day.isoformat() not in occupied:
            pool.append(day)
        day += datetime.timedelta(days=1)
    if not pool or count <= 0:
        return []
    step = max(1, len(pool) // count)
    return pool[::step][:count]


def _parse_window(text: str) -> tuple[int, int]:
    try:
        lo_s, hi_s = str(text).replace('：', ':').split('-', 1)
        lo, hi = int(lo_s), int(hi_s)
    except (AttributeError, ValueError):
        print(f'⚠️ --window {text!r} 解析失败，回退 6-21')
        return 6, 21
    lo = min(max(lo, 0), 23)
    hi = min(max(hi, 0), 24)
    if lo == hi:
        print(f'⚠️ --window {text!r} 起止相同，回退 6-21')
        return 6, 21
    return lo, hi


def _in_window(now: datetime.datetime, lo: int, hi: int) -> bool:
    if lo <= hi:
        return lo <= now.hour < hi
    return now.hour >= lo or now.hour < hi


async def wait_for_window(lo: int, hi: int, rng, reason: str = '') -> None:
    """窗外就睡到「次日窗口开启 + 0-40 分钟随机」。"""
    if _no_wait():
        return
    now = datetime.datetime.now()
    if _in_window(now, lo, hi):
        return
    target = now.replace(hour=lo, minute=0, second=0, microsecond=0)
    if now.hour >= lo and _in_window(now, lo, hi) is False:
        target += datetime.timedelta(days=1)
    if target <= now:
        target += datetime.timedelta(days=1)
    target += datetime.timedelta(minutes=rng.randint(0, 40), seconds=rng.randint(0, 59))
    gap = (target - now).total_seconds()
    print(f'{reason or "非提交时段"}：等到 {target:%m-%d %H:%M}（{gap / 3600:.1f}h）再提交…')
    await _sleep(max(1.0, gap))


def done_today(done: list) -> int:
    today = datetime.date.today().isoformat()
    return sum(1 for row in done if str(row.get('at', ''))[:10] == today)


def _load_json(path: str, default):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, payload) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)


def _days_from_plan(path: str) -> list[datetime.date]:
    data = _load_json(path, [])
    if isinstance(data, dict):
        data = data.get('records') or data.get('days') or []
    out = []
    for item in data or []:
        raw = item.get('day') if isinstance(item, dict) else item
        try:
            out.append(datetime.date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    return sorted(set(out))


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='backfill.py',
        description='批量补跑历史缺跑日期：本地门禁 + 禁跑守卫 + 台账断点（配置见 config.json）',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument('--check', action='store_true', help='只查禁跑状态后退出')
    ap.add_argument('--count', type=int, default=14, help='要补的天数')
    ap.add_argument('--dates', default='', help='直接指定日期，逗号分隔（2026-11-01,2026-11-03）')
    ap.add_argument('--plan', default='', help='从计划文件读日期（backfill 的 --dry-run 会写它）')
    ap.add_argument('--order', choices=('newest', 'oldest'), default='newest', help='提交顺序')
    ap.add_argument('--auto-dates', action='store_true', help='登录后拉已占用日期再抽样')
    ap.add_argument('--start', default='', help='--auto-dates 的起始日期 YYYY-MM-DD')
    ap.add_argument('--end', default='', help='--auto-dates 的结束日期 YYYY-MM-DD')
    ap.add_argument('--weekdays', action='store_true', help='只挑工作日')
    ap.add_argument('--gap', type=int, default=1, help='抽样最小间隔（天）')
    ap.add_argument('--stride', type=float, default=0.0, help='覆盖 metrics 的基准步幅（谨慎：影响门禁）')
    ap.add_argument('--seed', type=int, default=0, help='随机种子（0 = 用时间）')
    ap.add_argument('--no-face', action='store_true', help='不传人脸（会被服务端判黑名单，仅调试）')
    ap.add_argument('--submit', action='store_true', help='真的提交（默认只做本地门禁）')
    ap.add_argument('--force', action='store_true', help='禁跑中也强行提交（不建议）')
    ap.add_argument('--dry-run', action='store_true', help='只做本地门禁并写出计划')
    ap.add_argument('--watch', action='store_true', help='禁跑时盯守，解禁后继续')
    ap.add_argument('--interval', type=int, default=1800, help='盯守查询间隔（秒）')
    ap.add_argument('--warn-age', type=int, default=30, help='超过多少天的记录打提醒')
    ap.add_argument('--gap-min', type=int, default=25, help='两次提交的最小间隔（分钟）')
    ap.add_argument('--gap-max', type=int, default=70, help='两次提交的最大间隔（分钟）')
    ap.add_argument('--window', default='6-21', help='允许提交的时间窗 HH-HH')
    ap.add_argument('--per-day', type=int, default=0, help='每日成功上限（0 = 不限）')
    ap.add_argument('--spacing', type=int, default=0, help='秒级覆盖间隔（0 = 用 gap-min/gap-max）')
    ap.add_argument('--keep-going', action='store_true', help='门禁不通过也继续（仅诊断）')
    ap.add_argument('--max', type=int, default=0, help='本次最多成功几条（0 = 不限）')
    ap.add_argument('--resume', action='store_true', help='按台账跳过已完成的日期')
    return ap


async def main() -> None:
    args = build_parser().parse_args()
    cfg = config.load()
    print(f'配置: {config.config_path()}  学校 ID {cfg.get("school_id")}  '
          f'服务 {cfg.get("base_url")}')

    need_session = args.check or args.auto_dates or (args.submit and not args.dry_run)
    tsn = None
    banned, ban_msg = False, '离线规划'
    if need_session:
        tsn, banned, ban_msg = await connect(retries=3, forever=args.watch)
        print(f'禁跑状态: {"禁跑中" if banned else "正常"}（{str(ban_msg)[:60]}）')
        if args.check:
            return
        if banned and args.watch:
            fails = 0
            while True:
                await _sleep(args.interval)
                try:
                    banned, ban_msg = await ban_status(tsn)
                    fails = 0
                except Exception as exc:  # noqa: BLE001
                    fails += 1
                    backoff = min(600 * fails, 3600)
                    print(f'⚠️ 盯守查询失败 {fails} 次：{type(exc).__name__}: {str(exc)[:70]}'
                          f' — {backoff}s 后重试')
                    await _sleep(backoff)
                    if fails >= 3:
                        tsn, banned, ban_msg = await connect(forever=True)
                    continue
                print(f'… {datetime.datetime.now():%H:%M:%S} 仍禁跑：{str(ban_msg)[:60]}')
                if not banned:
                    print('✅ 已解禁，开始提交本批。')
                    break
        if banned and not args.force:
            print(f'⚠️ 当前禁跑，本次不提交（{str(ban_msg)[:80]}）。需要盯守请加 --watch。')
            return
    else:
        print('离线模式（--dry-run / 未加 --submit）：跳过登录与禁跑查询，只做本地门禁')

    if args.stride > 0:
        from track import metrics
        before = getattr(metrics, 'STRIDE_BASE_M', 0.86)
        metrics.STRIDE_BASE_M = float(args.stride)
        print(f'步幅基准 {before} → {args.stride}（注意：本仓库门禁要求 0.62–1.20 m）')

    ref, refrec = load_reference()
    setting = build_setting(refrec)
    fence = _norm_fence(setting.get('geofence'))
    if not fence:
        lat0, lon0 = _cfg_center()
        fence = _circle_fence(lat0, lon0, DEFAULT_FENCE_RADIUS_M)
        print(f'⚠️ 未配置围栏（reference.json / config.json 都没有 geofence），'
              f'临时用以 ({lat0:.4f}, {lon0:.4f}) 为中心、半径 {DEFAULT_FENCE_RADIUS_M:.0f}m 的圆；'
              f'--submit 前请配置真实围栏')
    setting['geofence'] = fence
    ring = _fence_ring(fence)
    src = 'reference.json' if ref else '内置合成器'
    print(f'线路来源: {src}  围栏: {len(ring)} 个顶点  里程要求 {setting["totalRange"]}km')

    # ---- 日期 ----
    today = datetime.date.today()
    if args.dates:
        days = []
        for chunk in args.dates.replace('/', '-').split(','):
            chunk = chunk.strip()
            if chunk:
                days.append(datetime.date.fromisoformat(chunk[:10]))
    elif args.plan:
        days = _days_from_plan(args.plan)
    elif args.auto_dates:
        occupied = await occupied_dates(tsn)
        start = datetime.date.fromisoformat(args.start) if args.start else None
        end = datetime.date.fromisoformat(args.end) if args.end else None
        days = pick_dates(args.count, occupied, today, start or end)
        print(f'已占用 {len(occupied)} 天，抽出 {len(days)} 天待补')
    else:
        start = datetime.date.fromisoformat(args.start) if args.start else None
        end = datetime.date.fromisoformat(args.end) if args.end else None
        days = plan_dates(args.count, start, end, True, args.gap)

    ledger = cfg.get('ledger_file') or 'logs/backfill_done.json'
    done = _load_json(ledger, [])
    if not isinstance(done, list):
        done = []
    if args.resume:
        done_days = {str(row.get('day') or '')[:10] for row in done}
        days = [d for d in days if d.isoformat() not in done_days]
    if not days:
        print('没有待提交的日期了（台账已满）。')
        return
    days = sorted(days, reverse=(args.order == 'newest'))
    print(f'待提交 {len(days)} 天：{days[0]} … {days[-1]}（{args.order}）')

    rng = random.Random(args.seed or time.time())
    lo, hi = _parse_window(args.window)
    with_face = not args.no_face
    ok = 0
    planned: list[dict] = []

    for day in days:
        age = (today - day).days
        flag = '老' if age > args.warn_age else '新'
        # `build_run` 内部已接管 sdk/runpath.py 的未播种随机源，这里不用再套。
        run = build_run(day, ref, setting, rng)
        report = compliance.check_submission(run['points'], run['segs'], run['meta'],
                                             fence=fence, history=[])
        print(f'[{flag}] {day}（{age} 天前）{run["dist"]:.0f}m 步数 {run["steps"]} '
              f'配速 {run["pace"]}/km → {report.summary()}')
        if not report.ok:
            for issue in report.errors:
                print(f'    ✗ {issue.code}: {issue.message}')
            if not args.keep_going:
                print('本地门禁未通过，整批中止。')
                break
        if args.dry_run or not args.submit:
            ok += 1
            planned.append({'day': day.isoformat(), 'points': run['points'],
                            'stepNumbers': run['segs'], 'meta': run['meta']})
            continue

        await wait_for_window(lo, hi, rng, reason='非提交时段')
        if args.per_day and done_today(done) >= args.per_day:
            print(f'今日已成功 {done_today(done)} 条（上限 {args.per_day}），明天再来。')
            await wait_for_window(lo, hi, rng, reason='今日额度已满')
            if args.per_day and done_today(done) >= args.per_day:
                break

        try:
            result = await submit(tsn, run, setting, fence, with_face)
        except Exception as exc:  # noqa: BLE001
            print(f'⚠️ 提交中断（{type(exc).__name__}: {str(exc)[:90]}）—— 结果未知。')
            print('   服务端可能已经建了记录（例如 httpx.ReadTimeout 后其实成功了）；'
                  '这里不重试，避免同一天叠两笔。下次 --resume 会用 occupied_dates '
                  '把这一天算作已占用而跳过。')
            break

        sport_status = str(result.get('sportStatus') or '')
        remark = str(result.get('remark') or '')
        face_ok = bool(result.get('face_ok'))
        if sport_status == '1' and '黑名单' not in remark and face_ok:
            ok += 1
            entry = {'day': day.isoformat(), 'id': result.get('id'), 'remark': remark,
                     'steps': run['steps'], 'dist_m': round(run['dist'], 1),
                     'pace': run['pace'],
                     'at': datetime.datetime.now().isoformat(timespec='seconds')}
            done.append(entry)
            _write_json(ledger, done)
            print(f'    ✅ 第 {ok} 条合格（id={result.get("id")}，{remark or "无备注"}）')
            if args.max and ok >= args.max:
                print(f'已达 --max {args.max}，收工。')
                break
        else:
            print(f'    ❌ 首条未被判合格：sportStatus={sport_status!r} remark={remark!r} '
                  f'face_ok={face_ok}')
            print('   立即停止，避免继续叠加处罚。')
            break

        if args.spacing:
            wait = args.spacing
        else:
            low = max(60, int(args.gap_min) * 60)
            wait = rng.randint(low, max(low + 60, int(args.gap_max) * 60))
        print(f'    下一条 {wait}s 后（{wait / 60:.0f} 分钟间隔）')
        await _sleep(wait)

    if planned:
        out = cfg.get('plan_file') or 'plan.json'
        _write_json(out, {
            'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'generator': 'backfill.py',
            'records': sorted(planned, key=lambda r: r['day']),
        })
        print(f'已写出计划 {out}（{len(planned)} 天，含点串，供 verify_plan.py 独立复核）')

    tail = '（dry-run 未提交）' if (args.dry_run or not args.submit) else ''
    print(f'\n完成: {ok}/{len(days)} 条通过{tail}')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f'❌ {type(exc).__name__}: {exc}')
        sys.exit(1)
