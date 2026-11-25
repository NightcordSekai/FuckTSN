"""沙盘（本地内存服务端）的离线单测：整条上报链路不发一个真实请求。

这些用例同时是「协议可复算」的取证：
- 客户端签名 / AES / RSA 编解码在服务端被**原样解回来**，签错一个字都过不去；
- 判定链路的每个拒绝分支都有一个反证用例；
- 端到端跑一遍 登录 → 设置下发 → 三类人脸 → 提交 → 回读 → 列表 → 汇总。
"""
from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from tsn import protocol
from tsn import submit

from tools.tsn_sandbox import (DEFAULT_SETTING, EDGE_RATIO, Judge, Sandbox,
                              build_client, check_environment)


def _env_json() -> str:
    from tsn import env as tsn_env
    return json.dumps(tsn_env.build_environment_payload(
        'sandbox', tsn_env.clean_device_signals(), ['arm64-v8a']), ensure_ascii=False)


def _good_fields(**over) -> dict:
    fields = {
        'identify': str(uuid.uuid4()).upper(),
        'sportRange': '2.160', 'gpsDistance': '2.160', 'step': 2200,
        'sportTime': 980, 'speed': '7′32″', 'environment': _env_json(),
    }
    fields.update(over)
    return fields


_LAT0, _LON0, _T0 = 23.523300, 113.611400, 1_700_000_000_000


def _pt(lat: float, e: int, t: int) -> dict:
    return {'a': f'{lat:.6f}', 'o': f'{_LON0:.6f}', 'e': str(e), 't': str(t),
            'c': '1', 'i': 'true', 'l': '1', 'm': '0', 's': '0'}


def _chain_json(meters: float, steps: int, seconds: int = 1200) -> str:
    """两点一条直链，总长 `meters` 米、总步数 `steps`。"""
    return json.dumps([_pt(_LAT0, 0, _T0),
                       _pt(_LAT0 + meters / 111_320.0, steps, _T0 + seconds * 1000)])


