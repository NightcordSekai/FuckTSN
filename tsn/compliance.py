# -*- coding: utf-8 -*-
"""合格门禁（合规自检）——按"证据强度"分级。

设计原则
--------
服务器对一条记录的判定完全在服务端完成（客户端上报的 status/sportStatus/remark/va/isValid
都会被重算或丢弃），所以本地唯一能做的是：**在提交之前，对照已经验证过的服务器行为，把
必然被拒的报文拦下来**。

真实抓取的数据（2026-11，见 tests/fixtures）给出了两类证据：

* **硬闸门**（``TIER_SERVER``）：服务器的判定文案直接指向它，且有"同一指标下通过/拒绝"的
  对照样本。例如 ``step=999`` → ``运动步数不符合131310``，而 ``step=1004/1015/1059/1099/2013/2109``
  全部合格；两者的分界线正是 ``stepSetting=1000``。
* **经验区间**（``TIER_HEURISTIC``）：只是 5 条合格记录落在这个范围里，服务器**并没有**按它
  拒绝过任何记录。例如合格记录的步幅从 0.95 m 到 3.79 m、步频从 47 到 129 步/分、点位链长
  比上报里程多出 37% —— 这些值都"不像真人"，但服务器全部判了合格。把它们当硬闸门会在本地
  自我否决掉本可以合格的报文，所以只做警告。

协议层（``TIER_PROTOCOL``）是"服务器根本解析不了/直接 403"的那一类：点位键缺失、值不是字符串、
步数分段不是合法 JSON。

三档模式
--------
``mode='strict'``     四层全算致命（本地最保守，用于自检/回归）
``mode='balanced'``   协议层 + 硬闸门算致命（默认，用于真实提交前）
``mode='compatible'`` 只有协议层算致命（用于对比"服务器到底卡不卡"）

用法
----
>>> report = check_submission(points, step_numbers, meta)
>>> report.ok
>>> report.raise_if_failed()          # 不通过时抛 ComplianceError
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
from dataclasses import dataclass, field as _field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from tsn import geo

# --------------------------------------------------------------------------- #
# 分级与模式
# --------------------------------------------------------------------------- #

TIER_PROTOCOL = 'protocol'        # 服务器解析不了 / WAF 403
TIER_SERVER = 'server'            # 有真实判定证据的硬闸门
TIER_HEURISTIC = 'heuristic'      # 合格样本归纳出的区间，超出只可疑
TIER_SELF = 'self'                # 我们自设的保险
TIER_ORDER = (TIER_PROTOCOL, TIER_SERVER, TIER_HEURISTIC, TIER_SELF)

MODE_STRICT = 'strict'
MODE_BALANCED = 'balanced'
MODE_COMPATIBLE = 'compatible'
MODE_TIERS = {
    MODE_STRICT: set(TIER_ORDER),
    MODE_BALANCED: {TIER_PROTOCOL, TIER_SERVER},
    MODE_COMPATIBLE: {TIER_PROTOCOL},
}

#: 公有接口报文的点位键，9 个缺一不可（真实记录 4984 个点全部齐备）
POINT_KEYS = {'a', 'c', 'e', 'i', 'l', 'm', 'o', 's', 't'}

#: 真实出现过的机型画像（tools/snapshot 与 tests 都用它）
EXPECTED_DEVICE = {
    'app_version': '3.6.814',
    'brand': 'iPhone',
    'model': 'iPhone 17 Pro',
    'system': 'iOS',
    'osver': '27.0.1',
}
DEVICE_ALLOWED = {
    'app_version': {'3.6.814'},
    'brand': {'iPhone'},
    'model': {'iPhone 17 Pro'},
    'system': {'iOS'},
    'osver': {'27.0.1', '27.3'},
}

PACE_MIN_S = 180.0        # 3'00"/km，规则原文下限
PACE_MAX_S = 600.0        # 10'00"/km，规则原文上限
PACE_SAFE_MIN_S = 240.0   # 贴边 4'00" 以下只是提醒
PACE_SAFE_MAX_S = 540.0   # 贴边 9'00" 以上只是提醒（10'00" 那条被罚了）
PACE_RE = re.compile(r'^\d{1,2}\u2032\d{2}\u2033$')
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                     r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

#: 杆位经验区间，全部来自 5 条合格记录（见 docs/合格规格.md 的取证表）
HEUR_STRIDE_M = (0.55, 4.00)          # 实测 0.953 – 3.787
HEUR_CADENCE = (40.0, 200.0)          # 实测 47.2 – 128.5
HEUR_SEGMENT = (20, 180)              # 实测 0 – 171
HEUR_CHAIN_OVER = 0.45                # 实测最大 +37.1%
SERVER_CHAIN_UNDER = -0.002           # 链长比上报短 0.2% 以上 → 自相矛盾
HEUR_STILL_RATIO = (0.002, 0.15)      # 实测 1.3% – 4.9%
HEUR_PAUSE_RATIO = (0.0005, 0.15)     # 实测 0.15% – 6.1%
SERVER_STILL_MAX = 0.30
SERVER_PAUSE_MAX = 0.30
SERVER_POINT_MIN = 300                # 实测 790 – 1346
SERVER_POINT_MAX = 1500
SERVER_SPEED_MEDIAN = (0.3, 6.0)      # 实测 1.53 – 3.58
SERVER_SPEED_MAX = 12.0               # 实测最大 10.26
HEUR_SPEED_MAX = 10.5
SERVER_RANGE_MAX_M = 12000.0          # 实测最大 4.01 km
HEUR_POINT_MIN = 500

HISTORY_FILE = './run_history.json'


class ComplianceError(RuntimeError):
    """门禁不通过。"""


@dataclass(frozen=True)
class ComplianceIssue:
    code: str
    message: str
    severity: str = 'error'          # error / warning
    tier: str = TIER_SERVER
    context: Dict[str, Any] = _field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {'code': self.code, 'message': self.message,
                'severity': self.severity, 'tier': self.tier, **self.context}


@dataclass
class ComplianceReport:
    issues: List[ComplianceIssue] = _field(default_factory=list)
    metrics: Dict[str, Any] = _field(default_factory=dict)
    mode: str = MODE_BALANCED

    # -- 分级视图 ---------------------------------------------------------- #
    @property
    def fatal_tiers(self) -> set:
        return MODE_TIERS.get(self.mode, MODE_TIERS[MODE_BALANCED])

    def _is_fatal(self, issue: ComplianceIssue) -> bool:
        return issue.severity == 'error' and issue.tier in self.fatal_tiers

    @property
    def errors(self) -> List[ComplianceIssue]:
        """会影响 ok 的问题（当前模式下致命）。"""
        return [i for i in self.issues if self._is_fatal(i)]

    @property
    def warnings(self) -> List[ComplianceIssue]:
        return [i for i in self.issues if not self._is_fatal(i)]

    def by_tier(self, tier: str) -> List[ComplianceIssue]:
        return [i for i in self.issues if i.tier == tier]

    @property
    def ok(self) -> bool:
        return not self.errors

    # -- 输出 -------------------------------------------------------------- #
    def summary(self) -> str:
        counts = {t: len(self.by_tier(t)) for t in TIER_ORDER}
        tier_txt = ' '.join(f'{t}={n}' for t, n in counts.items() if n)
        keys = ('distance_m', 'sport_time_s', 'pace_s_per_km', 'total_steps',
                'stride_m', 'cadence_spm', 'points', 'chain_delta_ratio')
        metric_txt = ' '.join(f'{k}={self.metrics.get(k)!r}' for k in keys
                              if self.metrics.get(k) is not None)
        state = '通过' if self.ok else '未通过'
        head = f'合格门禁[{self.mode}] {state}：致命 {len(self.errors)} 警告 {len(self.warnings)}'
        return f'{head} {tier_txt} 指标 {metric_txt}'.strip()

    def detail_lines(self) -> List[str]:
        lines = []
        for issue in self.issues:
            mark = 'x' if self._is_fatal(issue) else '!'
            lines.append(f'  {mark} [{issue.tier}/{issue.severity}] {issue.code}: {issue.message}')
        return lines

    def as_dict(self) -> Dict[str, Any]:
        return {'ok': self.ok, 'mode': self.mode,
                'issues': [i.as_dict() for i in self.issues],
                'metrics': self.metrics}

    def raise_if_failed(self, mode: Optional[str] = None) -> None:
        if mode:
            self.mode = mode
        if self.ok:
            return
        lines = '\n'.join(self.detail_lines())
        raise ComplianceError(f'{self.summary()}\n{lines}')


# --------------------------------------------------------------------------- #
# 基础几何 / 历史
# --------------------------------------------------------------------------- #

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return geo.haversine_distance(lat1, lon1, lat2, lon2)


def _point_lat_lon(point: Dict[str, Any]) -> tuple:
    if 'a' in point and 'o' in point:
        return float(point['a']), float(point['o'])
    return float(point['latitude']), float(point['longitude'])


def recompute_distance(points: Sequence[Dict[str, Any]]) -> float:
    """按点位重算链长（米）。"""
    total = 0.0
    prev = None
    for point in points:
        lat, lon = _point_lat_lon(point)
        if prev is not None:
            total += haversine_distance(prev[0], prev[1], lat, lon)
        prev = (lat, lon)
    return total


def load_history(path: str = HISTORY_FILE) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def record_success(meta: Dict[str, Any], path: str = HISTORY_FILE) -> None:
    history = load_history(path)
    history.append({
        'start_ts_ms': meta.get('start_ts_ms'),
        'sport_range_km': meta.get('sport_range_km'),
        'total_steps': meta.get('total_steps'),
        'identify': meta.get('identify'),
    })
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(history, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _decimals(text: Any) -> int:
    raw = str(text)
    if '.' not in raw:
        return 0
    return len(raw.split('.', 1)[1])


def _parse_segments(step_numbers: Any) -> Optional[List[int]]:
    if isinstance(step_numbers, str):
        try:
            data = json.loads(step_numbers)
        except Exception:
            return None
    else:
        data = step_numbers
    if not isinstance(data, (list, tuple)):
        return None
    try:
        return [int(v) for v in data]
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 检查：点位形态（协议层）
# --------------------------------------------------------------------------- #

def _check_point_schema(points: Sequence[Dict[str, Any]], issues: List[ComplianceIssue],
                        metrics: Dict[str, Any]) -> None:
    if not points:
        issues.append(ComplianceIssue('empty_points', '点位为空', tier=TIER_PROTOCOL))
        return

    bad_keys = [i for i, p in enumerate(points) if set(p) != POINT_KEYS]
    if bad_keys:
        sample = sorted(set(POINT_KEYS) - set(points[bad_keys[0]]))
        issues.append(ComplianceIssue(
            'point_keys', f'{len(bad_keys)}/{len(points)} 个点位的键不是 {sorted(POINT_KEYS)}'
                          f'（首个缺点位 #{bad_keys[0]} 缺 {sample}）', tier=TIER_PROTOCOL,
            context={'first_bad': bad_keys[0]}))

    for index, point in enumerate(points):
        bad_types = [k for k, v in point.items() if not isinstance(v, str)]
        if bad_types:
            issues.append(ComplianceIssue(
                'point_types',
                f'点位值必须是字符串；#{index} 的 {sorted(bad_types)} 是数字',
                tier=TIER_PROTOCOL, context={'first_bad': index}))
            break

    # a/o/s 的 6 位小数只是 App 的形态；位数不对服务器也照收，所以只提醒
    for key, name in (('a', '纬度'), ('o', '经度'), ('s', '瞬时速度')):
        odd = sum(1 for p in points if _decimals(p.get(key, '')) != 6)
        if odd:
            issues.append(ComplianceIssue(
                f'point_precision_{key}', f'{odd}/{len(points)} 个 {name} {key} 不是 6 位小数',
                severity='warning', tier=TIER_HEURISTIC))

    timestamps = [_as_int(p.get('t')) for p in points]
    if any(b < a for a, b in zip(timestamps, timestamps[1:])):
        issues.append(ComplianceIssue('t_not_increasing', 't 出现倒退', tier=TIER_PROTOCOL))
    off = [t % 1000 for t in timestamps]
    jitter = [v for v in off if v not in (0, 1, 2, 998, 999)]
    if jitter and len(jitter) > max(2, len(timestamps) * 0.02):
        issues.append(ComplianceIssue(
            't_jitter', f'{len(jitter)}/{len(timestamps)} 个 t 的毫秒抖动超过 ±2 ms',
            severity='warning', tier=TIER_HEURISTIC,
            context={'max_offset': max(jitter)}))
    duplicates = sum(1 for a, b in zip(timestamps, timestamps[1:]) if b == a)
    metrics['t_duplicates'] = duplicates

    last = points[-1]
    if str(last.get('i')) != 'true':
        issues.append(ComplianceIssue('point_pause_tail',
                                      f'末点 i={last.get("i")!r}，真实合格记录末点恒为 "true"',
                                      tier=TIER_SERVER))
    flags = {str(p.get('l')) for p in points}
    if flags - {'1'}:
        issues.append(ComplianceIssue('point_flag_l', f'l 出现非 "1" 的取值 {sorted(flags)}',
                                      severity='warning', tier=TIER_HEURISTIC))
    m_flags = {str(p.get('m')) for p in points}
    if m_flags - {'0'}:
        issues.append(ComplianceIssue('point_flag_m', f'm 出现非 "0" 的取值 {sorted(m_flags)}',
                                      severity='warning', tier=TIER_HEURISTIC))
    pause = {str(p.get('i')) for p in points}
    if pause - {'true', 'false'}:
        issues.append(ComplianceIssue('point_flag_i', f'i 出现非法取值 {sorted(pause)}',
                                      tier=TIER_PROTOCOL))

    counters = [_as_int(p.get('c')) for p in points]
    drops = sum(1 for a, b in zip(counters, counters[1:]) if b < a)
    if drops:
        issues.append(ComplianceIssue('gps_counter_drop', f'c（GPS 序号）回退 {drops} 次',
                                      severity='warning', tier=TIER_HEURISTIC))
    if counters and not 0 <= counters[0] <= 15:
        issues.append(ComplianceIssue('gps_counter_start',
                                      f'c 起点 {counters[0]} 不在 0–15（实测 0/4/5/15）',
                                      severity='warning', tier=TIER_HEURISTIC))
    metrics['gps_counter_first'] = counters[0] if counters else None
    metrics['gps_counter_last'] = counters[-1] if counters else None


# --------------------------------------------------------------------------- #
# 检查：时序与里程（协议层 + 硬闸门）
# --------------------------------------------------------------------------- #

def _check_series(points: Sequence[Dict[str, Any]], step_numbers: Any, meta: Dict[str, Any],
                  issues: List[ComplianceIssue], metrics: Dict[str, Any]) -> None:
    timestamps = [_as_int(p.get('t')) for p in points]
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]
    if deltas:
        ordered = sorted(deltas)
        median = ordered[len(ordered) // 2]
        metrics['dt_median_ms'] = median
        metrics['dt_max_ms'] = max(deltas)
        metrics['dt_zero'] = sum(1 for d in deltas if d == 0)
        if not 985 <= median <= 1015:
            issues.append(ComplianceIssue(
                'dt_median', f'采样间隔中位数 {median} ms，真实记录恒为 1000 ms',
                tier=TIER_SERVER, context={'median': median}))
        odd = sum(1 for d in deltas if d % 1000 not in (0, 999, 1) and d != 0)
        if odd > max(2, len(deltas) * 0.03):
            issues.append(ComplianceIssue(
                'dt_odd', f'{odd}/{len(deltas)} 个间隔不是整秒倍数', severity='warning',
                tier=TIER_HEURISTIC))

    speeds = [_as_float(p.get('s')) for p in points]
    if speeds:
        ordered = sorted(speeds)
        median = ordered[len(ordered) // 2]
        metrics['speed_median_mps'] = round(median, 4)
        metrics['speed_max_mps'] = round(max(speeds), 4)
        metrics['speed_mean_mps'] = round(sum(speeds) / len(speeds), 4)
        low, high = SERVER_SPEED_MEDIAN
        if not low <= median <= high:
            issues.append(ComplianceIssue('speed_median',
                                          f'瞬时速度中位数 {median:.3f} m/s 超出 {low}–{high}',
                                          tier=TIER_SERVER))
        if max(speeds) > SERVER_SPEED_MAX:
            issues.append(ComplianceIssue('speed_max',
                                          f'瞬时速度峰值 {max(speeds):.3f} m/s > {SERVER_SPEED_MAX}',
                                          tier=TIER_SERVER))
        elif max(speeds) > HEUR_SPEED_MAX:
            issues.append(ComplianceIssue('speed_peak_hint',
                                          f'瞬时速度峰值 {max(speeds):.3f} m/s 高于实测最高 10.26',
                                          severity='warning', tier=TIER_HEURISTIC))
        still = sum(1 for v in speeds if v <= 0.0) / len(speeds)
        metrics['still_ratio'] = round(still, 4)
        if still > SERVER_STILL_MAX:
            issues.append(ComplianceIssue('still_ratio',
                                          f'零速点占比 {still:.2%} > {SERVER_STILL_MAX:.0%}',
                                          tier=TIER_SERVER))
        elif not HEUR_STILL_RATIO[0] <= still <= HEUR_STILL_RATIO[1]:
            issues.append(ComplianceIssue('still_ratio_hint',
                                          f'零速点占比 {still:.2%} 不在实测区间 '
                                          f'{HEUR_STILL_RATIO[0]:.1%}–{HEUR_STILL_RATIO[1]:.0%}',
                                          severity='warning', tier=TIER_HEURISTIC))

    paused = sum(1 for p in points if str(p.get('i')) == 'true')
    pause_ratio = paused / len(points) if points else 0.0
    metrics['pause_ratio'] = round(pause_ratio, 4)
    if pause_ratio > SERVER_PAUSE_MAX:
        issues.append(ComplianceIssue('pause_ratio', f'暂停点占比 {pause_ratio:.2%} 过高',
                                      tier=TIER_SERVER))
    elif not HEUR_PAUSE_RATIO[0] <= pause_ratio <= HEUR_PAUSE_RATIO[1]:
        issues.append(ComplianceIssue('pause_ratio_hint',
                                      f'暂停点占比 {pause_ratio:.2%} 不在实测区间 '
                                      f'{HEUR_PAUSE_RATIO[0]:.2%}–{HEUR_PAUSE_RATIO[1]:.0%}',
                                      severity='warning', tier=TIER_HEURISTIC))

    # e 是累计步数，末点必须等于上报步数
    cumulative = [_as_int(p.get('e')) for p in points]
    steps = _as_int(meta.get('total_steps'), cumulative[-1] if cumulative else 0)
    if cumulative and cumulative[-1] != steps:
        issues.append(ComplianceIssue('steps_tail',
                                      f'末点 e={cumulative[-1]} ≠ 上报 step={steps}',
                                      tier=TIER_SERVER))
    if any(b < a for a, b in zip(cumulative, cumulative[1:])):
        issues.append(ComplianceIssue('steps_counter_drop', 'e（累计步数）出现回退',
                                      severity='warning', tier=TIER_HEURISTIC))

    # 步数分段
    segments = _parse_segments(step_numbers)
    if segments is None:
        issues.append(ComplianceIssue('step_numbers_parse', 'stepNumbers 不是合法 JSON 数组',
                                      tier=TIER_PROTOCOL))
    else:
        metrics['segment_count'] = len(segments)
        if not segments:
            issues.append(ComplianceIssue('step_numbers_missing', 'stepNumbers 为空',
                                          tier=TIER_SERVER))
        else:
            metrics['segment_min'] = min(segments)
            metrics['segment_max'] = max(segments)
            if sum(segments) != steps:
                issues.append(ComplianceIssue('step_numbers_sum',
                                              f'stepNumbers 求和 {sum(segments)} ≠ step {steps}',
                                              tier=TIER_SERVER))
            if not 8 <= len(segments) <= 30:
                issues.append(ComplianceIssue('segment_count_hint',
                                              f'分段数 {len(segments)} 不在实测 15–23',
                                              severity='warning', tier=TIER_HEURISTIC))
            low, high = HEUR_SEGMENT
            outside = [v for v in segments[:-1] if not low <= v <= high]
            if outside:
                issues.append(ComplianceIssue('segment_size_hint',
                                              f'{len(outside)} 个分段超出实测 {low}–{high}',
                                              severity='warning', tier=TIER_HEURISTIC))

    # 链长 vs 上报里程：服务器信上报值，链长不能比它短
    reported = _as_float(meta.get('distance_m'))
    if reported > 0:
        chain = recompute_distance(points)
        delta = chain / reported - 1.0
        metrics['chain_distance_m'] = round(chain, 2)
        metrics['chain_delta_ratio'] = round(delta, 5)
        if delta < SERVER_CHAIN_UNDER:
            issues.append(ComplianceIssue(
                'chain_short', f'点位链长 {chain:.0f} m 比上报 {reported:.0f} m 短 {delta:.2%}',
                tier=TIER_SERVER))
        elif delta > HEUR_CHAIN_OVER:
            issues.append(ComplianceIssue(
                'chain_over_hint', f'链长比上报多 {delta:.2%}（实测最大 +37.1%，服务器照收）',
                severity='warning', tier=TIER_HEURISTIC))


# --------------------------------------------------------------------------- #
# 检查：标量闸门（硬闸门）
# --------------------------------------------------------------------------- #

def _check_scalars(meta: Dict[str, Any], issues: List[ComplianceIssue],
                   metrics: Dict[str, Any]) -> None:
    distance = _as_float(meta.get('distance_m'))
    elapsed = _as_float(meta.get('sport_time_s'))
    steps = _as_int(meta.get('total_steps'))
    range_setting = _as_float(meta.get('range_setting'), 0.0)
    step_setting = _as_int(meta.get('step_setting'), 0)

    metrics['distance_m'] = round(distance, 2)
    metrics['sport_time_s'] = int(elapsed)
    metrics['total_steps'] = steps

    if distance <= 0:
        issues.append(ComplianceIssue('distance_zero', '上报里程为 0', tier=TIER_SERVER))
    if distance > SERVER_RANGE_MAX_M:
        issues.append(ComplianceIssue('range_high', f'里程 {distance:.0f} m 超过单次上限',
                                      tier=TIER_SERVER))

    # 距离闸门 = rangeSetting（2.000 km）。2.000 整 + 10'00" 整被判作弊，所以要留余量。
    required_range = _as_float(meta.get('required_range_m'), range_setting * 1000.0)
    metrics['required_range_m'] = round(required_range, 2)
    if required_range > 0:
        if distance < required_range:
            issues.append(ComplianceIssue(
                'range_low', f'里程 {distance:.0f} m < 要求 {required_range:.0f} m',
                tier=TIER_SERVER))
        elif distance < required_range * 1.02:
            issues.append(ComplianceIssue(
                'range_edge', f'里程只比要求多 {(distance / required_range - 1):.2%}，'
                              f'建议 ≥ +2%（被罚那条正好是 2.000 整）',
                severity='warning', tier=TIER_SERVER))

    # 步数闸门 = stepSetting（步数 < 1000 → 运动步数不符合）
    if step_setting > 0:
        metrics['step_setting'] = step_setting
        if steps < step_setting:
            issues.append(ComplianceIssue(
                'step_setting_low', f'步数 {steps} < stepSetting {step_setting}'
                                    f'（999 步被判「运动步数不符合131310」）',
                tier=TIER_SERVER))

    # 时长窗口 = [startQualifiedTime, endQualifiedTime] 分钟（服务器按距离放大 end）
    pace_s = elapsed / (distance / 1000.0) if distance > 0 else 0.0
    metrics['pace_s_per_km'] = round(pace_s, 1)
    if distance > 0 and elapsed > 0:
        if not PACE_MIN_S <= pace_s <= PACE_MAX_S:
            issues.append(ComplianceIssue(
                'pace_out_of_rule', f'配速 {pace_s:.0f} s/km 超出 {PACE_MIN_S:.0f}–{PACE_MAX_S:.0f}',
                tier=TIER_SERVER))
        elif not PACE_SAFE_MIN_S <= pace_s <= PACE_SAFE_MAX_S:
            issues.append(ComplianceIssue(
                'pace_edge', f'配速 {pace_s:.0f} s/km 贴着规则边界（10′00″ 那条被罚）',
                severity='warning', tier=TIER_SERVER))

    start_min = _as_float(meta.get('start_qualified_min'), -1.0)
    end_min = _as_float(meta.get('end_qualified_min'), -1.0)
    if start_min >= 0 and elapsed and elapsed < start_min * 60:
        issues.append(ComplianceIssue(
            'duration_short', f'时长 {elapsed:.0f}s < 下限 {start_min * 60:.0f}s', tier=TIER_SERVER))
    if end_min > 0 and elapsed > end_min * 60:
        issues.append(ComplianceIssue(
            'duration_long', f'时长 {elapsed:.0f}s > 上限 {end_min * 60:.0f}s', tier=TIER_SERVER))

    stride = distance / steps if steps else 0.0
    cadence = steps / elapsed * 60.0 if elapsed else 0.0
    metrics['stride_m'] = round(stride, 3)
    metrics['cadence_spm'] = round(cadence, 1)
    if steps and not HEUR_STRIDE_M[0] <= stride <= HEUR_STRIDE_M[1]:
        issues.append(ComplianceIssue(
            'stride_hint', f'步幅 {stride:.2f} m 不在实测 {HEUR_STRIDE_M[0]}–{HEUR_STRIDE_M[1]} m'
                           f'（合格记录最大 3.787 m，服务器不看这个）',
            severity='warning', tier=TIER_HEURISTIC))
    if elapsed and not HEUR_CADENCE[0] <= cadence <= HEUR_CADENCE[1]:
        issues.append(ComplianceIssue(
            'cadence_hint', f'步频 {cadence:.0f} 步/分 不在实测 {HEUR_CADENCE[0]:.0f}–{HEUR_CADENCE[1]:.0f}'
                           f'（合格记录最低 47.2）',
            severity='warning', tier=TIER_HEURISTIC))

    identify = str(meta.get('identify') or '')
    if not UUID_RE.match(identify):
        issues.append(ComplianceIssue('identify', f'identify={identify!r} 不是 UUID'
                                                 f'（被罚记录是 "1"）', tier=TIER_SERVER))

    pace = str(meta.get('pace') or '')
    if pace and not PACE_RE.match(pace):
        issues.append(ComplianceIssue('pace_format',
                                      f'配速串 {pace!r} 不是 MM′SS″（U+2032/U+2033）',
                                      tier=TIER_PROTOCOL))

    sport_type = meta.get('sport_type')
    if sport_type is not None and str(sport_type) != '1':
        issues.append(ComplianceIssue('sport_type',
                                      f'sportType={sport_type}，只有 1（阳光跑）计入成绩',
                                      severity='warning', tier=TIER_SERVER))

    if not meta.get('face_ok', True):
        issues.append(ComplianceIssue(
            'face_status', 'isFaceStatus 不为 1 / 人脸未全部上传（人脸最短路口，先于步数判定）',
            tier=TIER_SERVER))

    ok_points = _as_int(meta.get('ok_point_count'), 0)
    if meta.get('sport_type') in (None, '1', 1) and ok_points <= 0:
        issues.append(ComplianceIssue('ok_points', f'有效点数 {ok_points}', severity='warning',
                                      tier=TIER_HEURISTIC))

    points = _as_int(meta.get('point_count'), 0)
    if points and not SERVER_POINT_MIN <= points <= SERVER_POINT_MAX:
        issues.append(ComplianceIssue('point_count',
                                      f'点数 {points} 不在 {SERVER_POINT_MIN}–{SERVER_POINT_MAX}',
                                      tier=TIER_SERVER))

    start_ts = _as_int(meta.get('start_ts_ms'), 0)
    end_ts = _as_int(meta.get('end_ts_ms'), 0)
    if start_ts and end_ts:
        start_dt = _dt.datetime.fromtimestamp(start_ts / 1000.0)
        end_dt = _dt.datetime.fromtimestamp(end_ts / 1000.0)
        metrics['start_local'] = start_dt.strftime('%Y-%m-%d %H:%M:%S')
        metrics['end_local'] = end_dt.strftime('%Y-%m-%d %H:%M:%S')
        if end_dt.date() != start_dt.date():
            issues.append(ComplianceIssue('cross_day', '起终点跨天', tier=TIER_SERVER))
        if not 5 <= start_dt.hour < 22:
            issues.append(ComplianceIssue('time_of_day',
                                          f'开始时间 {start_dt:%H:%M} 不在 05:00–22:00'
                                          f'（真实合格记录最晚 21:10）',
                                          severity='warning', tier=TIER_HEURISTIC))


# --------------------------------------------------------------------------- #
# 检查：设备画像（自设）
# --------------------------------------------------------------------------- #

def _check_device(meta: Dict[str, Any], issues: List[ComplianceIssue]) -> None:
    mapping = {'app_version': 'app_version', 'brand': 'brand', 'model': 'model',
               'system': 'system', 'osver': 'osver'}
    for key, meta_key in mapping.items():
        got = meta.get(meta_key)
        if got is None:
            continue
        allowed = DEVICE_ALLOWED[key]
        if str(got) not in allowed:
            issues.append(ComplianceIssue(
                f'device_{key}',
                f'{meta_key}={got!r} 不在真实出现过的取值 {sorted(allowed)}'
                f'（被罚记录是 2.0.23/Xiaomi/Android 14）',
                severity='warning', tier=TIER_SELF))


# --------------------------------------------------------------------------- #
# 检查：围栏
# --------------------------------------------------------------------------- #

def _ring_points(fence: Any) -> List[Dict[str, Any]]:
    if not fence:
        return []
    if isinstance(fence, dict):
        if fence.get('type') == 'FeatureCollection':
            out = []
            for feature in fence.get('features') or []:
                out.extend(_ring_points(feature.get('geometry')))
            return out
        coords = fence.get('coordinates')
        if isinstance(coords, list):
            flat = coords[0] if coords and isinstance(coords[0], list) else coords
            out = []
            for item in flat:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.append({'lng': item[0], 'lat': item[1]})
                elif isinstance(item, dict):
                    out.append(item)
            return out
        return []
    if isinstance(fence, list):
        # 已经是 [{'lng':..,'lat':..}, ...] 的单环：直接就是顶点表。
        # 早期实现漏了这一支，导致 backfill 下发的围栏（正是这种形态）
        # 被解析成 0 个顶点，_check_fence 一进门就 len(ring) < 3 返回。
        if fence and all(isinstance(v, dict) and 'lat' in v and 'lng' in v for v in fence):
            return [v for v in fence if _as_float(v.get('lat')) is not None]
        out = []
        for entry in fence:
            out.extend(_ring_points(entry))
        return out
    return []


def _inside(lat: float, lon: float, ring: Sequence[Dict[str, Any]]) -> bool:
    if len(ring) < 3:
        return True
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = _as_float(ring[i].get('lat')), _as_float(ring[i].get('lng'))
        yj, xj = _as_float(ring[j].get('lat')), _as_float(ring[j].get('lng'))
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _check_fence(points: Sequence[Dict[str, Any]], fence: Any,
                 issues: List[ComplianceIssue], metrics: Dict[str, Any]) -> None:
    ring = _ring_points(fence)
    if len(ring) < 3:
        return
    outside = 0
    for point in points:
        lat, lon = _point_lat_lon(point)
        if not _inside(lat, lon, ring):
            outside += 1
    ratio = outside / len(points) if points else 0.0
    metrics['fence_outside_ratio'] = round(ratio, 4)
    if ratio > 0.10:
        issues.append(ComplianceIssue('fence_outside',
                                      f'{ratio:.1%} 的点在考勤围栏外（>10%）', tier=TIER_SERVER))
    elif outside:
        issues.append(ComplianceIssue('fence_outside_hint',
                                      f'{outside}/{len(points)} 个点在围栏外',
                                      severity='warning', tier=TIER_HEURISTIC))


# --------------------------------------------------------------------------- #
# 检查：频率
# --------------------------------------------------------------------------- #

def _check_frequency(meta: Dict[str, Any], history: Optional[Sequence[Dict[str, Any]]],
                     issues: List[ComplianceIssue]) -> None:
    if history is None:
        return
    start_ts = _as_int(meta.get('start_ts_ms'), 0)
    if not start_ts:
        return
    day = _dt.datetime.fromtimestamp(start_ts / 1000.0).date()
    for entry in history:
        other = _as_int(entry.get('start_ts_ms'), 0)
        if not other:
            continue
        if _dt.datetime.fromtimestamp(other / 1000.0).date() == day:
            issues.append(ComplianceIssue('frequency_same_day',
                                          f'{day} 已经提交过一条记录', tier=TIER_SERVER))
            return


# --------------------------------------------------------------------------- #
# 总入口
# --------------------------------------------------------------------------- #

def check_submission(points: Sequence[Dict[str, Any]],
                     step_numbers: Any = None,
                     meta: Optional[Dict[str, Any]] = None,
                     fence: Any = None,
                     history: Optional[Sequence[Dict[str, Any]]] = None,
                     mode: str = MODE_BALANCED) -> ComplianceReport:
    """提交前自检。

    ``meta`` 需要的键（缺省时按 0/None 处理）::

        distance_m, sport_time_s, total_steps, identify, pace, sport_type,
        required_range_m, range_setting, step_setting,
        start_qualified_min, end_qualified_min, face_ok, ok_point_count,
        point_count, start_ts_ms, end_ts_ms,
        app_version, brand, model, system, osver
    """
    meta = dict(meta or {})
    points = list(points or [])
    if 'point_count' not in meta:
        meta['point_count'] = len(points)
    if 'total_steps' not in meta and points:
        meta['total_steps'] = _as_int(points[-1].get('e'))
    if 'distance_m' not in meta:
        meta['distance_m'] = recompute_distance(points)

    issues: List[ComplianceIssue] = []
    metrics: Dict[str, Any] = {'points': len(points)}

    _check_point_schema(points, issues, metrics)
    _check_series(points, step_numbers, meta, issues, metrics)
    _check_scalars(meta, issues, metrics)
    _check_device(meta, issues)
    _check_fence(points, fence, issues, metrics)
    _check_frequency(meta, history, issues)

    return ComplianceReport(issues=issues, metrics=metrics, mode=mode)
