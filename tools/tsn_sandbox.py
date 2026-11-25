#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地协议沙盘：把 `https://h.tsnkj.com` 的服务端契约在内存里跑起来。

为什么需要它
------------
这个仓库里每一条链路（AES(JSON) + RSA(key) + `/v` 校验 + `sign` 头 + GET/POST
编码差异 + 九键点串 + 三类人脸 + 判定与配额）都是**纯客户端事实**，但它们只在
"有服务端按真实规则回话"时才可验证。没有账号时，真实服务端一个字都不会回。

于是这里做一件事：按真实报文契约实现一个**假服务端**，用 `httpx.MockTransport`
挂在客户端底下：

    TiShiNengSdkPublic → TsnHttpCore → httpx.MockTransport → 本模块

请求照旧被签名、被 AES 加密、被 RSA 包 key、被 quote 两次；本模块照旧解开它们、
复算 sign、按真实判定规则回 status/sportStatus/remark/reachRange。也就是说：
**这里判合格，真实服务端也判合格**——差的是设备风控画像与服务器策略，不是协议。

逆向得到的关键契约（本模块逐条实现）
----------------------------------
* `key`   = RSA(base64) 包住 16 字节会话 AES key；无私钥，故替换成可逆编解码
* `param` = AES-ECB/PKCS7(紧凑 JSON, key=会话 key)
* `v`     = AES-ECB/PKCS7(appSignHash)，藏在密文里，服务端用来认正版 App
* `sign`  = md5(aesUtils(token).encrypt(unquote(build_auth_info(params + appId + appSecret))))
  —— 注意：**签的是密文字段 `{'key','param'}`**，不是明文参数
* GET 的原值把 base64 的 `+` 换成空格；POST 加密体先 `quote` 一次再交给 httpx
  —— 所以服务端必须做「宽松解码」，这里用候选枚举把它做成确定性的
* 合格判据：step ≥ stepSetting、sportRange ≥ rangeSetting、sportTime 在窗口内、
  三类人脸齐全、identify 是 UUID、每天一条；合格时 `reachRange == sportRange`

用法
----
    from tools.tsn_sandbox import Sandbox
    box = Sandbox()
    box.attach(tsn)            # 必须在任何业务请求之前
    tsn.setToken(box.token)
    ...
    box.dump('logs/sandbox_report.json')
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import random
import re
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from tsn import compliance
from tsn import routes
from tsn import submit
from tsn.crypto import AESCrypto

# --------------------------------------------------------------------------- #
# 常量：与客户端硬编码逐字对齐（客户端才是事实来源）
# --------------------------------------------------------------------------- #

APP_ID = 'c9292ee89d2f49492f983f5931af0d09'
APP_SIGN_HASH = submit.KNOWN_APP_SIGN_HASH
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                     r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
PACE_RE = re.compile(r'^(\d{1,2})′(\d{2})″$')
SANDBOX_TOKEN = 'sandbox.' + hashlib.md5(b'tsn-sandbox-v1').hexdigest()
CODEC_TAG = b'SBX1'


def md5(text: str) -> str:
    return hashlib.md5(str(text).encode('utf-8')).hexdigest()


def _fmt_seconds(seconds: float) -> str:
    return f'{int(seconds // 60)}′{int(seconds % 60):02d}″'


def _pace_seconds(text: Any) -> Optional[float]:
    hit = PACE_RE.match(str(text or ''))
    if not hit:
        return None
    return int(hit.group(1)) * 60 + int(hit.group(2))


def _worst_window_stride(points: List[Dict[str, Any]], window_ms: Optional[int] = None) -> float:
    """按 `window_ms` 分段，返回最差那一段的「1 步里程」（米/步）。

    对应官方帮助中心那条「某一时间段步幅超出正常范围，1 步里程大于 2.5 米」：
    **不是**整条均值，而是任意一段的最大值，所以要逐段算再取 max。
    点数不足或时间戳缺失时返回 0.0（交给其它闸门去判，不在这里误杀）。
    """
    span = window_ms or 60_000
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    try:
        base = int(str(points[0].get('t') or 0))
    except (TypeError, ValueError, IndexError):
        return 0.0
    for point in points:
        try:
            key = (int(str(point.get('t') or 0)) - base) // span
            int(str(point.get('e') or 0))
        except (TypeError, ValueError):
            continue
        buckets.setdefault(key, []).append(point)
    worst = 0.0
    for chunk in buckets.values():
        if len(chunk) < 2:
            continue
        try:
            steps = int(str(chunk[-1].get('e') or 0)) - int(str(chunk[0].get('e') or 0))
        except (TypeError, ValueError):
            continue
        if steps <= 0:
            continue
        try:
            meters = compliance.recompute_distance(chunk)
        except (TypeError, ValueError, KeyError):
            continue
        worst = max(worst, meters / steps)
    return worst


