import asyncio
import datetime
import enum
import io
import json
import random
import re
import os
import time
from zoneinfo import ZoneInfo

import httpx
from PIL import Image
from loguru import logger
from sqlalchemy import select, func
from sdk.errors import ErrorCode, TiShiNengError
from sdk.runpath import genTiShiNengRunPathRepeat
from app.database import get_db
from app.device import deviceModel
from app.models import TsnAccount_Model, RunPath
from tsn.dao import getTsnAccountByid
from sdk.client import getTsnClientById
from tsn import env
from tsn import face_plan
from tsn import face_store
from tsn import compliance
from tsn import submit

# 服务端时间戳按学校所在时区渲染；容器里 TZ=UTC 时不能让本地时间整体偏 8 小时。
# Windows 默认不带 IANA 时区库（缺 tzdata），退化为固定 +08:00。
def _load_timezone():
    name = os.getenv('TSN_TZ', 'Asia/Shanghai')
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning('未找到时区 {}（缺少 tzdata？），退化为固定 UTC+8', name)
        return datetime.timezone(datetime.timedelta(hours=8))


LOCAL_TZ = _load_timezone()

class TsnRunType(enum.Enum):
    morningRun = 1
    sumRun = 2
    freedom = 3

def seconds_to_time_format(total_seconds):
    hours = total_seconds // 3600
    minutes = total_seconds % 3600 // 60
    seconds = total_seconds % 60
    time_formatted = '{:02d}:{:02d}:{:02d}'.format(hours, minutes, seconds)
    return time_formatted

