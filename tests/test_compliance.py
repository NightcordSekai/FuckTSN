# -*- coding: utf-8 -*-
"""合格门禁的回归测试。

不需要账号、网络或真实校园数据：线路用合成环形跑道生成。
"""
from __future__ import annotations

import math
import random
import time
import uuid

from tsn import compliance
from app.device import deviceModel
from sdk.runpath import genTiShiNengRunPathRepeat


def synthetic_route(lat0: float = 30.0, lon0: float = 120.0) -> list:
    pts = []
    for i in range(240):
        theta = 2 * math.pi * i / 240
        dlat = 62.0 * math.sin(theta) / 111320.0
        dlon = 41.0 * math.cos(theta) / (111320.0 * math.cos(math.radians(lat0)))
        pts.append([round(lon0 + dlon, 7), round(lat0 + dlat, 7)])
    pts.append(pts[0])
    return pts


def build_payload(seed: int = 0, distance_m: float = 2075.0):
    rng = random.Random(seed)
    # 固定落在当天的 07:0x，避免测试在夜间跑时踩到 06:00-21:00 的时段门禁
    now = time.localtime()
    start_ts = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                7, 10 + seed, 0, 0, 0, -1)) * 1000)
    plan_s = distance_m / 1000 * rng.uniform(6.2, 8.4) * 60
    points, segments, dist = genTiShiNengRunPathRepeat(
        synthetic_route(), distance_m, start_ts, plan_s, True)
    end_ts = int(points[-1]["t"]) + rng.randint(100, 200)
    used = (end_ts - start_ts) // 1000
    total_steps = sum(int(n) for n in segments)
    pace_s = used / (dist / 1000)
    meta = {
        "sport_range_km": round(dist / 1000, 2),
        "sport_time_s": used,
        "distance_m": dist,
        "total_steps": total_steps,
        "identify": str(uuid.uuid4()).upper(),
        "start_ts_ms": start_ts,
        "end_ts_ms": end_ts,
        "pace": f"{int(pace_s // 60)}\u2032{int(pace_s % 60):02d}\u2033",
        "app_version": deviceModel.app_version,
        "brand": deviceModel.brand,
        "model": deviceModel.model,
        "system": deviceModel.system,
        "osver": deviceModel.osver,
        "required_range_m": 2000.0,
        "ok_point_count": 3,
    }
    return points, segments, meta


def test_generated_run_passes_gate():
    for seed in range(3):
        points, segments, meta = build_payload(seed)
        report = compliance.check_submission(points, segments, meta, history=[])
        assert report.ok, [i.message for i in report.errors]
        # 真机点串累加恒比上报值长：5 条合格记录实测 +0.53%~+37.1%，
        # 所以链长只在下限（比上报短）算致命，比上报长只做提醒
        assert 0.0 <= report.metrics["chain_delta_ratio"] <= 0.45


def test_point_schema_matches_app_native():
    points, _, _ = build_payload()
    assert set(points[0]) == compliance.POINT_KEYS
    assert all(isinstance(v, str) for v in points[0].values())


def test_numeric_values_are_rejected():
    points, segments, meta = build_payload()
    bad = [dict(p) for p in points]
    bad[10]["s"] = float(bad[10]["s"])
    report = compliance.check_submission(bad, segments, meta, history=[])
    assert not report.ok
    assert any(i.code == "point_types" for i in report.errors)
    assert {i.tier for i in report.errors} == {compliance.TIER_PROTOCOL}


def test_non_uuid_identify_is_rejected():
    points, segments, meta = build_payload()
    meta = dict(meta, identify="1")
    report = compliance.check_submission(points, segments, meta, history=[])
    assert not report.ok
    assert any(i.code == "identify" for i in report.errors)


def test_absurd_stride_is_only_a_warning():
    """合格记录里步幅 0.95~3.79 m、步频 47~129 都存在，所以它们不能算致命。"""
    points, segments, meta = build_payload()
    meta = dict(meta, total_steps=int(meta["total_steps"] * 2))
    report = compliance.check_submission(points, segments, meta, history=[])
    codes = {i.code for i in report.issues}
    assert codes & {"stride_hint", "cadence_hint", "steps_tail"}
    assert "stride_hint" not in {i.code for i in report.errors}
    # 严格模式下才升级为不通过
    strict = compliance.check_submission(points, segments, meta, history=[],
                                        mode=compliance.MODE_STRICT)
    assert not strict.ok


def test_sample_interval_break_is_rejected():
    """整段采样间隔被拉到 2 s → 中位数不再是 1000 ms。"""
    points, segments, meta = build_payload()
    bad = [dict(p) for p in points]
    for index, point in enumerate(bad[20:]):
        point["t"] = str(int(point["t"]) + index * 1000)
    report = compliance.check_submission(bad, segments, meta, history=[])
    assert any(i.code == "dt_median" for i in report.errors)


def test_millisecond_jitter_is_tolerated():
    """真实合格记录 1125/4984 个点不在整秒上（±2 ms 抖动），不能因此拒。

    这条用例以前是随机的：`genTiShiNengRunPathRepeat` 自己就会给 `t` 加 ±2 ms
    抖动，再对每 7 个点无脑 -1，碰上"原本就已经 -2 ms"的点就会造出真正的倒退，
    于是 20 次里失败 5 次。这里改成按固定模式重铺时间轴：相邻点严格差 1 s，
    抖动只取 ±2 ms 内的固定序列，既确定又仍然覆盖规则边界。
    """
    points, segments, meta = build_payload()
    bad = [dict(p) for p in points]
    anchor = int(bad[0]["t"]) // 1000 * 1000
    pattern = (0, -1, 2, -2, 1)
    for index, point in enumerate(bad):
        point["t"] = str(anchor + index * 1000 + pattern[index % len(pattern)])
    report = compliance.check_submission(bad, segments, meta, history=[])
    codes = {i.code for i in report.issues}
    assert 'dt_median' not in codes and 't_not_increasing' not in codes
    assert 't_jitter' not in {i.code for i in report.errors}


def test_same_day_duplicate_is_rejected():
    points, segments, meta = build_payload()
    history = [{"start_ts_ms": meta["start_ts_ms"] - 3600 * 1000,
                "sport_range_km": 2.07, "total_steps": 2600}]
    report = compliance.check_submission(points, segments, meta, history=history)
    assert not report.ok
    assert any(i.code == "frequency_same_day" for i in report.errors)
