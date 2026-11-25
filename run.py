#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""非交互命令行驱动：把 main.py 的交互流程拆成子命令，便于脚本化执行。

用法（示例一律用北京大学：school_id=100、schoolCode=pku、sysType=2；换成自己学校即可）:
  python run.py school-info --code pku
  python run.py fetch-schools --province-id 11          # 北京市；--all 拉全量
  python run.py add-school --school-id 100 --code pku \
        --name 北京大学 --sys-type 2 --province 北京市
  python run.py list-schools --keyword 北京大学
  python run.py auth --school-id 100 --user 学号 --password 密码
  python run.py accounts
  python run.py crawl --account 1
  python run.py check --account 1 --type sun
  python run.py run --account 1 --type freedom --km 2 --yes
  python run.py stats --account 1
  python run.py face --account 1
  python run.py seedpath --school-code pku --lat 39.9928 --lng 116.3055 --km 2
  python run.py selftest --count 3
  python run.py backfill --count 14 --dry-run
  python run.py backfill --plan plan.json --submit --max 3 --per-day 3 --window 6-9
  python run.py progress
  python run.py verify-plan
  python run.py sandbox --count 3 --reject-demo   # 本地沙盘：无需账号密码，不联网

学校标识怎么查：`school-info --code pku` 拿校园云地址（不需要本地库）；
`list-schools --keyword 北京大学` 查本地库里的 school_id —— 但它要有库，首次请先跑
`fetch-schools --province-id 11`（北京市全量）或 `add-school`（只加一所）建库。
示例里的 id/code 只是占位，换成自己学校的即可。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 只用 reconfigure()：`sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 会接管底层
# buffer，wrapper 被回收时关闭捕获流（pytest 下报 `ValueError: I/O operation on closed file`）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (ValueError, OSError):
            pass

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tsn import routes
from app.database import get_db, init_db
from app.models import RunPath, TsnAccount_Model, TsnSchool_Model
from tsn.dao import addOrUpdateSchool, getSchoolBySchoolId
from tsn.dao import getTsnAccountByid
from sdk.private import TiShiNengPrivate
from sdk.client import getTsnClientById, tsnPasswordAuthServer
from sdk.runserver import TsnRunServer, TsnRunType
from sdk.spider import startSpider
from sdk.validcount import crawl_valid_count_for_account

logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"),
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}")

RUN_TYPE = {"morning": TsnRunType.morningRun, "sun": TsnRunType.sumRun, "freedom": TsnRunType.freedom}


def base_client() -> TiShiNengPrivate:
    return TiShiNengPrivate(1, 1, "", False, str(uuid.uuid4()), "Xiaomi", "25042PN24C", "")


# ---------------------------------------------------------------- 学校

async def cmd_school_info(args):
    """h.tsnkj.com 拉取 schoolCode 对应的校园云地址"""
    url = f"https://h.tsnkj.com/upms/sysSchool/getSchoolInfo?schoolCode={args.code}"
    r = httpx.get(url, headers={"User-Agent": "okhttp/4.9.0"}, timeout=15)
    print(r.status_code, r.text[:800])


async def cmd_fetch_schools(args):
    await init_db()
    tsn = base_client()
    resp = await tsn.findAllProvince()
    provinces = resp["data"]
    if args.province_id:
        provinces = [p for p in provinces if str(p["province_id"]) == str(args.province_id)]
    elif not args.all:
        for p in provinces:
            print(f"{p['province_id']:>3}  {p['province_name']}")
        print("\n加 --province-id N 抓单个省，加 --all 抓全部")
        return
    total = 0
    async for db in get_db():
        for province in provinces:
            pname = province["province_name"]
            list_resp = await tsn.listSchoolByProvinceId(province["province_id"])
            schools = (list_resp or {}).get("data") or []
            print(f"[{pname}] {len(schools)} 所")
            for s in schools:
                name = s["school_name"]
                if "demo" in name.lower() or "test" in name.lower():
                    continue
                lan_url = None
                if s["sysType"] == "2":
                    try:
                        info = httpx.get(
                            f"https://h.tsnkj.com/upms/sysSchool/getSchoolInfo?schoolCode={s['schoolCode']}",
                            headers={"User-Agent": "okhttp/4.9.0"}, timeout=10).json()["data"]
                        if info and info.get("url"):
                            lan_url = f"https://{info['url']}"
                    except Exception:
                        pass
                await addOrUpdateSchool(
                    s["school_id"], name, s["school_url"], lan_url, s["openId"],
                    s["isOpenKeep"] == "1", s["isOpenLive"] == "1", s["isOpenEncry"] == "1",
                    int(s["sysType"]), s["schoolCode"], db, province_name=pname)
                total += 1
    print(f"✅ 入库 {total} 所学校")


async def cmd_add_school(args):
    await init_db()
    lan_url = None
    if args.sys_type == 2:
        try:
            info = httpx.get(
                f"https://h.tsnkj.com/upms/sysSchool/getSchoolInfo?schoolCode={args.code}",
                headers={"User-Agent": "okhttp/4.9.0"}, timeout=15).json()["data"]
            if info and info.get("url"):
                lan_url = f"https://{info['url']}"
        except Exception as e:
            print(f"⚠️ getSchoolInfo 失败: {e}")
    async for db in get_db():
        sid = await addOrUpdateSchool(
            int(args.school_id), args.name, args.school_url or "", lan_url,
            args.app_id or "", False, True, False, args.sys_type, args.code, db,
            province_name=args.province)
        print(f"✅ school_id={args.school_id} {args.name} sys_type={args.sys_type} lan_url={lan_url} row={sid}")


async def cmd_list_schools(args):
    async for db in get_db():
        stmt = select(TsnSchool_Model)
        if args.keyword:
            stmt = stmt.where(TsnSchool_Model.school_name.contains(args.keyword))
        rows = (await db.execute(stmt)).scalars().all()
        for s in rows[:200]:
            tag = "公版" if s.sys_type == 2 else "私版"
            print(f"{s.school_id:>6}  {s.school_name}  [{tag}]  code={s.school_code}  lan={s.lan_url}")
        print(f"共 {len(rows)} 条")


# ---------------------------------------------------------------- 账号

async def cmd_auth(args):
    await init_db()
    async for db in get_db():
        school = await getSchoolBySchoolId(int(args.school_id), db)
        if not school:
            print(f"❌ 库里没有 school_id={args.school_id}，先 add-school / fetch-schools")
            return
        print(f"授权 {args.user} @ {school.school_name} (sys_type={school.sys_type})")
        uid = await tsnPasswordAuthServer(school.school_id, args.user, args.password, db)
        print(f"✅ 授权成功 uid={uid}")


async def cmd_accounts(args):
    async for db in get_db():
        stmt = select(TsnAccount_Model).options(selectinload(TsnAccount_Model.school))
        rows = (await db.execute(stmt)).scalars().all()
        for a in rows:
            name = a.school.school_name if a.school else "?"
            print(f"id={a.id}  user={a.username}  uid={a.user_id}  school_id={a.school_id} {name}  token={a.access_token[:16]}...")
        print(f"共 {len(rows)} 个账号")


async def cmd_crawl(args):
    try:
        await startSpider(int(args.account))
        print("✅ 路径爬取完成")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 爬取中断（已入库的部分保留）: {type(exc).__name__}: {exc}")
    async for db in get_db():
        rows = (await db.execute(select(RunPath))).scalars().all()
        for r in rows:
            print(f"  路线 {r.run_path_id}  {r.sport_range}km  school={r.school_code}  {len(r.run_line_path)}B")
        print(f"共 {len(rows)} 条路线")


async def cmd_check(args):
    """不提交，只看跑型可用性与 setting 要求。"""
    await init_db()
    async for db in get_db():
        account = await getTsnAccountByid(int(args.account), db)
        tsn = await getTsnClientById(account.id, db)
    print("=== sumExerciseRecord ===")
    summary = await tsn.sumExerciseRecord()
    for k in ("morningRun", "sunRun", "freedomRun"):
        d = summary.get(k) or {}
        print(f"  {k}: isShow={d.get('isShow')} succeed={d.get('succeed')} runSportRange={d.get('runSportRange')}")
    print(f"  totalRange={summary.get('totalRange')} succeed={summary.get('succeed')} noSucceed={summary.get('noSucceed')}")
    print("=== 历史路线起点/长度 ===")
    async for db in get_db():
        rows = (await db.execute(select(RunPath))).scalars().all()
        if not rows:
            print("  (空)")
        for r in rows[:5]:
            pts = json.loads(r.run_line_path)
            print(f"  {r.run_path_id} {r.sport_range}km 点数={len(pts)} 起点={pts[0][:2] if isinstance(pts[0], list) else pts[0]}")
    rt = RUN_TYPE[args.type]
    rts = TsnRunServer.publicRunTypeConvert(rt)
    async for db in get_db():
        rp = (await db.execute(select(RunPath))).scalars().first()
    pts = json.loads(rp.run_line_path)
    lon, lat = (pts[0][0], pts[0][1]) if isinstance(pts[0], list) else (pts[0]['o'], pts[0]['a'])
    print(f"=== getExerciseSetting(runType={rts}, {lat},{lon}) ===")
    setting = await tsn.getExerciseSetting(rts, lon, lat)
    keys = ["identify", "totalRange", "endStride", "limitSpeed", "endLimitStepFrequency",
            "isStartFace", "isEndFace", "isMidwayFace", "middleFaces", "remarks",
            "startQualifiedTime", "endQualifiedTime", "isStepRecursion", "sportRange"]
    for k in keys:
        if k in setting:
            print(f"  {k} = {setting[k]}")
    print(f"  geofence = {str(setting.get('geofence'))[:200]}")
    pl = setting.get("list") or []
    print(f"  打卡点 {len(pl)} 个: {str(pl)[:300]}")
    cl = setting.get("campusList") or []
    print(f"  校区 {len(cl)} 个: {[ (c.get('id'), c.get('name') or c.get('campusName')) for c in cl]}")
    st = await tsn.getExerciseStartTime(setting["identify"])
    print(f"  startTime = {st}")


async def cmd_stats(args):
    vc = await crawl_valid_count_for_account(int(args.account))
    print(vc)


async def cmd_face(args):
    async for db in get_db():
        account = await getTsnAccountByid(int(args.account), db)
        rs = TsnRunServer(accountId=account.id, runKiloMeter=2.0, logRunType=TsnRunType.freedom)
        rs.accountModel = account
        rs.tsnClient = await getTsnClientById(account.id, db)
        rs.isPublic = rs.client.isPublic()
        data = await rs.getFaceImage()
        print(f"✅ 人脸图片 {len(data)} bytes")


async def cmd_seedpath(args):
    """没有历史记录时，用给定中心点生成一条环形路线入库。"""
    import math
    lat, lng, km = float(args.lat), float(args.lng), float(args.km)
    n = 120
    r_km = km / (2 * math.pi)
    pts = []
    for i in range(n + 1):
        ang = 2 * math.pi * i / n
        dlat = (r_km / 111.0) * math.cos(ang)
        dlng = (r_km / (111.0 * math.cos(math.radians(lat)))) * math.sin(ang)
        pts.append([round(lng + dlng, 7), round(lat + dlat, 7)])
    await init_db()
    async for db in get_db():
        db.add(RunPath(run_path_id=f"seed-{lat}-{lng}-{km}", school_code=args.school_code,
                       sport_range=km, run_line_path=json.dumps(pts), point_id_list=None,
                       ok_point_list_json=None, is_public=True))
    print(f"✅ 已写入种子路线 {km}km，中心 {lat},{lng}")


def synthetic_route(lat0: float = 30.0, lon0: float = 120.0) -> list:
    """生成一条合成环形跑道（[lon, lat]，约 300 m 一圈），用于离线自检。

    不依赖任何真实校园数据，任何人 clone 下来都能直接跑 selftest。
    """
    import math
    semi_major, semi_minor = 62.0, 41.0
    pts = []
    for i in range(240):
        theta = 2 * math.pi * i / 240
        dlat = semi_major * math.sin(theta) / 111320.0
        dlon = semi_minor * math.cos(theta) / (111320.0 * math.cos(math.radians(lat0)))
        pts.append([round(lon0 + dlon, 7), round(lat0 + dlat, 7)])
    pts.append(pts[0])
    return pts


async def cmd_selftest(args):
    """离线跑合格门禁自检：生成 N 条轨迹，全部过 compliance.check_submission。"""
    import random
    import time
    import uuid as _uuid

    from tsn import compliance
    from app.device import deviceModel
    from sdk.runpath import genTiShiNengRunPathRepeat

    ref = synthetic_route(getattr(args, "lat", 30.0), getattr(args, "lng", 120.0))
    dev = deviceModel
    ok = 0
    for i in range(int(args.count)):
        need = random.uniform(2040, 2095)
        # 起跑时间放在白天（合规窗口 05:00–22:30 内），避免门禁报 start_window
        lt = time.localtime()
        day_base = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 30, 0, 0, 0, -1)))
        start_ts = (day_base + i * 86400 - random.randint(0, 3000)) * 1000
        plan = need / 1000 * random.uniform(5.8, 8.4) * 60
        points, segs, dist = genTiShiNengRunPathRepeat(ref, need, start_ts, plan, True)
        end_ts = int(points[-1]["t"]) + random.randint(100, 200)
        used = (end_ts - start_ts) // 1000
        total = sum(int(n) for n in segs)
        pace_s = used / (dist / 1000)
        meta = {
            "sport_range_km": round(dist / 1000, 2),
            "sport_time_s": used,
            "distance_m": dist,
            "total_steps": total,
            "identify": str(_uuid.uuid4()).upper(),
            "start_ts_ms": start_ts,
            "end_ts_ms": end_ts,
            "pace": f"{int(pace_s // 60)}\u2032{int(pace_s % 60):02d}\u2033",
            "app_version": dev.app_version,
            "brand": dev.brand,
            "model": dev.model,
            "system": dev.system,
            "osver": dev.osver,
            "required_range_m": 2000.0,
            "ok_point_count": 3,
        }
        rep = compliance.check_submission(points, segs, meta, fence=None, history=[])
        print(f"--- #{i + 1} {dist:.0f}m {meta['pace']}/km 步数 {total} 点数 {len(points)} | {rep.summary()}")
        for issue in rep.issues:
            print(f"    [{issue.severity}] {issue.code}: {issue.message}")
        ok += 1 if rep.ok else 0
    print(f"{'✅' if ok == int(args.count) else '❌'} 通过 {ok}/{args.count}")


async def cmd_run(args):
    await init_db()
    rt = RUN_TYPE[args.type]
    async for db in get_db():
        account = await getTsnAccountByid(int(args.account), db)
        if not account:
            print("❌ 账号不存在")
            return
        print(f"账号 {account.username} 学校 {account.school.school_name} 类型 {args.type} 距离 {args.km}km")
    if not args.yes:
        print("加 --yes 才会真正提交")
        return
    rs = TsnRunServer(accountId=int(args.account), runKiloMeter=float(args.km), logRunType=rt)
    await rs.startRunHandle()
    print("✅ 跑步任务完成")


# ---------------------------------------------------------------- 补跑 / 盘点（独立脚本代理）
def _script(name: str) -> str:
    """仓库根下的入口脚本路径；子目录脚本用 "app/backfill.py" 这样的相对路径。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *name.split("/"))


async def cmd_backfill(args):
    import subprocess
    cmd = [sys.executable, _script("app/backfill.py"), "--count", str(args.count)]
    if args.plan:
        cmd += ["--plan", args.plan]
    if args.dates:
        cmd += ["--dates", args.dates]
    if args.max:
        cmd += ["--max", str(args.max)]
    if args.per_day:
        cmd += ["--per-day", str(args.per_day)]
    if args.window:
        cmd += ["--window", args.window]
    if args.gap_min is not None:
        cmd += ["--gap-min", str(args.gap_min)]
    if args.gap_max is not None:
        cmd += ["--gap-max", str(args.gap_max)]
    if args.resume:
        cmd += ["--resume"]
    if args.submit:
        cmd += ["--submit"]
    if args.force:
        cmd += ["--force"]
    if args.watch:
        cmd += ["--watch"]
    if args.dry_run or not args.submit:
        cmd += ["--dry-run"]
    print("$ " + " ".join(cmd[1:]))
    return subprocess.call(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))


async def cmd_progress(args):
    import subprocess
    cmd = [sys.executable, _script("app/progress.py")]
    if args.json:
        cmd.append("--json")
    return subprocess.call(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))


async def cmd_sandbox(args):
    """本地沙盘端到端跑批：不需要账号密码，全部流量走内存服务端（见 tools/tsn_sandbox.py）。"""
    import subprocess
    # --province / --codes 走多校批量入口（见 tools/sandbox_province.py）
    if getattr(args, "province", "") or getattr(args, "codes", ""):
        cmd = [sys.executable, _script("tools/sandbox_province.py")]
        if args.province:
            cmd += ["--province", args.province]
        if args.codes:
            cmd += ["--codes", args.codes]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        if args.count:
            cmd += ["--count", str(args.count)]
        if args.seed is not None:
            cmd += ["--seed", str(args.seed)]
        if args.no_face:
            cmd += ["--no-face"]
        if args.dry_run:
            cmd += ["--dry-run"]
        print("$ " + " ".join(cmd[1:]))
        return subprocess.call(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))

    cmd = [sys.executable, _script("tools/sandbox_run.py")]
    if args.count:
        cmd += ["--count", str(args.count)]
    if args.dates:
        cmd += ["--dates", args.dates]
    if args.start:
        cmd += ["--start", args.start]
    if args.end:
        cmd += ["--end", args.end]
    if args.seed is not None:
        cmd += ["--seed", str(args.seed)]
    if args.no_face:
        cmd += ["--no-face"]
    if args.reject_demo:
        cmd += ["--reject-demo"]
    print("$ " + " ".join(cmd[1:]))
    return subprocess.call(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))


async def cmd_verify_plan(args):
    import subprocess
    cmd = [sys.executable, _script("track/verify_plan.py")]
    if args.plan:
        cmd += ["--plan", args.plan]
    return subprocess.call(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))


def build_parser():
    p = argparse.ArgumentParser(description="TiShiNeng 校园跑 非交互驱动")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("school-info"); q.add_argument("--code", required=True); q.set_defaults(fn=cmd_school_info)
    q = sub.add_parser("fetch-schools"); q.add_argument("--province-id"); q.add_argument("--all", action="store_true"); q.set_defaults(fn=cmd_fetch_schools)
    q = sub.add_parser("add-school")
    q.add_argument("--school-id", required=True); q.add_argument("--code", required=True)
    q.add_argument("--name", required=True); q.add_argument("--sys-type", type=int, default=2)
    q.add_argument("--school-url", default=""); q.add_argument("--app-id", default="")
    q.add_argument("--province", default=None)
    q.set_defaults(fn=cmd_add_school)
    q = sub.add_parser("list-schools"); q.add_argument("--keyword"); q.set_defaults(fn=cmd_list_schools)

    q = sub.add_parser("auth"); q.add_argument("--school-id", required=True)
    q.add_argument("--user", required=True); q.add_argument("--password", required=True); q.set_defaults(fn=cmd_auth)
    q = sub.add_parser("accounts"); q.set_defaults(fn=cmd_accounts)
    q = sub.add_parser("crawl"); q.add_argument("--account", required=True); q.add_argument("--school-code"); q.set_defaults(fn=cmd_crawl)
    q = sub.add_parser("check"); q.add_argument("--account", required=True)
    q.add_argument("--type", choices=list(RUN_TYPE), default="sun"); q.set_defaults(fn=cmd_check)
    q = sub.add_parser("stats"); q.add_argument("--account", required=True); q.set_defaults(fn=cmd_stats)
    q = sub.add_parser("face"); q.add_argument("--account", required=True); q.set_defaults(fn=cmd_face)
    q = sub.add_parser("seedpath")
    q.add_argument("--school-code", required=True); q.add_argument("--lat", required=True)
    q.add_argument("--lng", required=True); q.add_argument("--km", type=float, default=2.0)
    q.set_defaults(fn=cmd_seedpath)
    q = sub.add_parser("selftest"); q.add_argument("--count", default=5)
    q.add_argument("--lat", default=30.0); q.add_argument("--lng", default=120.0); q.set_defaults(fn=cmd_selftest)
    q = sub.add_parser("run"); q.add_argument("--account", required=True)
    q.add_argument("--type", choices=list(RUN_TYPE), default="freedom")
    q.add_argument("--km", type=float, default=2.0); q.add_argument("--yes", action="store_true")
    q.set_defaults(fn=cmd_run)
    q = sub.add_parser("backfill", help="补跑历史日期（参数见 python backfill.py --help）")
    q.add_argument("--plan", default=""); q.add_argument("--dates", default="")
    q.add_argument("--count", type=int, default=14); q.add_argument("--max", type=int, default=0)
    q.add_argument("--per-day", type=int, default=0); q.add_argument("--window", default="")
    q.add_argument("--gap-min", type=int, default=None); q.add_argument("--gap-max", type=int, default=None)
    q.add_argument("--resume", action="store_true"); q.add_argument("--submit", action="store_true")
    q.add_argument("--force", action="store_true"); q.add_argument("--dry-run", action="store_true")
    q.add_argument("--watch", action="store_true"); q.set_defaults(fn=cmd_backfill)
    q = sub.add_parser("progress", help="阳光跑进度盘点（只读）")
    q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_progress)
    q = sub.add_parser("sandbox", help="本地沙盘端到端跑批（无需账号密码，不碰真实服务器）")
    q.add_argument("--count", type=int, default=3); q.add_argument("--dates", default="")
    q.add_argument("--start", default=""); q.add_argument("--end", default="")
    q.add_argument("--seed", type=int, default=20261125)
    q.add_argument("--no-face", action="store_true")
    q.add_argument("--reject-demo", action="store_true", help="额外跑判定反证用例")
    q.add_argument("--province", default="", help="按省份批量，如 广东省（106 所）")
    q.add_argument("--codes", default="", help="逗号分隔的 school_code，如 gzccc,sgu,scnu")
    q.add_argument("--limit", type=int, default=0, help="批量时只跑前 N 所")
    q.add_argument("--dry-run", action="store_true", help="批量时只跑本地门禁，不提交")
    q.set_defaults(fn=cmd_sandbox)
    q = sub.add_parser("verify-plan", help="独立复核 plan.json")
    q.add_argument("--plan", default=""); q.set_defaults(fn=cmd_verify_plan)
    return p


async def amain():
    args = build_parser().parse_args()
    await args.fn(args)


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n中断")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"❌ {type(exc).__name__}: {msg}")
        sys.exit(1)