class TsnRunServer:

    def __init__(self, accountId: int, runKiloMeter: float, logRunType: TsnRunType):
        self.accountId = accountId
        self.runKiloMeter = runKiloMeter
        self.logRunType = logRunType
        self.accountModel: TsnAccount_Model | None = None
        self.tsnClient = None
        self.identify = None
        self.start_timestamp = None
        self.exerciseSetting = None
        self.geofence = None
        self.endStride = None
        self.limitSpeed = None
        self.endLimitStepFrequency = None
        self.pointList = None
        self.isFaceStatus = 1
        self.faceUploaded = False

    # 老代码里统一写 self.client，实际只在 tsnClient 上赋值；
    # 用属性兜住两种写法，避免又出现 AttributeError。
    @property
    def client(self):
        return self.tsnClient

    @client.setter
    def client(self, value):
        self.tsnClient = value

    @classmethod
    def publicRunTypeConvert(cls, runType: TsnRunType):
        if runType == TsnRunType.morningRun:
            return 2
        elif runType == TsnRunType.sumRun:
            return 1
        elif runType == TsnRunType.freedom:
            return 0
        else:
            raise ValueError('runType error')

    @staticmethod
    def _randomize_coordinates(longitude, latitude):
        random_longitude = random.uniform(longitude - 0.0005, longitude + 0.0005)
        random_latitude = random.uniform(latitude - 0.0005, latitude + 0.0005)
        return (random_longitude, random_latitude)

    @staticmethod
    def add_random_pixels_to_image(image_bytes: bytes, num_pixels: int=None) -> bytes:
        if not image_bytes:
            return image_bytes
        try:
            if num_pixels is None:
                num_pixels = random.randint(5, 15)
            image = Image.open(io.BytesIO(image_bytes))
            if image.mode != 'RGB':
                image = image.convert('RGB')
            width, height = image.size
            pixels = image.load()
            for _ in range(num_pixels):
                x = random.randint(10, width - 10)
                y = random.randint(10, height - 10)
                current_pixel = pixels[x, y]
                new_pixel = list(current_pixel)
                for i in range(random.randint(1, 3)):
                    channel = random.randint(0, 2)
                    delta = random.randint(-5, 5)
                    new_pixel[channel] = max(0, min(255, new_pixel[channel] + delta))
                pixels[x, y] = tuple(new_pixel)
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=95)
            modified_bytes = output.getvalue()
            logger.debug(f'图片修改完成: 添加了 {num_pixels} 个随机像素点，原始大小: {len(image_bytes)} bytes, 修改后: {len(modified_bytes)} bytes')
            return modified_bytes
        except Exception as e:
            logger.error(f'修改图片失败: {e}, 返回原始图片')
            return image_bytes

    async def getFaceImage(self):
        face_dir = face_store.build_face_dir('face_images', self.accountModel.school_id, self.accountModel.user_id)
        existing_images = face_store.list_images(face_dir)
        if existing_images:
            selected_image = random.choice(existing_images)
            logger.info('使用本地人脸图片: {}', selected_image)
            return selected_image.read_bytes()

        logger.info('本地没有人脸图片，从服务器获取...')
        face_list_resp = await self.client.listBasUserImageFace()
        if not face_list_resp:
            raise TiShiNengError('获取人脸列表失败，请检查网络连接或账号状态', ErrorCode.FACE_LIST_FAILED)
        if len(face_list_resp) == 0:
            raise TiShiNengError('该账号没有人脸图片记录，请先在APP中上传人脸照片', ErrorCode.FACE_NONE)

        download_count = 0
        async with httpx.AsyncClient() as client:
            for idx, face_item in enumerate(face_list_resp):
                image_url = face_item.get('imageRouteUrl')
                if not image_url:
                    continue
                try:
                    safe_url = face_store.validate_image_url(image_url)
                    logger.info('下载人脸图片 {}/{}', idx + 1, len(face_list_resp))
                    resp = await client.get(safe_url, timeout=30.0)
                    if resp.status_code != 200:
                        logger.warning('人脸图片下载失败，状态码 {}', resp.status_code)
                        continue
                    path = face_store.store_image(face_dir, face_item.get('id', f'face_{idx}'), resp.content)
                    logger.info('人脸图片已保存: {}', path)
                    download_count += 1
                except TiShiNengError as e:
                    logger.warning('跳过该人脸图片: {}', e.message)
                except Exception as e:
                    logger.error('下载人脸图片失败: {}', type(e).__name__)
                continue

        existing_images = face_store.list_images(face_dir)
        if not existing_images:
            raise TiShiNengError(f'人脸图片下载失败，尝试下载 {len(face_list_resp)} 张但都失败了，请检查网络连接', ErrorCode.FACE_DOWNLOAD_FAILED)
        selected_image = random.choice(existing_images)
        logger.info('使用刚下载的人脸图片: {} (共下载 {} 张)', selected_image, download_count)
        return selected_image.read_bytes()

    async def uploadFace(self, coordinates, sleep=0, faceType=1):
        if not self.isPublic:
            return
        if sleep > 0:
            await asyncio.sleep(sleep)
        original_image = await self.getFaceImage()
        modified_image = self.add_random_pixels_to_image(original_image)
        await self.client.exerciseRunningFace(modified_image, coordinates, self.identify, self.publicRunTypeConvert(self.logRunType), faceType)
        self.faceUploaded = True

    async def queryPath(self):
        resultStmt = select(RunPath).where(RunPath.school_code == self.client.schoolCode).order_by(func.abs(RunPath.sport_range - self.runKiloMeter)).limit(10)
        async for newDb in get_db():
            result = await newDb.execute(resultStmt)
            runPathList = result.scalars().all()
            if len(runPathList) == 0:
                raise TiShiNengError('没有可用的跑步路线', 200000)
            runPath = random.choice(runPathList)
            result = {'runLinePath': json.loads(runPath.run_line_path)}
            runLinePath = result['runLinePath']
            return runLinePath
        return None

    def getOkPointList(self, startTime, endTime):
        okPointList = []
        timeStep = (endTime - startTime) // (len(self.pointList) + 4)
        timeStamp = startTime + timeStep
        for i in self.pointList:
            dt_object = datetime.datetime.fromtimestamp(timeStamp / 1000, LOCAL_TZ)
            formatted_string = dt_object.strftime('%Y-%m-%d %H:%M:%S')
            tmp = {'latitude': float(i['latitude']), 'content': i['content'], 'id': i['id'], 'time': formatted_string, 'longitude': float(i['longitude'])}
            if 'sort' in i:
                tmp['sort'] = i['sort']
            okPointList.append(tmp)
            timeStamp += timeStep
        return okPointList

    def buildMiddleFacePoints(self, path, startTime, endTime):
        points = face_plan.build_middle_face_points(path=path, start_timestamp_ms=int(startTime), end_timestamp_ms=int(endTime), middle_faces=self.middleFaces, is_public=bool(self.isPublic), face_type=2)
        result = [point.as_dict() for point in points]
        logger.info(f'中途人脸验证点: {result}')
        return result

    async def uploadRunPath(self, path, stepNumbers, sumDistance):
        sportRange = str(round(sumDistance / 1000, 2))
        gpsDistance = sportRange
        if self.isPublic:
            endTime = path[-1]['t']
        else:
            endTime = path[-1]['time']
        endTime += random.randint(100, 200)
        endTimeStr = datetime.datetime.fromtimestamp(endTime / 1000, LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')
        logger.info(f'跑步中，预计结束时间：{endTimeStr}')
        usedTime = (endTime - int(self.start_timestamp)) // 1000
        okPointList = self.getOkPointList(int(self.start_timestamp), endTime)
        distanceKm = sumDistance / 1000
        paceTime = usedTime / distanceKm
        paceMinute = int(paceTime // 60)
        paceSecond = int(paceTime % 60)
        pace = f'{paceMinute}\u2032{paceSecond:02d}\u2033'
        logger.info(f'配速：{pace}')
        logger.info(f'usedTime:{usedTime}')
        sleepTime = int(endTime) / 1000 - time.time()
        taskList = []
        logger.info(f'等待{sleepTime}秒')
        taskList.append(asyncio.sleep(sleepTime))
        middleFacePoints = self.buildMiddleFacePoints(path, int(self.start_timestamp), endTime)
        for middleFaceItem in middleFacePoints:
            if sleepTime > 0:
                middleUsedTime = int(middleFaceItem['timestamp']) / 1000 - time.time()
                if middleUsedTime < 0:
                    middleUsedTime = 0
            else:
                middleUsedTime = 0
            latitude = middleFaceItem['latitude']
            longitude = middleFaceItem['longitude']
            coordinates = f'{latitude},{longitude}'
            taskList.append(self.uploadFace(coordinates=coordinates, sleep=middleUsedTime, faceType=2))
        await asyncio.gather(*taskList)
        avgSpeed = round(sumDistance / usedTime * 3.6, 2)
        if self.isEndFace == 1:
            logger.info('结束跑人脸识别')
            lastPoint = path[-1]
            if self.isPublic:
                lat = lastPoint['a']
                lng = lastPoint['o']
            else:
                lat = lastPoint['latitude']
                lng = lastPoint['longitude']
            coordinates = f'{lat},{lng}'
            await self.uploadFace(coordinates=coordinates, sleep=0, faceType=3)
        pointList = self.pointList.copy()
        for point in pointList:
            if 'isMustPoint' in point:
                point['isMustPoint'] = float(point['isMustPoint'])
            if 'okRadius' in point:
                point['okRadius'] = float(point['okRadius'])
        logger.info('上传跑步数据')
        for item in path:
            item.pop('distance', None)
        if self.isPublic:
            total_steps = sum((int(n) for n in stepNumbers))
            setting = self.exerciseSetting or {}
            submit_extras = submit.build_submit_extras(setting=setting, points=path, elapsed_s=usedTime, distance_m=sumDistance, steps=total_steps, user_open_develop=setting.get('userOpenDevelop', False), offline=0)
            logger.info(f'{deviceModel.app_version} 提交字段: {submit_extras}')
            logger.info(f'合格性判定明细: {submit.qualification_detail(setting, usedTime, sumDistance, total_steps)}')
            faceRequired = bool(getattr(self, 'middleFaces', None)) or getattr(self, 'isStartFace', 0) == 1 or getattr(self, 'isEndFace', 0) == 1 or getattr(self, 'isMidwayFace', 0) == 1
            gate_meta = {
                'sport_range_km': round(sumDistance / 1000, 2),
                'sport_time_s': usedTime,
                'distance_m': float(sumDistance),
                'total_steps': total_steps,
                'identify': self.identify,
                'start_ts_ms': int(self.start_timestamp),
                'end_ts_ms': int(endTime),
                'pace': pace,
                'app_version': getattr(self.tsnClient, 'versionName', ''),
                'brand': getattr(self.tsnClient, 'brandName', ''),
                'model': getattr(self.tsnClient, 'deviceNum', ''),
                'system': (getattr(self.tsnClient, 'v2BaseDict', {}) or {}).get('system', ''),
                'osver': getattr(self.tsnClient, 'AndroidOsVersion', ''),
                # 单次要求 = rangeSetting（公有接口的 totalRange 是阶段累计，不能当门槛）
                'required_range_m': submit.per_run_range_meters(setting),
                'range_setting': submit.per_run_range_meters(setting) / 1000.0,
                'step_setting': setting.get('stepSetting') or setting.get('step'),
                'start_qualified_min': setting.get('startQualifiedTime'),
                'end_qualified_min': submit.adaptive_end_qualified_time(setting, sumDistance),
                'ok_point_count': len(okPointList),
                'point_count': len(path),
                'sport_type': self.publicRunTypeConvert(self.logRunType),
                'limit_speed': self.limitSpeed,
                'limit_step_frequency': self.endLimitStepFrequency,
                'face_ok': bool(self.faceUploaded) or not faceRequired,
            }
            gate_report = compliance.check_submission(path, stepNumbers, gate_meta, fence=self.geofence)
            logger.info(f'合格门禁: {gate_report.summary()}')
            for issue in gate_report.issues:
                logger.warning(f'  [{issue.severity}] {issue.code}: {issue.message}')
            gate_report.raise_if_failed()
            stopResp, exerciseRecordId = await self.client.addExerciseRecord(sportType=self.publicRunTypeConvert(self.logRunType), startTime=self.start_timestamp, endTime=endTime, sportTime=usedTime, sportRange=sportRange, speed=pace, avgSpeed=str(avgSpeed), gitudeLatitude=json.dumps(path), stepNumbers=json.dumps(stepNumbers), isSequencePoint='0', pointList=json.dumps(pointList, ensure_ascii=False), okPointList=json.dumps(okPointList, ensure_ascii=False), isFaceStatus=self.isFaceStatus, uploadType=0, identify=self.identify, geofence=json.dumps(self.geofence), limitSpeed=self.limitSpeed, limitStride=self.endStride, limitStepFrequency=self.endLimitStepFrequency, gpsDistance=gpsDistance, d=submit_extras.get('d'), f=submit_extras.get('f'), m=submit_extras.get('m'), h=submit_extras.get('h'), isValid=submit_extras.get('isValid'), remarkId=submit_extras.get('remarkId'), setting=setting, trackPoints=path, stepCount=total_steps, elapsedSeconds=usedTime, distanceMeters=sumDistance)
            await self.client.sumExerciseRecord()
            logger.info(stopResp)
            logger.info(exerciseRecordId)
            if exerciseRecordId is not None:
                exerciseRecordResp = await self.client.getExerciseRecord(exerciseRecordId)
                if not isinstance(exerciseRecordResp, dict):
                    logger.warning('记录已提交但回读失败（id={}），无法判断是否合格', exerciseRecordId)
                else:
                    remark = exerciseRecordResp.get('remark') or ''
                    logger.info(remark)
                    if str(exerciseRecordResp.get('sportStatus')) != '1':
                        raise TiShiNengError(remark or '记录未通过判定', 200000)
                    compliance.record_success(gate_meta)
                await self.client.getFeedbackBalance()
                await self.client.sumExerciseRecord()
                await self.client.statisticsExerciseRecord()
        else:
            formatSportTime = seconds_to_time_format(usedTime)
            stopResp = await self.client.appAddSportRecord(runType=self.logRunType.value, startTime=self.start_timestamp, endTime=endTime, gitudeLatitude=json.dumps(path), identify=self.identify, formatSportTime=formatSportTime, formatSportRange=sportRange, avgspeed=str(avgSpeed), speed=pace, okPointList=json.dumps(okPointList), stepNumbers=json.dumps(stepNumbers), isFaceStatus=self.privateFaceStatus, points=json.dumps(self.pointList), uploadType=0)
            logger.info(stopResp)
            sportRecord = await self.client.sumSportRecord()
            await asyncio.sleep(5)
            appSportRecordList = await self.client.appSportRecordList()
            rows = (appSportRecordList or {}).get('data') or []
            if not rows:
                logger.warning('私有版记录已提交但列表回读为空，无法判断是否合格')
            else:
                latest = rows[0]
                if str(latest.get('sportStatus')) != '1':
                    raise TiShiNengError(latest.get('remark') or '记录未通过判定', 200000)
            logger.info('跑步数据上传完成')

    async def startRun(self):
        async for newDb in get_db():
            self.accountModel = await getTsnAccountByid(self.accountId, newDb)
            self.tsnClient = await getTsnClientById(self.accountModel.id, newDb)
        self.isPublic = self.client.isPublic()
        canRunTypeList = []
        if self.isPublic:
            initSumRunResp = await self.client.sumExerciseRecord()
            if initSumRunResp['morningRun']['isShow'] == '1':
                canRunTypeList.append(TsnRunType.morningRun)
            if initSumRunResp['sunRun']['isShow'] == '1':
                canRunTypeList.append(TsnRunType.sumRun)
            if initSumRunResp['freedomRun']['isShow'] == '1':
                canRunTypeList.append(TsnRunType.freedom)
        else:
            initSettingResp = await self.client.sportRecordSetting()
            freedom = initSettingResp['freedom']
            sunRun = initSettingResp['sunRun']
            morningRun = initSettingResp['morningRun']
            if freedom == 0:
                canRunTypeList.append(TsnRunType.freedom)
            if sunRun == 0:
                canRunTypeList.append(TsnRunType.sumRun)
            if morningRun == 0:
                canRunTypeList.append(TsnRunType.morningRun)
        if len(canRunTypeList) == 0:
            raise TiShiNengError('没有可用的跑步类型', 200000)
        logger.info(f'可用跑步类型：{canRunTypeList}')
        if self.logRunType not in canRunTypeList:
            raise TiShiNengError('当前跑步类型不可用', 200000)
        runLinePath = await self.queryPath()
        startPoint = runLinePath[0]
        logger.info(f'起点信息：{startPoint}')
        if self.isPublic:
            longitude = startPoint[0]
            latitude = startPoint[1]
            if longitude is not None and latitude is not None:
                longitude, latitude = self._randomize_coordinates(longitude, latitude)
            self.startLongitude = longitude
            self.startLatitude = latitude
            self.exerciseSetting = await self.client.getExerciseSetting(self.publicRunTypeConvert(self.logRunType), longitude, latitude)
            logger.info(f'school{self.client.schoolId} setting Info:{self.exerciseSetting}')
            await self.client.getExerciseExplanation()
        else:
            self.exerciseSetting = await self.client.getSportSetting(self.logRunType.value)
        if self.identify is None or self.start_timestamp is None:
            self.identify = self.exerciseSetting['identify']
            if not re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', str(self.identify or '')):
                logger.warning(f'identify 不是标准 UUID，服务端极易判定数据异常: {self.identify!r}')
            if self.isPublic:
                startTimeResp = await self.client.getExerciseStartTime(self.identify)
            else:
                startTimeResp = await self.client.getRunningStartTime(self.identify)
            self.start_timestamp = int(startTimeResp['startTime']) + random.randint(14, 20) * 1000
        self.isStartFace = int(self.exerciseSetting.get('isStartFace', 0))
        self.isEndFace = int(self.exerciseSetting.get('isEndFace', 0))
        self.middleFaces = self.exerciseSetting.get('middleFaces', [])
        self.isMidwayFace = int(self.exerciseSetting.get('isMidwayFace', 0))
        if len(self.middleFaces) == 0 and self.isMidwayFace == 1:
            self.middleFaces.append(random.uniform(0.5, 1.2))
        if not (len(self.middleFaces) > 0 or self.isStartFace == 1 or self.isEndFace == 1 or (self.isMidwayFace == 1)):
            self.isFaceStatus = 0
        if hasattr(self.tsnClient, 'setEnvSignals'):
            self.client.setEnvSignals(env.clean_device_signals())
        if self.isStartFace == 1:
            logger.info('开始人脸识别')
            coordinates = f'{self.startLatitude},{self.startLongitude}'
            await asyncio.sleep(random.uniform(7, 12))
            await self.uploadFace(coordinates=coordinates, faceType=1)
        self.geofence = self.exerciseSetting['geofence']
        self.pointList = self.exerciseSetting.get('list', [])
        if self.pointList == '':
            self.pointList = []
        if 'campusList' in self.exerciseSetting:
            campusList = self.exerciseSetting['campusList']
            campusIdPointDict = {}
            for campus in campusList:
                campusIdPointDict[campus['id']] = campus['point']
            userInfo = await self.client.getLoginUserInfo()
            campusId = userInfo['campusId']
            if campusId in campusIdPointDict:
                self.pointList = campusIdPointDict[campusId]
            else:
                self.pointList = campusIdPointDict[random.choice(list(campusIdPointDict.keys()))]
        logger.info(f'pointList:{self.pointList}')
        self.needRunKm = self.exerciseSetting['totalRange']
        if self.runKiloMeter < self.needRunKm:
            self.runKiloMeter = self.needRunKm + random.uniform(0.1, 0.2)
        logger.info(self.exerciseSetting)
        self.planUseTime = self.runKiloMeter * random.uniform(6.2, 8.4) * 60
        logger.info(f'planUseTime:{self.planUseTime}')
        if self.isPublic:
            self.endStride = self.exerciseSetting['endStride']
            self.limitSpeed = self.exerciseSetting['limitSpeed']
            self.endLimitStepFrequency = self.exerciseSetting['endLimitStepFrequency']
        await asyncio.sleep(random.uniform(1, 4))
        geoData, stepList, sumDistance = genTiShiNengRunPathRepeat(runLinePath, self.runKiloMeter * 1000, int(self.start_timestamp) + random.randint(1200, 1800), self.planUseTime, self.isPublic)
        await self.uploadRunPath(geoData, stepList, sumDistance)

    async def startRunHandle(self):
        logger.info('开始运行')
        await self.startRun()
