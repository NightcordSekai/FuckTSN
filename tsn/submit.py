"""提交报文字段工厂：把"设置 + 轨迹 + 时长步数"折算成 addExerciseRecord 的字段。

这一层是整条链路的翻译官，服务端的判定完全依赖它产出的几个标量：
`sportRange` / `gpsDistance` / `stepNumbers` / `sportTime` / `speed` / `avgSpeed`
以及风险位 `d/f/m/h`。逆向结论如下（依据 v3.6.814 真机记录 + 服务端返回）：

1. 距离与步数：服务器**不**从 pointList 重算距离，只信 `sportRange`（公里，
   两位小数的字符串）与 `gpsDistance`。因此链长只要 ≥ 上报值即可，多余部分
   不会被追责（真机合格样本里链长最多比上报值高 37%）。
2. 合格线（isValid）：`sportRange` 达到 rangeSetting，`step` 达到 stepSetting，
   且 sportTime 落在 {startQualifiedTime, endQualifiedTime} 窗口内。三个条件
   与 App 自己算 isValid 的逻辑必须一致 —— 我们照抄的是 App 的算法，所以
   isValid 只是"回显"，服务器会重算并覆盖它。
3. 步数不足的直接后果是 remark「运动步数不符合」（实测 999 步 / 2.090 km 被
   判掉，1015 步同样距离通过，说明卡的是 stepSetting=1000 而不是步幅）。
4. 风险位 d/f/m/h 由点位里的 f/m/h 标记与"开发者模式/离线"聚合而成，正常
   真机应当全 0；非 0 会让人工复核介入。
"""
from __future__ import annotations

import base64
import hashlib
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# App 内硬编码的签名哈希（由 APK 签名证书 DER 的 sha256 再 sha256 得到，见 app_sign_hash）
KNOWN_APP_SIGN_HASH = 'd2tnIyqximO/L8Y4MzfRELa1hSAtSRxzmvXlcOCzRyk='

_TRUTHY = {'1', 'true', 'yes', 'y', 't'}


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


# ---------------------------------------------------------------------------
# 签名 / 会话密钥
# ---------------------------------------------------------------------------

def colon_hex_upper(digest: bytes) -> str:
    """b'\\xab\\xcd' -> 'AB:CD'。"""
    return ':'.join(f'{byte:02X}' for byte in digest)


def app_sign_hash(cert_der: bytes) -> str:
    """APK 签名证书 → App 内的 appSignHash（两层 sha256 再 base64）。"""
    first = hashlib.sha256(cert_der).digest()
    second = hashlib.sha256(colon_hex_upper(first).encode()).digest()
    return base64.b64encode(second).decode()


def app_sign_hash_from_cert_b64(cert_b64: str) -> str:
    return app_sign_hash(base64.b64decode(cert_b64))


def session_key(token: str, timestamp: str) -> str:
    """每次请求的随机 AES key：md5(token + timestamp) 的前 16 个字符。"""
    return hashlib.md5(f'{token}{timestamp}'.encode()).hexdigest()[0:16]


def _cbc_key_iv(token: str):
    digest = hashlib.md5(token.encode()).hexdigest()
    return digest[0:16], digest[16:32]


def _aes_cbc_b64(plain: str, key: str, iv: str) -> str:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
    return base64.b64encode(cipher.encrypt(pad(plain.encode('utf-8'), 16))).decode()


def old_sign(auth_info: str, token: str) -> str:
    """旧版 sign：AES/CBC/PKCS7 的 base64 密文（key/iv 来自 md5(token)）。"""
    import urllib.parse

    key, iv = _cbc_key_iv(token)
    return _aes_cbc_b64(urllib.parse.unquote(auth_info), key, iv)


def sign_auth_info(auth_info: str, token: str) -> str:
    """新版 sign：上面那段密文再做一次 md5 十六进制。"""
    return hashlib.md5(old_sign(auth_info, token).encode()).hexdigest()


def build_auth_info(params: Mapping[str, Any]) -> str:
    """按 key 排序拼 a=b&c=d；None→null，bool→true/false，不做 urlencode。"""
    parts: List[str] = []
    for key in sorted(params.keys()):
        value = params[key]
        if value is None:
            text = 'null'
        elif isinstance(value, bool):
            text = 'true' if value else 'false'
        else:
            text = str(value)
        parts.append(f'{key}={text}')
    return '&'.join(parts)