class JudgeTestCase(unittest.TestCase):
    """判定顺序与阈值：优先跑纯函数，不碰传输层。"""

    def setUp(self):
        self.judge = Judge(DEFAULT_SETTING)
        self.faces = {1, 2, 3}

    def test_pass(self):
        v = self.judge.judge(_good_fields(), self.faces)
        self.assertEqual(v['status'], '1')
        self.assertEqual(v['sportStatus'], '1')
        self.assertEqual(v['remark'], '合格')
        self.assertFalse(v['reject'])
        self.assertEqual(v['reachRange'], '2.160')

    def test_range_edge_rejected(self):
        """真实回放：sportRange=2.000 贴线被判「运动里程不符合」。"""
        v = self.judge.judge(_good_fields(sportRange='2.000', gpsDistance='2.000'), self.faces)
        self.assertTrue(v['reject'])
        self.assertEqual(v['reachRange'], '0.000')
        self.assertIn('运动里程不符合', v['remark'])
        need = 2.0 * EDGE_RATIO
        self.assertAlmostEqual(need, 2.01, places=6)

    def test_gps_edge_rejected(self):
        v = self.judge.judge(_good_fields(gpsDistance='2.001'), self.faces)
        self.assertTrue(v['reject'])
        self.assertIn('GPS里程不符合', v['remark'])

    def test_steps_rejected(self):
        v = self.judge.judge(_good_fields(step=999), self.faces)
        self.assertTrue(v['reject'])
        self.assertIn('步数', v['remark'])

    def test_identify_rejected(self):
        v = self.judge.judge(_good_fields(identify='1'), self.faces)
        self.assertTrue(v['reject'])
        self.assertIn('识别码', v['remark'])

    def test_window_rejected(self):
        v = self.judge.judge(_good_fields(sportTime=DEFAULT_SETTING['endQualifiedTime'] * 60 + 60),
                             self.faces)
        self.assertTrue(v['reject'])
        self.assertIn('运动时间', v['remark'])

    def test_pace_rejected(self):
        v = self.judge.judge(_good_fields(speed='10.44'), self.faces)
        self.assertTrue(v['reject'])
        self.assertIn('配速', v['remark'])

    def test_face_missing_rejected(self):
        v = self.judge.judge(_good_fields(), {1, 2})
        self.assertTrue(v['reject'])
        self.assertIn('人脸', v['remark'])

    def test_environment_missing_rejected(self):
        v = self.judge.judge(_good_fields(environment='MISSING'), self.faces)
        self.assertTrue(v['reject'])
        self.assertIn('JSON', v['remark'])

    def test_chain_over_twice_steps_is_diagnostic_only(self):
        """帮助中心那句「轨迹距离 > 步数两倍即异常」**只做诊断，不做硬拒**。

        取证：`tests/fixtures/records_qualified.json` 里 5 条服务器判「合格」的真实记录，
        chain/step = 3.805 / 9.445 / 10.832 / 3.799 / 7.414，全部 > 2.0。若按硬拒处理
        会 100% 误杀真实合格记录，所以这条只写进 `checks`。
        """
        # 2600m 链长 vs 1100 步 → 2×1100=2200 < 2600；步数本身已过门槛
        v = self.judge.judge(_good_fields(step=1100, gitudeLatitude=_chain_json(2600.0, 1100)),
                             self.faces)
        self.assertFalse(v['reject'], v['remark'])
        self.assertTrue(v['checks']['chain_over_2x_steps'])

    def test_window_stride_over_limit_is_diagnostic_only(self):
        """帮助中心「某一时间段 1 步里程 > 2.5 米」同样只诊断。

        真实合格记录 60s 窗口最差 7.468 / 86.584 / 396.339 / 5.789 / 60.243，全部 > 2.5。
        """
        pts = [_pt(_LAT0, i * 10, _T0 + i * 1000) for i in range(121)]
        pts.append(_pt(_LAT0 + 50.0 / 111_320.0, 1201, _T0 + 121_000))
        v = self.judge.judge(_good_fields(step=1201, gitudeLatitude=json.dumps(pts)), self.faces)
        self.assertFalse(v['reject'], v['remark'])
        self.assertTrue(v['checks']['worst_window_over_limit'])

    def test_real_qualified_records_all_pass(self):
        """5 条服务器判「合格」的真实记录，沙盘必须一条都不拒；且它们全部违反那两条上限。

        这是「上限两条不许当硬拒」的实证：如果它们能当硬拒，这 5 条就该被拒。
        """
        raw = json.loads((Path(__file__).resolve().parents[1] /
                          'tests/fixtures/records_qualified.json').read_text(encoding='utf-8'))
        rows = list(raw.values()) if isinstance(raw, dict) else raw
        self.assertEqual(len(rows), 5)
        for row in rows:
            setting = dict(DEFAULT_SETTING, **{k: row[k] for k in
                                               ('rangeSetting', 'stepSetting', 'totalRange')
                                               if row.get(k) is not None})
            for src, dst in (('startQualifiedTimeSetting', 'startQualifiedTime'),
                             ('endQualifiedTimeSetting', 'endQualifiedTime')):
                if row.get(src) is not None:
                    setting[dst] = row[src]
            points = row.get('gitudeLatitude')
            fields = {
                'identify': str(uuid.uuid4()),
                'sportRange': str(row['sportRange']), 'gpsDistance': str(row['sportRange']),
                'step': int(row['step']), 'sportTime': int(float(row['sportTime'])),
                'speed': str(row['speed']),
                'gitudeLatitude': points if isinstance(points, str) else json.dumps(points),
                'environment': _env_json(),
            }
            v = Judge(setting).judge(fields, self.faces)
            self.assertFalse(v['reject'], f"{row['id']} → {v['remark']}")
            self.assertTrue(v['checks']['chain_over_2x_steps'] or
                            v['checks']['worst_window_over_limit']) if v['checks'] else None

    def test_window_stride_under_limit_passes(self):
        """同样形状但 60s 段内只走 2m/步 → 诊断值不拉满。"""
        pts = [_pt(_LAT0, i * 10, _T0 + i * 1000) for i in range(121)]
        pts.append(_pt(_LAT0 + 2.0 / 111_320.0, 1201, _T0 + 121_000))
        v = self.judge.judge(_good_fields(step=2200, gitudeLatitude=json.dumps(pts)), self.faces)
        self.assertFalse(v['reject'], v['remark'])
        self.assertFalse(v['checks']['worst_window_over_limit'])