# --------------------------------------------------------------------------- #
# 1. RSA 位置的替身
# --------------------------------------------------------------------------- #

class SandboxCodec:
    """顶替 `RSACrypto`：真实链路是 RSA 公钥加密会话 key，而私钥只有服务端有。

    沙盘两端都由我们控制，所以只要求"可逆且形状一致"：`base64('SBX1' + key)`。
    客户端那行 `rsaUtils.encrypt_bytes(aesKey.encode())` 的调用契约完全不变，
    于是 `buildEncParams` / `getFaceEncParams` 的其余部分（含 `+`→空格、quote）
    仍然被逐字执行、逐字校验。
    """

    def encrypt_bytes(self, data: bytes) -> str:
        return base64.b64encode(CODEC_TAG + bytes(data)).decode('ascii')

    def encrypt(self, message: str) -> str:        # 兼容旧签名
        return self.encrypt_bytes(str(message).encode('utf-8'))

    @staticmethod
    def decode(blob: str) -> str:
        raw = base64.b64decode(_pad_b64(blob).encode('ascii'))
        if not raw.startswith(CODEC_TAG):
            raise ValueError('key 不是沙盘编解码格式（是否忘了 attach？）')
        return raw[len(CODEC_TAG):].decode('utf-8')


def _pad_b64(text: str) -> str:
    text = str(text).strip().replace('\n', '').replace(' ', '+')
    return text + '=' * (-len(text) % 4)


# --------------------------------------------------------------------------- #
# 2. 宽松解码：服务端收到的字段被 httpx 又编了一遍，必须枚举还原
# --------------------------------------------------------------------------- #

def _variants(value: str) -> List[str]:
    """按可能性从高到低给出该字段在客户端"签名时"的样子。

    顺序即优先级：先还原本来的、再考虑被 `unquote` 过 / 被 `+`↔空格 换过。
    """
    value = '' if value is None else str(value)
    out: List[str] = []
    for base in (value, urllib.parse.unquote(value), urllib.parse.unquote(urllib.parse.unquote(value))):
        for cand in (base, base.replace('+', ' '), base.replace(' ', '+'),
                     urllib.parse.unquote(base.replace(' ', '+'))):
            if cand not in out:
                out.append(cand)
    return out


def _restore(value: str, *, encoded: bool) -> str:
    """`encoded=True` 对应 `buildEncParams(..., is_encoded=True)`：客户端先 quote 过一次。"""
    text = str(value)
    if encoded:
        text = urllib.parse.unquote(text)
    return text.replace(' ', '+')


def decode_envelope(key_field: str, param_field: str, v_field: str = '') -> Dict[str, Any]:
    """把 `{'key','param','v'}` 还原成客户端原本要发的明文参数 dict。

    候选枚举而不是死磕一种编码：真实服务端同样是宽松 Base64 解码，两边必须一致。
    """
    last: Optional[Exception] = None
    for key_cand in _variants(key_field):
        try:
            aes_key = SandboxCodec.decode(key_cand)
        except Exception as exc:                  # noqa: BLE001
            last = exc
            continue
        aes = AESCrypto(aes_key, None, True)
        for param_cand in _variants(param_field):
            for cipher in (param_cand, urllib.parse.unquote(param_cand)):
                try:
                    params = json.loads(aes.decrypt(cipher))
                except Exception as exc:          # noqa: BLE001
                    last = exc
                    continue
                if not isinstance(params, dict):
                    continue
                # `/v` 必须是 AES(appSignHash)：服务端用它确认请求来自正版 App
                if v_field:
                    given = params.get('v') or v_field
                    if not _same_b64(str(given), aes.encrypt(APP_SIGN_HASH)):
                        raise ValueError(f"v 校验失败：appSignHash 不匹配（{str(given)[:12]}…）")
                return params
    raise ValueError(f'无法还原加密报文：{type(last).__name__}: {last}')


def _same_b64(a: str, b: str) -> bool:
    def norm(text: str) -> str:
        text = urllib.parse.unquote(str(text)).replace(' ', '+').rstrip('=')
        return text
    return norm(a) == norm(b)


