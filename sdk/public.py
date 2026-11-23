import base64
import hashlib
import io
import json
import time
import urllib.parse
from typing import Any, Dict

from loguru import logger

from app.device import deviceModel
from tsn import env
from tsn import submit
from tsn import protocol
from tsn import routes
from tsn.crypto import AESCrypto
from tsn.crypto import RSACrypto
from sdk.errors import TiShiNengError
from sdk.base import TiShiNengSdkBase

# 报文分类与重试策略统一在 tsn_protocol / tsn_http，本模块只负责「公有版签名」。
# 注意：getAccessToken 的密码密文必须走 query string（实测表单体返回 428 /
# "验证码不能为空"），这是服务端契约，不要改成 data=。


class TiShiNengSdkPublic:

    appId = 'c9292ee89d2f49492f983f5931af0d09'
    appSecret = 'e8167ef026cbc5e456ab837d9d6d9254'
    appSign = '7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3'

    def __init__(self, uid, schoolId, schoolCode, openId, deviceId, brandName, deviceNum, osVersion, token, a_list):
        self.uid = uid
        self.schoolId = schoolId
        self.schoolCode = schoolCode
        self.deviceId = self.getMd5(deviceId)
        self.brandName = brandName
        self.deviceNum = deviceNum
        self.AndroidOsVersion = osVersion
        self.openId = openId
        self.cloudUrl = 'http://a.sxstczx.com'
        self.platform = '1'
        self.tiShiNengBaseClient = TiShiNengSdkBase(uid, schoolId, deviceId, brandName, deviceNum, token)
        self.versionName = self.tiShiNengBaseClient.versionName
        self.core = self.tiShiNengBaseClient.core
        self.token = token
        self._rebuild_aes(token)
        self.passwordAesUtils = AESCrypto('thanks,pig4cloud', 'thanks,pig4cloud')
        self._environmentOverride = None
        self._envSignals = None
        self.appSignHash = submit.KNOWN_APP_SIGN_HASH
        self.rsaUtils = RSACrypto('MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAgzQ7BYqBZ5LTjoOb9aHO8fI0hbww9YRW2lnqIdDyIjBwmhthTR+EmiKNm4yFKg6Vz2GW5ix3IdUQdaAq3ZZ7se/dCOTpu3dk15ZgkO6ZImUE7gqzSXXJ0NaACudk4yJwk3Q69kB4m3xIKxiOlG2HtbEed01LrUmLag9VOP96BuSao2sP4Als5hA/8C6KqdihTOcZF1RT+lqrT3Qvja7q+qI5QZw9d7NrFFycQs8jk8O49f9mkvLZRZCCWEbwzuCPTlMy/ZNAsMeU/gNSRKUnquOiPboc2KUhsvY4cK0GeuS9vuIrMGE01L/BCc+rUrautq3n3WiIVJwnwWiJtgk33QIDAQAB')
        self.Cookie = 'host=' + self.schoolCode
        self.headers = {
            'model': self.deviceNum,
            'uniqueCode': self.deviceId,
            'school': self.schoolCode,
            'Cookie': self.Cookie,
            'accept-encoding': 'gzip',
            'user-agent': 'okhttp/4.9.0',
        }
        self.v2BaseDict = {
            'appType': 'Android',
            'versionCode': int(self.tiShiNengBaseClient.version),
            'versionName': self.tiShiNengBaseClient.versionName,
            'signatureMD5': '7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3',
            'brand': self.brandName,
            'model': self.deviceNum,
            'system': getattr(deviceModel, 'system', 'Android'),
            'version': self.AndroidOsVersion,
            'uniqueCode': self.deviceId,
        }
        self.a_list = a_list.split(',')

    # ---- 兼容层 -----------------------------------------------------
    @property
    def httpClient(self):
        return self.core.client

    @httpClient.setter
    def httpClient(self, value):
        self.core.client = value

    def isPublic(self):
        return True

    @staticmethod
    def kVtoStr(key, value, is_encoded):
        if is_encoded:
            try:
                return f'{key}={urllib.parse.quote(value)}'
            except Exception:
                return f'{key}={value}'
        return f'{key}={value}'

    @staticmethod
    def getMd5(text):
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    @staticmethod
    def jsonOrRaise(resp):
        """保留旧入口；新代码请直接用 protocol.classify_response。"""
        try:
            return resp.json()
        except ValueError:
            result = protocol.classify_response(resp)
            raise result.to_error()

    @staticmethod
    def buildEncParams(randomAesUtils: AESCrypto, aesRandomKey: str, params: dict, rsaUtils, appSignHash: str, is_encoded=False):
        """加密参数组装（公有版 /v 校验 + AES(JSON) + RSA(key)）。

        is_encoded 是服务端契约的一部分，不要统一：GET 用原值交给 httpx 编码，
        POST 加密体历史上先 quote 一次（服务端用宽松 Base64 解码容忍这种双重编码）。
        """
        v = randomAesUtils.encrypt(appSignHash)
        params['v'] = v
        sorted_params = {key: params[key] for key in sorted(params.keys())}
        logger.debug('enc params keys: {}', list(sorted_params.keys()))
        encData = randomAesUtils.encrypt(json.dumps(sorted_params, separators=(',', ':')))
        rsaEncryptedAesKey = rsaUtils.encrypt_bytes(aesRandomKey.encode())
        if not is_encoded:
            return {'key': rsaEncryptedAesKey.replace('+', ' '), 'param': encData.replace('+', ' ')}
        return {'key': urllib.parse.quote(rsaEncryptedAesKey), 'param': urllib.parse.quote(encData)}

    def getEncParams(self, randomAesUtils: AESCrypto, aesRandomKey: str, params: dict, is_encoded=False):
        return self.buildEncParams(randomAesUtils, aesRandomKey, params, self.rsaUtils, self.appSignHash, is_encoded)

    def getFaceEncParams(self, params: dict, timestamp: str):
        aesRandomKey = self.getMd5(self.token + timestamp)
        aesKey = aesRandomKey[0:16]
        randomAesUtils = AESCrypto(aesKey, None, True)
        v = randomAesUtils.encrypt(self.appSignHash)
        params['v'] = v
        sorted_params = {key: params[key] for key in sorted(params.keys())}
        encData = randomAesUtils.encrypt(json.dumps(sorted_params, separators=(',', ':')))
        key = self.rsaUtils.encrypt_bytes(aesKey.encode())
        # 与 buildEncParams(is_encoded=True) 一致：表单字段必须 percent-encode，
        # 否则未编码的 base64（含 + / =）会被宝塔 WAF 以 403 拦下。
        return {'key': urllib.parse.quote(key, safe=''), 'param': urllib.parse.quote(encData, safe='')}

    # ---- 状态 -------------------------------------------------------
    def _rebuild_aes(self, token: str) -> None:
        md5Token = self.getMd5(token)
        self.key = md5Token[0:16]
        self.iv = md5Token[16:32]
        self.aesUtils = AESCrypto(self.key, self.iv)

    def setCloudUrl(self, cloudUrl):
        """设置网关地址。

        公有版学校普遍只提供 http，因此这里不强制
        https，只做规范化并把协议记进日志，方便排查"为什么是明文"。
        """
        if not cloudUrl:
            return
        normalized = routes.normalize_base_url(cloudUrl)
        self.cloudUrl = normalized.rstrip('/')
        self.core.set_base_url(self.cloudUrl)
        if self.cloudUrl.startswith('http://'):
            logger.warning('网关使用明文 http：{}（该学校未提供 https，凭据在链路上可见）', self.cloudUrl)
        # 网关默认把 Host 钉在 m.boxkj.com；一旦切到学校自建 lan_url，
        # Host 必须跟着换成目标 netloc，否则学校 nginx 按 Host 分流会直接 404。
        parsed = urllib.parse.urlparse(normalized)
        if parsed.netloc:
            self.headers['Host'] = parsed.netloc
            if getattr(self.core, 'headers', None) is not None:
                self.core.headers['Host'] = parsed.netloc

    def setToken(self, token):
        self.token = token
        self._rebuild_aes(token)
        self.core.set_token(token)

    def setEnvironment(self, environment: dict | None):
        self._environmentOverride = dict(environment) if environment else None

    def setCertDer(self, cert_der: bytes | None):
        self.appSignHash = submit.app_sign_hash(bytes(cert_der)) if cert_der else submit.KNOWN_APP_SIGN_HASH

    def setEnvSignals(self, signals):
        self._envSignals = signals

    # ---- 签名 -------------------------------------------------------
    def getAesAppid(self, timeStamp):
        return self.getMd5(self.aesUtils.encrypt(self.appId + self.token + timeStamp))

    def getAesAppSecret(self, timeStamp):
        return self.getMd5(self.aesUtils.encrypt(self.token + timeStamp))

    def _sign(self, params: dict, timeStr: str) -> str:
        params.update({'appId': self.getAesAppid(timeStr), 'appSecret': self.getAesAppSecret(timeStr)})
        auth_info = submit.build_auth_info(params)
        return self.getMd5(self.aesUtils.encrypt(urllib.parse.unquote(auth_info)))

    def getSign(self, params: dict, timeStr):
        return self._sign(params, timeStr)

    def getFaceSign(self, params: dict, timeStr):
        return self._sign(params, timeStr)

    # ---- 认证 -------------------------------------------------------
    def _oauth_headers(self) -> dict:
        headers = self.headers.copy()
        headers['Authorization'] = 'Basic ' + base64.b64encode(f'{self.schoolCode}:pig'.encode('utf-8')).decode('utf-8')
        return headers

    async def _oauth_request(self, params: dict, context: str) -> dict:
        result = await self.core.send(
            'POST',
            routes.route('oauthToken'),
            params=params,          # 必须是 query string
            headers=self._oauth_headers(),
            context=context,
        )
        result.raise_for_error()
        return result.payload

    async def getAccessToken(self, username, password):
        encPassword = self.passwordAesUtils.encrypt(password, zero_padding=True)
        params = {'username': username, 'password': encPassword, 'grant_type': 'password', 'type': 'app', 'appType': 'stuApp'}
        return await self._oauth_request(params, 'getAccessToken')

    async def freshToken(self, freshToken):
        params = {'refresh_token': freshToken, 'scope': 'server', 'grant_type': 'refresh_token', 'type': 'app', 'appType': 'stuApp'}
        return await self._oauth_request(params, 'freshToken')

    # ---- 通用请求 ---------------------------------------------------
    def _request_headers(self, sign: str, timestamp: str) -> dict:
        headers = self.headers.copy()
        headers.update({
            'sign': sign,
            'timestamp': timestamp,
            'Authorization': 'Bearer ' + self.token,
        })
        return headers

    async def httpPost(self, url, data, timestamp, *, return_meta=False):
        """发送加密表单，返回 data；return_meta=True 时返回 (data, exerciseRecordId)。"""
        sign = self.getSign(data.copy(), timestamp)
        headers = self._request_headers(sign, timestamp)
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        result = await self.core.send('POST', url, data=data, headers=headers, context=url)
        result.raise_for_error()
        payload = result.payload or {}
        if return_meta:
            return payload.get('data'), payload.get('exerciseRecordId')
        return payload.get('data')

    async def httpGet(self, url, params, timestamp=None):
        if timestamp is None:
            timestamp = str(int(time.time() * 1000))
        sign = self.getSign(params.copy(), timestamp)
        result = await self.core.send(
            'GET',
            url,
            params=params,
            headers=self._request_headers(sign, timestamp),
            context=url,
        )
        result.raise_for_error()
        return (result.payload or {}).get('data')

    # ---- 业务接口（只读） -------------------------------------------
    async def getAppid(self):
        return await self.httpGet(routes.route('getAppid'), {})

    async def listMenu(self, location=1):
        return await self.httpGet(routes.route('listMenu'), {'location': str(location)})

    async def messageArticleListByType(self, mtype=4):
        return await self.httpGet(routes.route('messageArticleListByType'), {'type': str(mtype)})

    async def getLatestUnreadNotice(self):
        return await self.httpGet(routes.route('latestUnreadNotice'), {})

    async def isDefalutPass(self):
        return await self.httpGet(routes.route('isDefaultPass'), {})

    async def sumExerciseRecord(self):
        return await self.httpGet(routes.route('sumExerciseRecord'), {})

    async def getFeedbackBalance(self):
        return await self.httpGet(routes.route('getFeedbackBalance'), {})

    async def statisticsExerciseRecord(self):
        return await self.httpGet(routes.route('statisticsExerciseRecord'), {})

    async def getExerciseStartTime(self, identify):
        return await self.httpGet(routes.route('getExerciseStartTime'), {'identify': identify})

    async def getExerciseRecord(self, exerciseRecordId):
        return await self.httpGet(routes.route('getExerciseRecord'), {'exerciseRecordId': exerciseRecordId})

    async def getExerciseExplanation(self):
        return await self.httpGet(routes.route('getExerciseExplanation'), {})

    async def getLoginUserInfo(self):
        return await self.httpGet(routes.route('getLoginUserInfo'), {})

    async def listExerciseRecord(self, runStatus=1, date='', datePageIndex=1):
        return await self.httpGet(routes.route('listExerciseRecord'), {'status': runStatus, 'date': date, 'datePageIndex': datePageIndex})

    async def getAppSocketServer(self):
        return await self.httpGet(routes.route('getAppSocketServer'), {'basUserId': self.uid})

    async def listBasUserImageFace(self):
        return await self.httpGet(routes.route('listBasUserImageFace'), {'basUserId': self.uid})

    # ---- 业务接口（加密） -------------------------------------------
    def _new_random_aes(self, timestamp: str):
        aesKey = self.getMd5(self.token + timestamp)[0:16]
        return AESCrypto(aesKey, None, True), aesKey

    async def getExerciseSetting(self, runType, longitude, latitude):
        params = self.v2BaseDict.copy()
        params.update({'runType': runType, 'longitude': longitude, 'latitude': latitude})
        timestamp = str(int(time.time() * 1000))
        randomAesUtils, aesKey = self._new_random_aes(timestamp)
        encParams = self.getEncParams(randomAesUtils, aesKey, params)
        data = await self.httpGet(routes.route('getExerciseSetting'), encParams, timestamp)
        return json.loads(randomAesUtils.decrypt(data))

    async def addExerciseRecord(self, sportType, startTime, endTime, sportTime, sportRange, speed, avgSpeed, gitudeLatitude, stepNumbers, isSequencePoint, pointList, okPointList, isFaceStatus, uploadType, identify, geofence, limitSpeed, limitStride, limitStepFrequency, gpsDistance, *, d=None, f=None, m=None, h=None, isValid=None, remarkId=None, setting=None, trackPoints=None, stepCount=None, elapsedSeconds=None, distanceMeters=None, userOpenDevelop=False, offline=0, environment=None):
        limitSpeed = f'{float(limitSpeed):.1f}'
        limitStride = f'{float(limitStride):.1f}'
        point_dicts = self._parse_track_points(trackPoints if trackPoints is not None else gitudeLatitude)
        extras: Dict[str, Any]
        if setting is not None:
            extras = submit.build_submit_extras(
                setting,
                points=point_dicts,
                elapsed_s=float(elapsedSeconds if elapsedSeconds is not None else sportTime),
                distance_m=float(distanceMeters if distanceMeters is not None else float(sportRange) * 1000.0),
                steps=int(stepCount or 0),
                user_open_develop=userOpenDevelop,
                offline=offline,
                include_is_valid=isValid is None,
            )
        else:
            extras = submit.aggregate_risk_flags(point_dicts, user_open_develop=userOpenDevelop, offline=offline)
        for key, value in (('d', d), ('f', f), ('m', m), ('h', h)):
            if value is not None:
                extras[key] = int(value)
        if isValid is not None:
            extras['isValid'] = int(isValid)
        if remarkId is not None:
            extras['remarkId'] = remarkId

        data = {
            'sportType': sportType, 'startTime': startTime, 'endTime': endTime,
            'sportTime': sportTime, 'sportRange': sportRange, 'speed': speed,
            'avgSpeed': avgSpeed, 'appVersion': self.versionName,
            'stepNumbers': stepNumbers, 'isSequencePoint': isSequencePoint,
            'gitudeLatitude': gitudeLatitude, 'pointList': pointList,
            'okPointList': okPointList, 'isFaceStatus': str(isFaceStatus),
            'uploadType': int(uploadType), 'identify': identify, 'geofence': geofence,
            'limitSpeed': limitSpeed, 'limitStride': limitStride,
            'limitStepFrequency': str(limitStepFrequency), 'gpsDistance': gpsDistance,
            'd': extras.get('d', 0), 'f': extras.get('f', 0), 'm': extras.get('m', 0), 'h': extras.get('h', 0),
            'environment': environment if environment is not None else self.getEnvData(),
        }
        if 'isValid' in extras:
            data['isValid'] = extras['isValid']
        if 'remarkId' in extras:
            data['remarkId'] = extras['remarkId']

        params = self.v2BaseDict.copy()
        params.update(data)
        timestamp = str(int(time.time() * 1000))
        randomAesUtils, aesKey = self._new_random_aes(timestamp)
        encParams = self.getEncParams(randomAesUtils, aesKey, params, True)
        encResp, exerciseRecordId = await self.httpPost(routes.route('addExerciseRecord'), encParams, timestamp, return_meta=True)
        logger.info(
            'addExerciseRecord -> id={} flags={{d:{},f:{},m:{},h:{}}} isValid={} remarkId={}',
            exerciseRecordId, data['d'], data['f'], data['m'], data['h'],
            data.get('isValid'), data.get('remarkId'),
        )
        return (encResp, exerciseRecordId)

    @staticmethod
    def _parse_track_points(raw):
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return [p for p in raw if isinstance(p, dict)]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return []
            if isinstance(parsed, list):
                return [p for p in parsed if isinstance(p, dict)]
        return []

    def getEnvData(self):
        if self._environmentOverride is not None:
            data = dict(self._environmentOverride)
            data.setdefault('deviceId', self.deviceId)
            return data
        return env.build_environment_payload(device_id=self.deviceId, signals=self._envSignals, abis=self.a_list)

    async def exerciseRunningFace(self, faceBytes: bytes, coordinates, identify, runType=1, faceType=1):
        timestamp = str(int(time.time() * 1000))
        data = {
            'identify': identify, 'type': str(faceType), 'runType': str(runType),
            'coordinates': coordinates, 'timeStamp': timestamp, 'exception': '0',
            'environment': self.getEnvData(),
        }
        params = self.v2BaseDict.copy()
        params.update(data)
        encParams = self.getFaceEncParams(params, timestamp)
        key, param = encParams['key'], encParams['param']
        headers = self._request_headers(self.getFaceSign({'key': key, 'param': param}, timestamp), timestamp)
        # key/param 必须以普通表单字段（不带每段 Content-Type）上传，
        # 否则宝塔 WAF 会以 "from-data 请求异常" 拦截较长的加密参数。
        result = await self.core.send(
            'POST',
            routes.route('exerciseRunningFace'),
            data={'key': key, 'param': param},
            files={'file': ('file.jpg', io.BytesIO(faceBytes), 'image/jpeg')},
            headers=headers,
            timeout=30,
            context='exerciseRunningFace',
        )
        result.raise_for_error()
        return (result.payload or {}).get('data')

    async def aclose(self):
        await self.tiShiNengBaseClient.aclose()