class EnvironmentTestCase(unittest.TestCase):
    def test_real_payload_passes(self):
        ok, msg = check_environment(_env_json())
        self.assertTrue(ok, msg)

    def test_checksum_tampered(self):
        env = json.loads(_env_json())
        oppo = json.loads(env['oppo'])
        oppo['checksum'] = '0' * 32
        env['oppo'] = json.dumps(oppo)
        ok, msg = check_environment(env)
        self.assertFalse(ok)
        self.assertIn('checksum', msg)

    def test_risk_flag_true(self):
        env = json.loads(_env_json())
        oppo = json.loads(env['oppo'])
        oppo.pop('checksum', None)
        oppo['xposed'] = {'result': 'true'}
        import hashlib
        oppo['checksum'] = hashlib.md5(
            json.dumps(oppo, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        env['oppo'] = json.dumps(oppo)
        ok, msg = check_environment(env)
        self.assertFalse(ok)
        self.assertIn('xposed', msg)


class EndToEndTestCase(unittest.IsolatedAsyncioTestCase):
    """登录 → 设置 → 人脸 → 提交 → 回读，全程 MockTransport。"""

    async def asyncSetUp(self):
        self.box = Sandbox(DEFAULT_SETTING, verbose=False)
        self.tsn = build_client()
        self.box.attach(self.tsn)
        token = await self.tsn.getAccessToken('sandbox-user', 'whatever')
        self.tsn.setToken(token['access_token'])

    async def asyncTearDown(self):
        await self.tsn.aclose()

    async def _faces(self, identify: str):
        jpeg = b'\xff\xd8\xff' + b'\x00' * 64
        for ftype in (1, 2, 3):
            ok = await self.tsn.exerciseRunningFace(jpeg, '120.000000,30.000000', identify,
                                                   runType=1, faceType=ftype)
            self.assertIs(ok, True)

    async def test_setting_roundtrip(self):
        setting = await self.tsn.getExerciseSetting(1, '120.000000', '30.000000')
        self.assertEqual(setting['rangeSetting'], '2.000')
        self.assertEqual(setting['stepSetting'], 1000)
        self.assertEqual(setting['startQualifiedTime'], 6)
        self.assertEqual(setting['endQualifiedTime'], 21)
        self.assertEqual(setting['stepSetting'], DEFAULT_SETTING['stepSetting'])

    async def test_sign_is_verified(self):
        """签名一旦被拒绝，沙盘会直接回 code=1，这里确认正常路径没被误杀。"""
        await self.tsn.getExerciseSetting(1, '120.000000', '30.000000')
        self.assertEqual(self.box.counters['error'], 0)

    async def test_submit_and_read_back(self):
        fields = _good_fields()
        identify = fields['identify']
        await self._faces(identify)
        start = 1791979325941
        resp, rid = await self.tsn.addExerciseRecord(
            sportType='1', startTime=str(start), endTime=str(start + 980_000),
            sportTime='980', sportRange='2.160', speed='7′32″', avgSpeed='7.349',
            gitudeLatitude=json.dumps([]), stepNumbers=json.dumps([1100, 1100]),
            isSequencePoint='0', pointList=json.dumps([]), okPointList=json.dumps([]),
            isFaceStatus=1, uploadType=0, identify=identify, geofence=json.dumps([]),
            limitSpeed='3.33', limitStride='2.3', limitStepFrequency='300',
            gpsDistance='2.160', setting=DEFAULT_SETTING, elapsedSeconds=980,
            distanceMeters=2160.0, stepCount=2200, offline=0)
        self.assertTrue(rid)
        self.assertEqual(resp['status'], '1')
        self.assertEqual(resp['sportStatus'], '1')
        self.assertEqual(resp['remark'], '合格')
        self.assertEqual(resp['reachRange'], '2.160')
        self.assertEqual(resp['step'], 2200)

        rec = await self.tsn.getExerciseRecord(rid)
        self.assertEqual(rec['id'], rid)
        self.assertEqual(rec['reachRange'], rec['sportRange'])

        rows = await self.tsn.listExerciseRecord(1, '', 1)
        self.assertIsInstance(rows, list)          # occupied_dates 直接迭代
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], rid)

        total = await self.tsn.sumExerciseRecord()
        self.assertEqual(total['sunRun']['succeed'], 1)
        self.assertAlmostEqual(total['sunRun']['runSportRange'], 2.16, places=3)

    async def test_duplicate_day_is_refused(self):
        fields = _good_fields()
        await self._faces(fields['identify'])
        base = dict(
            sportType='1', endTime='1791980905941', sportTime='980', sportRange='2.160',
            speed='7′32″', avgSpeed='7.349', gitudeLatitude=json.dumps([]),
            stepNumbers=json.dumps([1100, 1100]), isSequencePoint='0',
            pointList=json.dumps([]), okPointList=json.dumps([]), isFaceStatus=1,
            uploadType=0, identify=fields['identify'], geofence=json.dumps([]),
            limitSpeed='3.33', limitStride='2.3', limitStepFrequency='300',
            gpsDistance='2.160', setting=DEFAULT_SETTING, elapsedSeconds=980,
            distanceMeters=2160.0, stepCount=2200, offline=0)
        _, first = await self.tsn.addExerciseRecord(startTime='1791979325941', **base)
        _, second = await self.tsn.addExerciseRecord(startTime='1791979825941', **base)
        self.assertEqual(first, second)
        self.assertEqual(len(self.box.records), 1)

    async def test_rejected_submit_is_recorded(self):
        """同一沙盘把里程改成贴线，必须被判 0 并记进 rejected。"""
        fields = _good_fields()
        await self._faces(fields['identify'])
        resp, rid = await self.tsn.addExerciseRecord(
            sportType='1', startTime='1791979325941', endTime='1791980905941',
            sportTime='980', sportRange='2.000', speed='7′32″', avgSpeed='7.349',
            gitudeLatitude=json.dumps([]), stepNumbers=json.dumps([1100, 1100]),
            isSequencePoint='0', pointList=json.dumps([]), okPointList=json.dumps([]),
            isFaceStatus=1, uploadType=0, identify=fields['identify'], geofence=json.dumps([]),
            limitSpeed='3.33', limitStride='2.3', limitStepFrequency='300',
            gpsDistance='2.000', setting=DEFAULT_SETTING, elapsedSeconds=980,
            distanceMeters=2000.0, stepCount=2200, offline=0)
        self.assertTrue(rid)
        self.assertEqual(resp['status'], '0')
        self.assertEqual(resp['reachRange'], '0.000')
        self.assertEqual(self.box.counters['rejected'], 1)


class BannedTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_banned_account_gets_anti_cheat(self):
        box = Sandbox(DEFAULT_SETTING, banned=True, verbose=False)
        tsn = build_client()
        box.attach(tsn)
        token = await tsn.getAccessToken('sandbox-user', 'whatever')
        tsn.setToken(token['access_token'])
        with self.assertRaises(protocol.TsnProtocolError) as ctx:
            await tsn.getExerciseSetting(1, '120.000000', '30.000000')
        self.assertIs(ctx.exception.kind, protocol.ResultKind.ANTI_CHEAT.value)
        await tsn.aclose()


class ProtocolContractTestCase(unittest.TestCase):
    """`tsn/submit.py` 里的判定口径（沙盘与客户端必须同源）。"""

    def test_required_steps_matches_setting(self):
        # isStepRecursion=1 且里程超一圈 → 按比例放大 ceil(1000 * 2160 / 2000)
        self.assertEqual(submit.required_steps(DEFAULT_SETTING, 2160.0), 1080)
        self.assertEqual(submit.required_steps(DEFAULT_SETTING, 2000.0), 1000)

    def test_required_range_prefers_range_setting(self):
        self.assertAlmostEqual(submit.per_run_range_meters(DEFAULT_SETTING), 2000.0)


class ProvinceTestCase(unittest.IsolatedAsyncioTestCase):
    """多校批量（tools/sandbox_province.py）：一所学校一个独立沙盘。"""

    async def test_load_schools_by_province(self):
        from tools import sandbox_province as prov
        rows = await prov.load_schools('广东省')
        self.assertEqual(len(rows), 106)
        self.assertTrue(all(r['province_name'] == '广东省' for r in rows))
        codes = {r['school_code'] for r in rows}
        self.assertIn('gzccc', codes)
        self.assertIn('scnu', codes)
        self.assertIn('sysu', codes)

    async def test_load_schools_by_codes(self):
        from tools import sandbox_province as prov
        rows = await prov.load_schools(codes='gzccc,sgu,scnu')
        self.assertEqual({r['school_code'] for r in rows}, {'gzccc', 'sgu', 'scnu'})

    def test_synthesis_fence_differs_from_judge_fence(self):
        """合成环必须比判定环小，否则合成器外扩会把 ~15% 的点甩出围栏。"""
        from app import backfill
        from tools import sandbox_province as prov
        points, rec = backfill.load_reference()
        setting, judge = prov.build_setting_for({}, points, rec)
        self.assertEqual(len(backfill._norm_fence(setting['geofence'])), 4)
        synth_r = prov._route_radius(points)
        judge_r = prov._judge_radius(points)
        self.assertAlmostEqual(synth_r, 96.6, places=0)
        self.assertGreaterEqual(judge_r, synth_r * 2.5)

    async def test_one_school_end_to_end(self):
        """真跑一所学校：build_run → 门禁 → 人脸 → 提交 → 回读。"""
        from tools import sandbox_province as prov
        schools = await prov.load_schools(codes='gzccc')
        packed = await prov.run_school(schools[0], 1, 20261125, False, False)
        summary = packed['summary']
        self.assertEqual(summary['error'], '')
        self.assertEqual(summary['ok'], 1)
        self.assertGreater(summary['km'], 2.0)
        self.assertEqual(packed['box'].counters['submit'], 1)
        self.assertEqual(packed['box'].counters['face'], 3)
        self.assertEqual(packed['box'].counters['error'], 0)
        self.assertEqual(summary['rows'][0]['sportStatus'], '1')

    def test_seed_is_reproducible(self):
        """`--seed` 必须真的可复现。

        `sdk/runpath.py` 里有几处随机不接外部 rng（模块级 random.randint /
        无名 random.Random()），`build_run` 里没套 `seeded()` 的话同一 seed
        连跑两次会得到不同里程（实测 2.3524 vs 2.3299）。
        """
        import hashlib
        import random
        from app import backfill
        points, rec = backfill.load_reference()
        setting = dict(backfill.DEFAULT_SETTING)
        setting.update({'totalRange': '2.000', 'startQualifiedTime': 6,
                        'endQualifiedTime': 21, 'stepSetting': 1000})
        setting['geofence'] = backfill._norm_fence(rec.get('geofence'))
        day = backfill.plan_dates(1, None, None, True, 1)[0]

        def fingerprint() -> str:
            run = backfill.build_run(day, points, setting, random.Random(20261125))
            blob = (f'{run["dist"]}|{run["used"]}|{run["steps"]}|'
                    + ''.join(p['a'] + p['o'] for p in run['points']))
            return hashlib.md5(blob.encode()).hexdigest()

        self.assertEqual(fingerprint(), fingerprint())

    async def test_schools_do_not_share_trajectories(self):
        """106 所不能跑出同一份轨迹（都用 `Random(seed)` 就会撞车）。"""
        from tools import sandbox_province as prov
        schools = await prov.load_schools(codes='gzccc,sgu,scnu')
        self.assertEqual(len(schools), 3)
        seeds = {prov.school_rng(20261125, s).random() for s in schools}
        self.assertEqual(len(seeds), 3, '三所学校的 RNG 种子撞车了')
        # 同一学校、同一 --seed 必须给出同一条记录
        again = {prov.school_rng(20261125, s).random() for s in schools}
        self.assertEqual(seeds, again)


if __name__ == '__main__':
    unittest.main()