def parse_multipart(body: bytes) -> Tuple[Dict[str, str], Dict[str, bytes]]:
    """极简 multipart 解析：本仓库只发一个 `file` 加若干普通字段。"""
    fields: Dict[str, str] = {}
    files: Dict[str, bytes] = {}
    head = body.split(b'\r\n', 1)[0]
    if not head.startswith(b'--'):
        return fields, files
    boundary = head[2:]
    for chunk in body.split(b'--' + boundary):
        if not chunk.strip() or chunk.strip() == b'--':
            continue
        if b'\r\n\r\n' not in chunk:
            continue
        raw_head, raw_body = chunk.split(b'\r\n\r\n', 1)
        name_hit = re.search(rb'name="([^"]+)"', raw_head)
        if not name_hit:
            continue
        name = name_hit.group(1).decode('utf-8')
        payload = raw_body.rstrip(b'\r\n')
        if b'filename=' in raw_head:
            files[name] = payload
        else:
            fields[name] = payload.decode('utf-8')
    return fields, files


# --------------------------------------------------------------------------- #
# 3. 判定：把 docs/合格规格.md 的六条硬闸门落成代码
# --------------------------------------------------------------------------- #

class Judge:
    """真实服务端的判定顺序（依据 docs/合格规格.md 与真实记录回放）。"""

    def __init__(self, setting: Dict[str, Any]):
        self.setting = dict(setting)

    def required_steps(self, distance_km: float) -> int:
        return submit.required_steps(self.setting, distance_km * 1000.0)

    def required_range(self) -> float:
        return float(self.setting.get('rangeSetting') or self.setting.get('totalRange') or 2.0)

    def window(self) -> Tuple[int, int]:
        return (int(self.setting.get('startQualifiedTime') or 0),
                int(self.setting.get('endQualifiedTime') or 120))

    def judge(self, fields: Dict[str, Any], faces: set) -> Dict[str, Any]:
        """返回 {'status','sportStatus','remark','reachRange','reject','checks'}。

        ``checks`` 只放诊断量（不影响放行），供审计与文档取证使用。
        """

        def reject(reason: str, checks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            return {'status': '0', 'sportStatus': '0', 'remark': reason,
                    'reachRange': '0.000', 'reject': True, 'checks': checks or {}}

        identify = str(fields.get('identify') or '')
        if not UUID_RE.match(identify):
            return reject('识别码格式不合法（identify 必须是 UUID）')

        try:
            sport_range = float(fields.get('sportRange') or 0)
            gps = float(fields.get('gpsDistance') or 0)
            steps = int(fields.get('step') or 0)
            sport_time = int(float(fields.get('sportTime') or 0))
        except (TypeError, ValueError):
            return reject('数值字段无法解析')

        # 1) 三类人脸：type 1/2/3 各一次
        missing = sorted({1, 2, 3} - set(faces))
        if missing:
            return reject(f'人脸识别不完整（缺 type={missing}）')

        # 2) 里程：sportRange 与 gpsDistance 都必须超过 rangeSetting 留余量
        #    真实记录回放：2.000 贴线被判「运动里程不符合」，最小合格值 2.010
        need = self.required_range() * EDGE_RATIO
        if sport_range + 1e-9 < need:
            return reject(f'运动里程不符合（{sport_range:.3f} < {need:.3f}）')
        if gps + 1e-9 < need:
            return reject(f'GPS里程不符合（{gps:.3f} < {need:.3f}）')

        # 3) 步数
        need_steps = self.required_steps(sport_range)
        if steps < need_steps:
            return reject(f'运动步数不符合{steps}（要求 {need_steps}）')

        # 3.5) 两条「上限」量（来源：官方帮助中心原文，见模块常量注释）
        #      只记进 checks 诊断字段，**不拒** —— 5 条真实「合格」记录全部违反它们，
        #      硬拒会 100% 误杀。详见常量区注释里的取证数据。
        checks: Dict[str, Any] = {}
        try:
            raw_points = fields.get('gitudeLatitude')
            pts = json.loads(raw_points) if isinstance(raw_points, str) else (raw_points or [])
        except (TypeError, ValueError):
            pts = []
        if pts and steps > 0:
            chain_m = compliance.recompute_distance(pts)
            worst = max(0.0, _worst_window_stride(pts, WINDOW_MS))
            checks = {
                'chain_m': round(chain_m, 1),
                'chain_per_step': round(chain_m / steps, 3),
                'chain_over_2x_steps': chain_m > MAX_DISTANCE_STEP_RATIO * steps,
                'worst_window_per_step': round(worst, 3),
                'worst_window_over_limit': worst > MAX_METERS_PER_STEP,
            }

        # 4) 时长窗口（单位分钟）
        lo, hi = self.window()
        if not (lo * 60 <= sport_time <= hi * 60):
            return reject(f'运动时间不符合（{sport_time}s 不在 {lo}–{hi} 分钟窗口内）', checks)

        # 5) 配速格式
        if _pace_seconds(fields.get('speed')) is None:
            return reject('配速格式不合法', checks)

        # 6) 环境画像
        env_ok, env_msg = check_environment(fields.get('environment'))
        if not env_ok:
            return reject(env_msg, checks)

        return {'status': '1', 'sportStatus': '1', 'remark': '合格',
                'reachRange': f'{sport_range:.3f}', 'reject': False, 'checks': checks}


def check_environment(env: Any) -> Tuple[bool, str]:
    """复算 `oppo.checksum`，并确认 11 项风险位全 false。"""
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except ValueError:
            return False, 'environment 不是 JSON'
    if not isinstance(env, dict):
        return False, '缺少 environment'
    try:
        oppo = json.loads(env.get('oppo') or '{}')
        safe = json.loads(env.get('safe') or '{}')
    except (TypeError, ValueError):
        return False, 'environment 内层 JSON 解析失败'
    expect = oppo.pop('checksum', None)
    if not expect:
        return False, 'environment.oppo 缺少 checksum'
    blob = json.dumps(oppo, separators=(',', ':'), ensure_ascii=False)
    if hashlib.md5(blob.encode('utf-8')).hexdigest() != str(expect):
        return False, 'environment.oppo checksum 不自洽'
    for name, entry in oppo.items():
        if name in ('auth', 'nonce', 'timestamp'):
            continue
        if str((entry or {}).get('result')) != 'false':
            return False, f'风控画像异常：{name}=true'
    if str(safe.get('root')) != 'false' or str(safe.get('emulator')) != 'false':
        return False, '风控画像异常：root/emulator'
    return True, 'ok'


# --------------------------------------------------------------------------- #
# 4. 沙盘服务端
# --------------------------------------------------------------------------- #

DEFAULT_SETTING: Dict[str, Any] = {
    'totalRange': '2.000', 'rangeSetting': '2.000', 'stepSetting': 1000,
    'startQualifiedTime': 6, 'endQualifiedTime': 21,
    'startQualifiedTimeSetting': 6, 'endQualifiedTimeSetting': 22,
    'limitSpeed': '3.33', 'limitStride': '2.3', 'limitStepFrequency': '300',
    'qualifyTotalCount': 40, 'qualifyTotalRange': 80.0,
    'isStepRecursion': 1, 'userOpenDevelop': False,
}

BANNED_MSG = ('系统检测到您有作弊嫌疑已被拉入禁跑黑名单。'
              '请规范跑步行为，如有疑问请联系学校管理员')

# 真实记录回放：sportRange=2.000 贴线被判「运动里程不符合」，最小合格值 2.010。
# 也就是说服务端在 rangeSetting 之上还留了 0.5% 余量，这里按同一比例复现。
EDGE_RATIO = 1.005

# 官方帮助中心（/upms/sysHelpCenter/sysHelpCenterList，匿名可读）逐条写明的两条上限：
#   「跑步记录提示轨迹距离校验异常？」→ 轨迹点串累加距离 > 步数的两倍即判异常
#   「跑步详情中存在异常提示？」    → 某一时间段步幅 > 2.5 米/步即判异常
# 第二条是**分段的**（「某一时间段」），所以按 60s 滑窗逐段量，取最差的一段。
#
# ⚠️ 这两条**只做诊断，不做硬拒**。取证：把 tests/fixtures/records_qualified.json 里
# 5 条服务器判「合格」的真实记录回放，两条全都被违反（无一例外）：
#     chain/step        3.805 / 9.445 / 10.832 / 3.799 / 7.414    （上限 2.0）
#     60s 窗口最差       7.468 / 86.584 / 396.339 / 5.789 / 60.243（上限 2.5）
# 连「上报里程÷步数」这一口径也挡不住：sportRange=4.010、step=1059 的那条 = 3.787 m/步，
# 它照样是「合格」。所以帮助中心这两句要么只对特定跑型/版本生效，要么文案≠实现。
# 若按硬拒处理，会 100% 误杀真实合格记录 —— 比不判还糟。因此只写进 `checks` 诊断字段。
MAX_METERS_PER_STEP = 2.5
MAX_DISTANCE_STEP_RATIO = 2.0
WINDOW_MS = 60_000



def _path_map() -> Dict[str, str]:
    """path → 路由名（客户端上报的是绝对 URL，这里只比 path）。"""
    out: Dict[str, str] = {}
    for name, token in routes.ROUTE_NAMES.items():
        target = routes.ROUTE_BY_TOKEN.get(token)
        if not target:
            continue
        path = urllib.parse.urlparse(target).path.strip('/')
        if path and path not in out:
            out[path] = name
    return out


class Sandbox:
    """内存服务端：登录 / 禁跑 / 设置下发 / 人脸 / 提交 / 回读 / 进度。"""

    def __init__(self, setting: Optional[Dict[str, Any]] = None, *, token: str = SANDBOX_TOKEN,
                 fence: Any = None, banned: bool = False, verbose: bool = True):
        self.token = token
        self.setting = dict(setting or DEFAULT_SETTING)
        if fence is not None:
            self.setting['geofence'] = fence
        self.judge = Judge(self.setting)
        self.banned = banned
        self.verbose = verbose

        self.paths = _path_map()
        self.records: Dict[str, Dict[str, Any]] = {}
        self.by_day: Dict[str, str] = {}
        self.faces: Dict[str, set] = {}
        self.log: List[Dict[str, Any]] = []
        # 每条提交的**明文参数**（服务端视角），可直接用于对拍/复放/人工核对
        self.payloads: List[Dict[str, Any]] = []
        self.counters = {'oauth': 0, 'setting': 0, 'face': 0, 'submit': 0,
                         'rejected': 0, 'query': 0, 'error': 0}

    # ---- 装配 -----------------------------------------------------
    def attach(self, tsn) -> None:
        """把沙盘挂到 SDK 实例底下（必须在任何业务请求之前调用）。"""
        tsn.rsaUtils = SandboxCodec()
        tsn.httpClient = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        logger.debug('沙盘已挂载：{}', type(tsn).__name__)

    def dump(self, path: str) -> Dict[str, Any]:
        payload = {
            'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'token': self.token,
            'setting': self.setting,
            'counters': self.counters,
            'quota': {'records': len(self.records),
                      'range_km': round(sum(float(r.get('sportRange') or 0)
                                             for r in self.records.values()), 3)},
            'records': list(self.records.values()),
            'payloads': self.payloads,
            'log': self.log,
        }
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        return payload

    # ---- 入口 -----------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = urllib.parse.urlparse(str(request.url)).path.strip('/')
        name = self.paths.get(path)
        if not name:
            return self._json({'code': 404, 'msg': f'沙盘没有这条路由: {path}'}, 404)
        try:
            return self._dispatch(name, request)
        except Exception as exc:                    # noqa: BLE001
            self.counters['error'] += 1
            logger.warning('沙盘处理 {} 异常：{}: {}', name, type(exc).__name__, exc)
            return self._json({'code': 1, 'msg': f'沙盘异常: {type(exc).__name__}: {exc}'})

    # ---- 分发 -----------------------------------------------------
    def _dispatch(self, name: str, request: httpx.Request) -> httpx.Response:
        if name == 'oauthToken':
            return self._oauth(request)
        if name == 'getExerciseSetting':
            self.counters['setting'] += 1
            return self._setting_query(request)
        if name == 'listExerciseRecord':
            self.counters['query'] += 1
            return self._list_records(request)
        if name == 'sumExerciseRecord':
            self.counters['query'] += 1
            return self._sum(request)
        if name == 'statisticsExerciseRecord':
            self.counters['query'] += 1
            return self._statistics(request)
        if name == 'getExerciseRecord':
            self.counters['query'] += 1
            return self._get_record(request)
        if name == 'getLoginUserInfo':
            return self._json({'code': 200, 'data': {'userId': self._user_id(),
                                                     'userName': 'sandbox-user',
                                                     'schoolId': 178, 'schoolName': '沙盘学校'}})
        if name == 'getExerciseStartTime':
            self._envelope(request, name=name)
            return self._json({'code': 200, 'data': int(time.time() * 1000)})
        if name == 'exerciseRunningFace':
            return self._face(request)
        if name == 'addExerciseRecord':
            return self._submit(request)
        self.counters['query'] += 1
        return self._json({'code': 200, 'data': None})

    # ---- 认证 -----------------------------------------------------
    def _oauth(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.counters['oauth'] += 1
        if not params.get('username'):
            return self._json({'code': 1, 'msg': '用户名不能为空'})
        if not params.get('password'):
            return self._json({'code': 1, 'msg': '验证码不能为空'})
        return self._json({
            'access_token': self.token,
            'refresh_token': self.token + '.refresh',
            'token_type': 'bearer',
            'expires_in': 86399,
            'scope': 'server',
            'user_id': self._user_id(),
            'jti': uuid.uuid4().hex,
        })

    @staticmethod
    def _user_id() -> int:
        return 1300000000000000001

    # ---- 参数还原 + 验签 ------------------------------------------
    def _raw_fields(self, request: httpx.Request) -> Tuple[Dict[str, str], Dict[str, bytes]]:
        """拿到客户端发来的原始字段（可能是 query，也可能是表单体）。"""
        if request.method.upper() == 'GET':
            return {k: v for k, v in request.url.params.items()}, {}
        body = request.read()
        ctype = str(request.headers.get('content-type') or '')
        if 'multipart/form-data' in ctype:
            return parse_multipart(body)
        text = body.decode('utf-8', 'replace')
        if text.startswith('{'):
            try:
                data = json.loads(text)
                return {k: str(v) for k, v in data.items()}, {}
            except ValueError:
                pass
        return dict(urllib.parse.parse_qsl(text, keep_blank_values=True)), {}

    def _envelope(self, request: httpx.Request, *, name: str = '',
                  fields: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """还原明文参数并复算 sign。返回 (明文参数, 原始字段)。"""
        raw = dict(fields if fields is not None else self._raw_fields(request)[0])
        self._verify_sign(request, raw)
        if 'param' in raw:
            params = decode_envelope(raw.get('key', ''), raw.get('param', ''), raw.get('v', ''))
        else:
            params = dict(raw)
        if name:
            self.log.append({'at': datetime.datetime.now().isoformat(timespec='seconds'),
                             'route': name, 'keys': sorted(params.keys())})
        return params, raw

    def _verify_sign(self, request: httpx.Request, raw: Dict[str, str]) -> None:
        """复算 `_sign`：与 sdk/public.py:191 `_sign` 逐字同构。

        客户端签的是它**自己组出来的那份 dict**（`getSign(data.copy(), ts)`），
        服务端收到的是被 httpx 再编码一遍的字符串，所以同样枚举变体逐个比对。
        """
        timestamp = request.headers.get('timestamp')
        given = request.headers.get('sign')
        if not timestamp or not given:
            raise ValueError('缺少 sign/timestamp 头')
        aes = AESCrypto(md5(self.token)[0:16], md5(self.token)[16:32])
        app_id = md5(aes.encrypt(APP_ID + self.token + timestamp))
        app_secret = md5(aes.encrypt(self.token + timestamp))
        base = {k: v for k, v in raw.items() if k not in ('sign', 'timestamp')}

        # 逐键枚举变体（最坏 6^k，实际 k ≤ 2，可忽略）
        def expands(items: List[Tuple[str, str]]) -> List[Dict[str, str]]:
            out: List[Dict[str, str]] = [{}]
            for key, value in items:
                out = [dict(prev, **{key: cand}) for prev in out for cand in _variants(value)]
            return out

        for candidate in expands(list(base.items())):
            merged = dict(candidate, appId=app_id, appSecret=app_secret)
            expect = md5(aes.encrypt(urllib.parse.unquote(submit.build_auth_info(merged))))
            if expect == str(given):
                return
        raise ValueError(f'sign 校验失败（given={str(given)[:10]}…，字段={sorted(base)}）')

    @staticmethod
    def _seal(aes_key: str, payload: Any) -> str:
        return AESCrypto(aes_key, None, True).encrypt(
            json.dumps(payload, separators=(',', ':'), ensure_ascii=False))

    def _aes_key_of(self, request: httpx.Request, fields: Optional[Dict[str, str]] = None) -> str:
        raw = fields if fields is not None else self._raw_fields(request)[0]
        return SandboxCodec.decode(_restore(raw.get('key', ''), encoded=False))

    # ---- 业务 -----------------------------------------------------
    def _setting_query(self, request: httpx.Request) -> httpx.Response:
        fields, _ = self._raw_fields(request)
        self._envelope(request, name='getExerciseSetting', fields=fields)
        if self.banned:
            return self._json({'code': 999, 'msg': BANNED_MSG})
        aes_key = SandboxCodec.decode(_restore(fields.get('key', ''), encoded=False))
        return self._json({'code': 200, 'data': self._seal(aes_key, self.setting)})

    def _list_records(self, request: httpx.Request) -> httpx.Response:
        params, _ = self._envelope(request, name='listExerciseRecord')
        want = str(params.get('status', '1'))
        rows = [r for r in self._ordered() if str(r.get('status')) == want]
        try:
            page = int(params.get('datePageIndex') or 1)
        except (TypeError, ValueError):
            page = 1
        chunk = rows if page == 1 else []
        # 真实契约：data 是**扁平数组**。`app/backfill.py:occupied_dates` 直接
        # `for row in rows` 再 `row.get('startTime')`——返回分页信封会在这里炸。
        return self._json({'code': 200, 'data': chunk})

    def _sum(self, request: httpx.Request) -> httpx.Response:
        self._envelope(request, name='sumExerciseRecord')
        ok = [r for r in self.records.values() if str(r.get('status')) == '1']
        total_km = round(sum(float(r.get('sportRange') or 0) for r in ok), 3)
        return self._json({'code': 200, 'data': {
            'sunRun': {'succeed': len(ok), 'runSportRange': total_km,
                       'validitySunRun': 20, 'totalRange': 80.0, 'sportTime': 0},
            'morningRun': {'succeed': 0, 'runSportRange': 0},
            'freedomRun': {'succeed': 0, 'runSportRange': 0},
        }})

    def _statistics(self, request: httpx.Request) -> httpx.Response:
        self._envelope(request, name='statisticsExerciseRecord')
        return self._json({'code': 200, 'data': {
            'totalCount': len(self.records),
            'qualifyCount': sum(1 for r in self.records.values() if r.get('status') == '1'),
            'totalRange': round(sum(float(r.get('sportRange') or 0)
                                     for r in self.records.values()), 3),
        }})

    def _get_record(self, request: httpx.Request) -> httpx.Response:
        params, _ = self._envelope(request, name='getExerciseRecord')
        rid = str(params.get('exerciseRecordId') or '')
        rec = self.records.get(rid)
        if rec is None:
            return self._json({'code': 1, 'msg': '记录不存在'})
        return self._json({'code': 200, 'data': rec})

    def _face(self, request: httpx.Request) -> httpx.Response:
        fields, files = parse_multipart(request.read())
        self._verify_sign(request, {k: v for k, v in fields.items()
                                    if k in ('key', 'param', 'v', 'nonce')})
        params = decode_envelope(fields.get('key', ''), fields.get('param', ''))
        payload = files.get('file') or b''
        if payload[:3] != b'\xff\xd8\xff':
            return self._json({'code': 1, 'msg': '人脸图片格式不合法（只接受 JPEG）'})
        identify = str(params.get('identify') or '')
        try:
            ftype = int(params.get('type') or 0)
        except (TypeError, ValueError):
            ftype = 0
        if ftype not in (1, 2, 3):
            return self._json({'code': 1, 'msg': f'人脸 type 非法: {ftype}'})
        if not UUID_RE.match(identify):
            return self._json({'code': 1, 'msg': 'identify 必须是 UUID'})
        self.faces.setdefault(identify, set()).add(ftype)
        self.counters['face'] += 1
        return self._json({'code': 200, 'data': True})

    def _submit(self, request: httpx.Request) -> httpx.Response:
        fields, _ = self._raw_fields(request)
        params, _ = self._envelope(request, name='addExerciseRecord', fields=fields)
        self.counters['submit'] += 1
        identify = str(params.get('identify') or '')
        day = self._day_of(int(float(params.get('startTime') or 0)))

        # 每天一条：真实服务端直接回同一 id + 「今日已提交」
        if day and day in self.by_day:
            # 真实服务端：同一天重复提交**不报错**，直接回原来那条的 id
            rid = self.by_day[day]
            return self._json({'code': 200, 'msg': '今日已提交',
                               'exerciseRecordId': rid, 'data': self.records[rid]})

        merged = dict(params)
        merged['step'] = self._sum_steps(params.get('stepNumbers'))
        verdict = self.judge.judge(merged, self.faces.get(identify, set()))
        rid = f'{int(time.time() * 1000)}{len(self.records):04d}'
        rec = {
            'id': rid,
            'identify': identify,
            'startTime': str(params.get('startTime')),
            'endTime': str(params.get('endTime')),
            'sportTime': str(params.get('sportTime')),
            'sportRange': str(params.get('sportRange')),
            'reachRange': verdict['reachRange'],
            'gpsDistance': str(params.get('gpsDistance')),
            'speed': str(params.get('speed')),
            'avgSpeed': str(params.get('avgSpeed')),
            'step': merged['step'],
            'stepNumbers': params.get('stepNumbers'),
            'sportType': str(params.get('sportType') or '1'),
            'sportTypeName': '阳光跑',
            'isFaceStatus': int(float(params.get('isFaceStatus') or 0)),
            'uploadType': int(float(params.get('uploadType') or 0)),
            'status': verdict['status'],
            'sportStatus': verdict['sportStatus'],
            'remark': verdict['remark'],
            'basUserId': self._user_id(),
            'appVersion': params.get('appVersion'),
            'brand': 'Apple', 'model': 'iPhone 17 Pro', 'system': 'iOS',
            'version': '27.0.1', 'week': '',
            'd': params.get('d'), 'f': params.get('f'),
            'm': params.get('m'), 'h': params.get('h'),
            'isValid': params.get('isValid'),
            'pointList': params.get('pointList'), 'okPointList': params.get('okPointList'),
        }
        self.records[rid] = rec
        self.payloads.append({
            'id': rid, 'day': day, 'identify': identify,
            'faces': sorted(self.faces.get(identify, set())),
            'status': verdict['status'], 'remark': verdict['remark'],
            'checks': verdict.get('checks') or {},
            'fields': {k: merged[k] for k in sorted(merged)},
        })
        if verdict['reject']:
            self.counters['rejected'] += 1
            logger.info('沙盘驳回：{}', verdict['remark'])
        elif day:
            self.by_day[day] = rid
        return self._json({'code': 200, 'data': rec, 'exerciseRecordId': rid,
                           'msg': verdict['remark']})

    # ---- 工具 -----------------------------------------------------
    @staticmethod
    def _sum_steps(step_numbers: Any) -> int:
        try:
            data = json.loads(step_numbers) if isinstance(step_numbers, str) else step_numbers
            return int(sum(int(x) for x in (data or [])))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _day_of(ts_ms: int) -> str:
        if not ts_ms:
            return ''
        return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d')

    def _ordered(self) -> List[Dict[str, Any]]:
        return sorted(self.records.values(), key=lambda r: int(r.get('startTime') or 0))

    @staticmethod
    def _json(payload: Dict[str, Any], status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=payload)


def build_client(school_id: int = 178, school_code: str = 'gzccc',
                 base_url: str = 'https://h.tsnkj.com'):
    """按真实装配顺序造一个**挂在沙盘上**的公有版 SDK 实例（不发真实请求）。"""
    from app.device import deviceModel
    from sdk.public import TiShiNengSdkPublic
    tsn = TiShiNengSdkPublic(1, school_id, school_code, '', str(uuid.uuid4()),
                             deviceModel.brand, deviceModel.model, deviceModel.osver,
                             '', deviceModel.a_list)
    tsn.setCloudUrl(base_url)
    return tsn


# --------------------------------------------------------------------------- #
# 5. 合成人脸图：仓库里 face_images/ 是空的，--no-face 会被真实服务端判黑名单
# --------------------------------------------------------------------------- #

def make_face_jpeg(path: str, seed: int = 7) -> str:
    """生成一张确定性的 JPEG 人脸占位图（沙盘只校验 magic 与来源）。"""
    from PIL import Image, ImageDraw
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    rng = random.Random(seed)
    img = Image.new('RGB', (480, 640), (28, 32, 38))
    draw = ImageDraw.Draw(img)
    draw.ellipse((120, 120, 360, 400), fill=(214, 186, 160))
    draw.ellipse((170, 210, 215, 250), fill=(40, 40, 44))
    draw.ellipse((265, 210, 310, 250), fill=(40, 40, 44))
    draw.arc((190, 300, 290, 360), start=0, end=180, fill=(120, 70, 70), width=6)
    for _ in range(400):
        draw.point((rng.randrange(480), rng.randrange(640)),
                   fill=(rng.randrange(256),) * 3)
    img.save(path, 'JPEG', quality=82)
    return path