def decrypt_param(param_b64: str, key_b64: str) -> str:
    """AES/ECB/PKCS7 解出 param（key 由 RSA 私钥解出的明文 key 经 base64 之后）。"""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    key = base64.b64decode(key_b64)
    data = base64.b64decode(param_b64)
    return unpad(AES.new(key, AES.MODE_ECB).decrypt(data), 16).decode('utf-8')


# ---------------------------------------------------------------------------
# 风险位
# ---------------------------------------------------------------------------

def point_risk_flags(point: Mapping[str, Any]) -> Dict[str, bool]:
    """点位里的 f/m/h 三枚标记（f=改机, m=模拟, h=外挂）。"""
    return {flag: _truthy(point.get(flag)) for flag in ('f', 'm', 'h')}


def aggregate_risk_flags(
    points: Optional[Iterable[Mapping[str, Any]]] = None,
    user_open_develop: bool = False,
    offline: int = 0,
) -> Dict[str, int]:
    """聚合风险位：d=开发者模式/离线次数，f/m/h=点位标记计数。"""
    counts = {'f': 0, 'm': 0, 'h': 0}
    for point in points or []:
        for flag, hit in point_risk_flags(point).items():
            if hit:
                counts[flag] += 1
    return {
        'd': (1 if user_open_develop else 0) + int(offline or 0),
        'f': counts['f'],
        'm': counts['m'],
        'h': counts['h'],
    }


# ---------------------------------------------------------------------------
# 合格线
# ---------------------------------------------------------------------------

def total_range_meters(setting: Mapping[str, Any]) -> float:
    """``totalRange``（公里）→ 米。

    **注意字段语义在公有接口里变了**：私有接口的 totalRange 是"单次要求"，
    公有接口（getExerciseSetting / getExerciseRecord）里它是**阶段累计**——
    真实记录回显 totalRange="14.200"、leaveTotalRangeZong="65.800"，
    是学期累计与剩余额度，不是单次要跑 14.2 km。

    单次要求请用 :func:`per_run_range_meters`（读 rangeSetting）。
    """
    return _as_float(setting.get('totalRange'), 0.0) * 1000.0


def per_run_range_meters(setting: Mapping[str, Any]) -> float:
    """单次要求里程（米）：公有接口读 ``rangeSetting``，老报文回落到 ``totalRange``。

    取证：rangeSetting="2.000" 的记录被判合格；同一份 setting 里 totalRange="14.200"
    显然不是单次门槛（没有一条记录跑过 14.2 km）。
    """
    per_run = _as_float(setting.get('rangeSetting'), 0.0)
    if per_run > 0:
        return per_run * 1000.0
    return total_range_meters(setting)


def required_steps(setting: Mapping[str, Any], distance_m: float) -> int:
    """应跑步数：基准是 stepSetting；开了步数递推时按超出倍数线性放大。

    递推的「一圈」是 ``totalRange``（阶段累计目标），**不是**单次门槛 ``rangeSetting``。

    取证（5 条服务器判「合格」的真实记录，tests/fixtures/records_qualified.json）：
    sportRange=4.010、step=1059 的那条 `remark` 就是「合格」——如果按 rangeSetting
    （2 km）做分母递推，它会要求 2005 步，直接判「步数不足」；按 totalRange
    （14.2 km）做分母则 4.01 km 仍未超一圈，要求恒为 1000 步，与实测全对上。
    """
    base = _as_int(setting.get('step'), 0) or _as_int(setting.get('stepSetting'), 0)
    lap = total_range_meters(setting) or per_run_range_meters(setting)
    if _truthy(setting.get('isStepRecursion')) and lap > 0 and distance_m > lap:
        return int(math.ceil(base * (distance_m / lap)))
    return base


def adaptive_end_qualified_time(setting: Mapping[str, Any], distance_m: float) -> int:
    """结束时间窗口（分钟）：-1 表示不限制；距离超过一圈时按比例放大后向上取整。

    注意这是 App 侧的宽松口径（21 分钟 × 1.045 圈 → 22 分钟），服务器实际判定
    用的是它自己那份 setting，所以这里只用于回显与日志。
    """
    end_q = _as_int(setting.get('endQualifiedTime'), -1)
    total = per_run_range_meters(setting)
    if end_q != -1 and total > 0 and distance_m > total:
        return int(math.ceil(end_q * (distance_m / total)))
    return end_q


