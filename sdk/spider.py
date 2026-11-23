import json
from loguru import logger
from sqlalchemy import select, func
from sdk.private import TiShiNengPrivate
from sdk.public import TiShiNengSdkPublic
from app.database import get_db
from app.models import RunPath, TsnRunRecord_Model
from sdk.client import getTsnClientById
from sdk.runpath import getPointListDistance

async def AddDateBase(runPathId, runLineList, sportRange, pointIdList, okPointListJson, schoolCode, isPublic=True):
    async for db in get_db():
        if isPublic:
            stmt = select(func.count(RunPath.id)).where(RunPath.run_path_id == str(runPathId))
        else:
            stmt = select(func.count(RunPath.id)).where(RunPath.run_path_id == str(runPathId), RunPath.school_code == schoolCode)
        result = await db.execute(stmt)
        isExist = result.scalar()
        if isExist > 0:
            logger.info(f'RunPath {runPathId} already exists, skipping')
            return
        repeatPointList = runLineList[:int(len(runLineList) * 0.05)]
        repeatPointListDistance = getPointListDistance(repeatPointList)
        if repeatPointListDistance < 0.1:
            logger.info(f'RunPath {runPathId} distance too short: {repeatPointListDistance}, skipping')
            return
        school_count_stmt = select(func.count(RunPath.id)).where(RunPath.school_code == schoolCode)
        result = await db.execute(school_count_stmt)
        schoolCount = result.scalar()
        if schoolCount > 10000:
            logger.info(f'School {schoolCode} has too many paths ({schoolCount}), skipping')
            return
        runLinePathJson = json.dumps(runLineList, ensure_ascii=False)
        runPath = RunPath(run_path_id=str(runPathId), school_code=schoolCode, sport_range=float(sportRange), run_line_path=runLinePathJson, point_id_list=json.dumps(pointIdList, ensure_ascii=False) if pointIdList else None, ok_point_list_json=okPointListJson, is_public=isPublic)
        db.add(runPath)
        await db.commit()
        await db.refresh(runPath)
        logger.info(f'AddDateBase success: runPathId={runPathId}, id={runPath.id}, school={schoolCode}')

async def AddRunRecord(recordId, schoolCode, sportType, rawData):
    async for db in get_db():
        stmt = select(func.count(TsnRunRecord_Model.id)).where(TsnRunRecord_Model.record_id == str(recordId))
        result = await db.execute(stmt)
        isExist = result.scalar()
        if isExist > 0:
            logger.info(f'TsnRunRecord {recordId} already exists, skipping')
            return
        record = TsnRunRecord_Model(
            record_id=str(recordId),
            school_code=schoolCode,
            sport_type=sportType,
            raw_data=json.dumps(rawData, ensure_ascii=False),
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        logger.info(f'AddRunRecord success: recordId={recordId}, id={record.id}, school={schoolCode}')

async def processRawAndAddDateBase(schoolCode, rawData, isPublic=True):
    runPathId = str(rawData['id'])
    sportRange = rawData['sportRange']
    okPointList = rawData['okPointList']
    if okPointList is None:
        okPointList = []
    okPointListJson = json.dumps(okPointList)
    print(okPointList)
    pointIdList = [i['id'] if 'id' in i else int(i) for i in okPointList]
    gitudeLatitude = rawData['gitudeLatitude']
    if len(gitudeLatitude) == 0:
        return
    if isPublic:
        runLineList = [[float(i['o']), float(i['a'])] for i in gitudeLatitude]
    else:
        runLineList = [[float(i['longitude']), float(i['latitude'])] for i in gitudeLatitude]
    await AddDateBase(runPathId, runLineList, sportRange, pointIdList, okPointListJson, schoolCode, isPublic=isPublic)

async def startSpider(accountId: int) -> None:
    client: TiShiNengPrivate | TiShiNengSdkPublic | None = None
    async for newDb in get_db():
        client = await getTsnClientById(accountId, newDb)
    if client is None:
        raise RuntimeError(f'账号 {accountId} 无可用客户端')
    if client.isPublic():
        baseRecord = await client.listExerciseRecord(1, '', 1)
        dates = baseRecord['dates']
        for date in dates:
            recodeIdList = []
            for pageIndex in range(1, 10):
                recodeList = await client.listExerciseRecord(1, date['date'], pageIndex)
                breakFlag = False
                for i in recodeList['records']:
                    if i['id'] in recodeIdList:
                        breakFlag = True
                        break
                    else:
                        recodeIdList.append(i['id'])
                if breakFlag:
                    break
            logger.info(recodeIdList)
            for i in recodeIdList:
                rawRecord = await client.getExerciseRecord(i)
                await AddRunRecord(i, client.schoolCode, rawRecord.get('sportType'), rawRecord)
                if rawRecord['sportType'] != '0' and int(rawRecord['step']) >= 500:
                    logger.info(f' {client.schoolCode} {rawRecord}')
                    await processRawAndAddDateBase(client.schoolCode, rawRecord)
    else:
        recodeIdList = []
        for pageIndex in range(1, 10):
            resp = await client.appSportRecordList(2, pageIndex, 10)
            if 'data' not in resp:
                break
            breakFlag = False
            for data in resp['data']:
                if data['sportStatus'] != 1:
                    continue
                if data['id'] in recodeIdList:
                    breakFlag = True
                    break
                else:
                    recodeIdList.append(data['id'])
            if breakFlag:
                break
        for i in recodeIdList:
            rawRecord = await client.getSportRecordId(i)
            if int(rawRecord['sportStatus']) == 1 and sum(rawRecord['stepNumbers']) >= 500:
                await processRawAndAddDateBase(client.schoolCode, rawRecord, isPublic=False)