def is_valid_qualified(
    setting: Mapping[str, Any],
    elapsed_s: float,
    distance_m: float,
    steps: int,
) -> bool:
    """App 侧的合格判定：时间窗口 + 距离 + 步数 三条同时满足。"""
    start_q = _as_int(setting.get('startQualifiedTime'), 0) * 60
    required = per_run_range_meters(setting)
    end_q = adaptive_end_qualified_time(setting, distance_m)
    in_window = elapsed_s >= start_q or (end_q != -1 and elapsed_s <= end_q * 60)
    return bool(
        in_window
        and distance_m >= required
        and steps >= required_steps(setting, distance_m)
    )


def compute_is_valid(setting: Mapping[str, Any], elapsed_s: float, distance_m: float, steps: int) -> int:
    return 1 if is_valid_qualified(setting, elapsed_s, distance_m, steps) else 0


def qualification_detail(
    setting: Mapping[str, Any],
    elapsed_s: float,
    distance_m: float,
    steps: int,
) -> Dict[str, Any]:
    """把合格判定的每一项摊开，写进日志以便复盘"为什么合格/不合格"。"""
    return {
        'startQualifiedTime_min': _as_int(setting.get('startQualifiedTime'), 0),
        'endQualifiedTime_min': adaptive_end_qualified_time(setting, distance_m),
        'elapsed_s': round(float(elapsed_s), 1),
        'per_run_range_m': per_run_range_meters(setting),
        'totalRange_m': total_range_meters(setting),
        'distance_m': round(float(distance_m), 1),
        'required_steps': required_steps(setting, distance_m),
        'steps': int(steps),
        'isValid': compute_is_valid(setting, elapsed_s, distance_m, steps),
    }


# ---------------------------------------------------------------------------
# 备注（运动类型）
# ---------------------------------------------------------------------------

def remark_options(setting: Mapping[str, Any]) -> List[Dict[str, Any]]:
    options = setting.get('remarks') or []
    return [dict(item) for item in options if isinstance(item, dict)]


def remark_id(setting: Mapping[str, Any]) -> Optional[str]:
    value = setting.get('remarkId')
    if value in (None, '', 0, '0'):
        return None
    return str(value)


def select_remark(setting: Mapping[str, Any], name_or_id: Any) -> Optional[str]:
    """按 remarkId 或名称挑一个运动类型，返回 remarkId。"""
    if name_or_id in (None, ''):
        return remark_id(setting)
    wanted = str(name_or_id)
    for item in remark_options(setting):
        if str(item.get('id')) == wanted:
            return wanted
    for item in remark_options(setting):
        if str(item.get('name')) == wanted:
            return str(item.get('id'))
    return None


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def build_submit_extras(
    setting: Mapping[str, Any],
    points: Optional[Iterable[Mapping[str, Any]]] = None,
    elapsed_s: float = 0.0,
    distance_m: float = 0.0,
    steps: int = 0,
    user_open_develop: Optional[bool] = None,
    offline: int = 0,
    include_is_valid: bool = True,
) -> Dict[str, Any]:
    """addExerciseRecord 里那几个"软字段"：d/f/m/h (+isValid) (+remarkId)。"""
    if user_open_develop is None:
        user_open_develop = _truthy(setting.get('userOpenDevelop', False))
    extras: Dict[str, Any] = dict(
        aggregate_risk_flags(points, user_open_develop=bool(user_open_develop), offline=offline)
    )
    if include_is_valid:
        extras['isValid'] = compute_is_valid(setting, elapsed_s, distance_m, steps)
    rid = remark_id(setting)
    if rid is not None:
        extras['remarkId'] = rid
    return extras


def describe_submit_fields(fields: Mapping[str, Any]) -> str:
    """一行日志：把真正影响判定的标量列出来。"""
    keys = ('sportType', 'sportTime', 'sportRange', 'speed', 'avgSpeed',
            'gpsDistance', 'limitSpeed', 'limitStride', 'limitStepFrequency')
    shown = ', '.join(f'{k}={fields.get(k)}' for k in keys if k in fields)
    return f'{shown}, step={fields.get("step", "-")}'


__all__ = [
    'KNOWN_APP_SIGN_HASH', 'colon_hex_upper', 'app_sign_hash', 'app_sign_hash_from_cert_b64',
    'session_key', 'old_sign', 'sign_auth_info', 'build_auth_info', 'decrypt_param',
    'point_risk_flags', 'aggregate_risk_flags',
    'total_range_meters', 'required_steps', 'adaptive_end_qualified_time',
    'is_valid_qualified', 'compute_is_valid', 'qualification_detail',
    'remark_options', 'remark_id', 'select_remark', 'build_submit_extras', 'describe_submit_fields',
]
