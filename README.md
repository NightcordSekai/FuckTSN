# TSN 校园跑：协议复现 · 合格判定规格 · 轨迹合成 · 批量编排

> 对一个高校「校园阳光跑」App（`com.bxkj.student`）的通信协议做完整复现，并在协议之上做五层工程：
> ① 可执行的「成绩合格」判定层；② 生理自洽的轨迹合成层；③ 跨客户端版本的字段兼容层；
> ④ 失败即停、可断点续跑的批量编排层；⑤ **不用账号、不联网也能验证全部判定的离线沙盘**。
>
> 所有实现来自**报文观察 + 服务端真实记录**两源合证，不含任何第三方代码。
> 仓库内**不含真实账号、学号、姓名、人脸与坐标**：抓回来的样本已由 `tools/sanitize.py`
> 做等价脱敏（见第八节）。用自己的账号使用本仓库，风险自担。

- 协议层：OAuth2 + AES/RSA 双层加密 + md5 签名，纯 Python 复现，不依赖 App
- 判定层：`tsn/compliance.py` 四档门禁（协议 / 硬闸门 / 经验区间 / 自设），14 条真实记录回放 0 误杀
- 生成层：步幅-步频-配速自洽曲线 + GPS 噪声 + 三种跑道原型 + 60 s 步数分桶
- 兼容层：`tsn/fields.py` 字段对照表 + `migrate_legacy()` 一键归一 + `audit_submission()` 体检
- 沙盘层：`tools/tsn_sandbox.py` 内存服务端，**不用账号、不联网**就能把
  「登录 → 人脸 → 提交 → 回读 → 盘点」整条链跑通（8 条硬闸门逐条有反证，2 条帮助中心
  「上限」因与真实合格记录矛盾而降级为诊断量，见 [`docs/沙盘复现.md`](docs/沙盘复现.md) §3.2）；
  `tools/sandbox_province.py` 再把它扩到**多校批量**（`--province 广东省` 一次 106 所，
  一所一个独立沙盘，各自去重表/人脸记录/收包计数、各自可复现的轨迹种子）
- 情报层：[`docs/服务端情报.md`](docs/服务端情报.md) —— 四个域名的真实身份、被 WAF 挡住的东西、
  匿名可读的接口清单、官方帮助中心 48 条判定规则原文，以及与沙盘 `Judge` 的逐条对账

---

## 一、三分钟看懂「怎么才算合格」

完整规格见 [`docs/合格规格.md`](docs/合格规格.md)（每条结论都标了证据来源与对应记录）。
判定 100% 发生在服务端，客户端上报的 `status`/`sportStatus`/`isValid`/`remark`/`va` 一律被重算覆盖。
服务端的短路顺序是：**人脸 → 步数 → 轨迹质量 → 拼接处罚后缀**。

| # | 决定项 | 阈值 | 被拒时的原话 |
|---|---|---|---|
| 1 | `step` | ≥ `stepSetting`（实测 1000） | `运动步数不符合131310` |
| 2 | `sportRange` | ≥ `rangeSetting`（实测 2.000 km），留 2% 以上余量 | 合并进 `数据异常-作弊嫌疑` |
| 3 | `sportTime` | ∈ `[startQualifiedTime, endQualifiedTime]`，**单位是分钟**（6–21，超距离按比例放大） | 不通过 |
| 4 | 人脸 | 三类都传，且**识别位置在规定距离内** | `人脸对比超出规定范围` |
| 5 | `identify` | 必须是 UUID | `数据异常-作弊嫌疑` |
| 6 | 频率 | 每天一条；学期累计 40 条 / 80 km 配额 | 今日已提交 / 配额 |

三个最容易踩的坑：

1. **步幅、步频、`limitStride`、`limitStepFrequency` 服务端并没有执行** —— 合格记录里步幅从
   0.95 m 到 3.79 m、步频从 47 spm 到 129 spm，全部照收。把步幅窗口当成硬闸门只会误杀。
2. **点串不参与距离判定** —— 服务端只信 `sportRange` / `gpsDistance`；真实合格记录按
   `∫|s|·dt` 算只比上报多 +0.53%~+37.1%，但按点串 haversine 累加却是上报的 **3.7–5.7 倍**
   （`docs/合格规格.md` 附录 A 的「链长/上报」列用的是前者）。两条口径都不是判据，
   服务器不重算点位；但点串**短于**上报会挨罚，且 9 个键必须齐、值必须是字符串。
3. **`startQualifiedTimeSetting=6` 是"6 分钟"，不是"6 点"** —— 实测 21:10 开跑照样合格。

另外两条来自官方帮助中心（`/upms/sysHelpCenter/sysHelpCenterList`，匿名可读）的「上限」，
**实测和真实合格记录矛盾**，沙盘里只做诊断不做拦截：

- 「轨迹距离大于步数的两倍则提示异常」→ 5 条真实合格记录的点串/步数 = 3.8 / 9.4 / 10.8 / 3.8 / 7.4，全部超 2×
- 「某一时间段步幅 > 2.5 米/步」→ 同样 5 条记录 60 s 段最差 7.5 / 86.6 / 396.3 / 5.8 / 60.2 m/步，全部超 2.5

详见 [`docs/沙盘复现.md`](docs/沙盘复现.md) §3.2 与 [`docs/服务端情报.md`](docs/服务端情报.md) §5.1。

判断某条到底算不算数，只看一个字段：`reachRange == sportRange` 时计入成绩，`0.000` 就是没算。

---

## 二、快速开始

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Linux/macOS: .venv/bin/pip

copy config.example.json config.json               # 见「配置」
echo 学号> account.txt
echo 密码>> account.txt

# ① 离线自检：造数据 + 过门禁，不联网
python run.py selftest --count 3

# ①' 离线沙盘：不用账号、不联网，把「登录 → 人脸 → 提交 → 回读 → 盘点」整条链跑通
python run.py sandbox --count 5 --reject-demo      # 判定反证 + 5 条真实合格记录回放
python run.py sandbox --province 广东省            # 全省 106 所，一所一个独立沙盘
python run.py sandbox --codes gzccc,sgu,scnu      # 指定几所
#    去掉 config.json / account.txt 也能跑：它把 make_client() 整体换掉，不读账号、不发一个真实请求

# ② 接学校与账号（示例＝北京大学：school_id 100 / schoolCode pku / sysType 2）
python run.py school-info --code pku
python run.py add-school --school-id 100 --code pku \
        --name 北京大学 --sys-type 2 --province 北京市
python run.py auth --school-id 100 --user 学号 --password 密码
# add-school 会建库；也可以先 fetch-schools --province-id 11 把整市学校入库，
# 再用 list-schools --keyword 北京大学 核对 school_id。
# 示例用的 id/code 只是占位，换成自己学校的即可。

# ③ 只读盘点与门禁预演
python run.py stats --account 1
python run.py backfill --count 14 --dry-run
python run.py verify-plan

# ④ 分段提交（不加 --submit 只生成不发请求）
python run.py backfill --plan plan.json --submit --max 3 --per-day 3 --window 6-9
```

### 配置

`config.example.json` 只有七个键，全部可缺省：

```json
{
  "school_id": 100,
  "school_code": "pku",
  "base_url": "https://h.tsnkj.com",
  "account_file": "account.txt",
  "plan_file": "plan.json",
  "history_file": "run_history.json",
  "ledger_file": "logs/backfill_done.json"
}
```

- `TSN_CONFIG=/path/to/other.json` 指定另一份配置。
- `config.json`、`account.txt`、`plan.json`、`reference.json`、`logs/` 全在 `.gitignore` 里。
- `reference.json`（可选）放自己的历史线路与围栏；不放就用合成跑道，默认以 `(30.0, 120.0)`
  半径 120 m 的圆当围栏（提交前会警告）。
- Windows 控制台若 `UnicodeEncodeError: 'gbk'`，先 `chcp 65001` 并设 `PYTHONIOENCODING=utf-8`。

---

## 三、目录

根目录只留入口脚本与配置模板，代码按职责分进四个包：

```
├── run.py                      非交互 CLI（唯一入口；子命令会转发到 app/ 与 track/ 的脚本）
├── config.example.json         配置模板（复制成 config.json 后填写，config.json 不入库）
│
├── tsn/                        协议事实层：服务端怎么判、字段长什么样
│   ├── crypto.py               AES-CBC/ECB、RSA 分段、PKCS#7 / 零填充
│   ├── geo.py                  haversine、局部投影、坐标规范化
│   ├── buckets.py              60 s 步数分桶（最大余数法）
│   ├── env.py                  设备环境自检报文（root/selinux/xposed/代理…）
│   ├── submit.py               提交体组装、签名要素、合格判定公式
│   ├── face_plan.py            中途人脸点的时机换算
│   ├── face_store.py           本地人脸图片管理
│   ├── fields.py           ★  字段对照表（81 条）+ migrate_legacy() + audit_submission()
│   ├── compliance.py       ★  合格门禁（四档分层）
│   ├── protocol.py             报文分类（业务错误 / 网关拦截 / 服务端降级）
│   ├── http.py                 httpx 封装（超时/重试/时区）
│   ├── routes.py               路由常量（令牌化路径 ↔ 语义名）
│   ├── secrets.py              令牌 / 凭据的本地存放
│   ├── dao.py                  账号 / 学校服务层（异步 SQLAlchemy）
│   └── log.py                  日志（loguru，缺失时回落 logging）
│
├── sdk/                        与校园云通信的 SDK 与本地服务
│   ├── public.py               公版后端（sys_type=2）：AES+RSA+md5 签名、OAuth2
│   ├── private.py              私版后端（sys_type=1）：token header + RSA 加密密码
│   ├── base.py                 HTTP 基类（HTTPS、超时、统一错误）
│   ├── runpath.py              点串生成（App 原生 9 字段字符串格式）
│   ├── client.py               账号 → 已登录客户端
│   ├── runserver.py            跑量提交编排（人脸 → 轨迹 → 门禁 → 提交 → 回读）
│   ├── resilient.py            token 失效自重试包装
│   ├── spider.py               历史记录抓取
│   ├── validcount.py           有效次数统计
│   └── errors.py               异常与错误码
│
├── track/                      轨迹生成与质检
│   ├── resample.py             等间隔重采样
│   ├── shape.py                线路形态合成（椭圆 / 折返 / 8 字）
│   ├── metrics.py              距离 / 配速 / 步频 / 步幅
│   ├── perturb.py              平滑扰动（避免机械重复）
│   ├── plot.py                 轨迹出图
│   ├── filter.py               轨迹清洗（重复点 / 几何尖刺）
│   ├── audit.py                轨迹自洽校验（距离 / 速度 / 时间戳 / 累计）
│   └── verify_plan.py          独立复核器（自带 haversine，不复用合规代码）
│
├── app/                        应用层
│   ├── config.py               配置读取（config.json / TSN_CONFIG）
│   ├── models.py               ORM 模型
│   ├── database.py             异步引擎 / 会话
│   ├── device.py               设备画像（brand/model/system/osver）
│   ├── main.py                 交互式菜单
│   ├── backfill.py         ★  批量编排执行器（台账 / 限速 / 盯守）
│   └── progress.py             学期盘点（只读）
│
├── docs/                       合格规格 / 字段对照 / 判定链路 / 接口报文 / 逆向笔记 / 重构说明 / 沙盘复现
├── tools/                      sanitize.py、audit_gates.py、render_field_doc.py、snapshot_legacy.py、
│                               tsn_sandbox.py（内存服务端）、sandbox_run.py（单校入口）、
│                               sandbox_province.py（多校批量，按省份/学校码）
└── tests/                      协议、路由契约、门禁、字段、几何等价性、沙盘
```

---

## 四、协议层：三条链路

| 链路 | 用途 | 关键点 |
|---|---|---|
| `m.boxkj.com` 公共服务 | 省份/学校列表 | md5 签名：`md5(按 key 排序的 k=v&…&appId&appSecret)` |
| `h.tsnkj.com` 网关 | 学校档案 | `getSchoolInfo?schoolCode=` → `lan_url`、`sys_type` |
| 学校后端（`lan_url`） | 业务 | 公版 OAuth2 Bearer；每请求动态生成 `aesRandomKey=RSA(md5(token+ts))` 与 `sign` |

**踩过的坑**：`lan_url` 不带尾斜杠时 `cloudUrl + url` 会拼成 `https://xxx.tsnkj.comupms/...`
（`getaddrinfo failed`）。所有拼接统一走 `tsn.routes.join_url()`。

报文结构（`addExerciseRecord`）与五处判定点的位置见
[`docs/接口报文与判定.md`](docs/接口报文与判定.md)；判定链路见
[`docs/判定链路.md`](docs/判定链路.md)。

---

## 五、判定层：门禁怎么分层

`tsn/compliance.py` 给每条检查打上 tier，模式决定哪些 tier 致命：

| tier | 含义 | 致命？ |
|---|---|---|
| `protocol` | 报文结构错（9 键不齐、值非字符串、`t` 倒退、配速格式）→ 服务端直接 403/999 | 永远 |
| `server` | 有真实拒绝证据的硬闸门（步数、里程、时长窗口、人脸、identify、点串偏短…） | `balanced` / `strict` |
| `heuristic` | 从合格记录归纳的区间（步幅、步频、抖动、链长上限…），超出只提醒 | 仅 `strict` |
| `self` | 自设的保守要求（设备画像一致性） | 仅 `strict` |

**链长（`chain_delta_ratio`）只挂在 `heuristic` 档是对的**：真实合格记录按 haversine 复算
是上报里程的 3.7–5.7 倍（`docs/合格规格.md` 附录 A 的「链长/上报」列用的是 `∫|s|·dt`，
不是 haversine）。把它抬成 `server` 档会 100% 误杀真实合格记录。

```bash
python -c "from tsn import compliance"             # 默认 balanced
python tools/audit_gates.py                        # 14 条真实记录回放，0 误杀
```

`check_submission(points, step_numbers, meta, fence, mode='balanced')` 返回
`ComplianceReport`：`.ok`、`.errors`、`.warnings`、`.by_tier()`、`.summary()`。
`error` 直接抛 `ComplianceError`，提交动作发不出去。

---

## 六、生成层：生理自洽

门禁管"能不能过"，生成层管"像不像一段真实跑动"。后者刻意**严格于**服务端当前执行的规则：
哪天服务端开始执行 `limitStride` / `limitStepFrequency`，生理自洽的数据仍然安全。

| 维度 | 做法 |
|---|---|
| 步幅 | `stride = 0.86 + 0.22×(v−2.4)`，夹到 0.62–1.30 m；由步幅反算步频、夹到 150–190，三者自洽 |
| 配速曲线 | 起步 5% 偏慢 → 中段匀速 → 末段 7% 掉速；叠 2–4 段 0.87–1.15 的随机快慢段 |
| 步数序列 | 60 s 桶 CV 2.5–2.7%（机械均分一眼可见） |
| 疲劳 | 后程累计步幅再降 3% |
| GPS 噪声 | 高斯 σ=0.30 m，首末点 ×0.35，0.4% 概率 1.6–3.2 m 多径跳点 |
| GPS 序号 | `c` 分段冻结 2–5 点后跳 1/2/3 —— 只冻结计数，不复制坐标 |
| 速度 | AR(1)：`v ← 0.86v + 0.51·N(0,1)`，一阶自相关 0.45–0.69 |
| 静止 | 3–6 段零速窗口（等红灯、系鞋带） |
| 跑道几何 | 椭圆 / 折返 / 8 字 + 车道漂移 |
| 距离口径 | 上报用平滑规划长度，点串累加比上报长 +0.3%~+2.3% |

`python run.py selftest --count 5` 可以直接看这批数据的实际分布与门禁结论。

---

## 七、兼容层：同一接口的两个客户端版本

`docs/字段对照.md` 是机器生成的字段表（81 个字段，每条标 `旧值 → 新值 / 判定权重 / 改法 / 证据`），
开头另有一张 **26 项必改清单**。代码入口是 `tsn/fields.py`：

```python
import tsn.fields

payload = tsn.fields.migrate_legacy(old_payload, device_id="abc123")   # 字段归一 + 格式化
issues  = tsn.fields.audit_submission(payload)                          # 逐字段体检
print(tsn.fields.to_markdown())                                        # 重新生成文档
```

必改项集中在六组：

| 组 | 差异 |
|---|---|
| 版本与画像 | `appVersion 3.6.814`、`iPhone`、`iPhone 17 Pro`、`iOS`、`27.0.1`（`27.3` 亦出现） |
| 身份 | `identify` 必须是 UUID（大小写不敏感），把人脸 / 轨迹 / 记录串在一起 |
| 距离 | `sportRange` 与 `gpsDistance` 同值、2 位小数，且高于 `rangeSetting` |
| 步数 | `step` ≥ `stepSetting`；`stepNumbers` 与 `step` 求和相等，段数随轨迹时长自然变化 |
| 配速 | `speed` 用 `U+2032`/`U+2033`（`7′02″`），不要贴 3′00″/10′00″ 边界 |
| 点位 | 9 键 `a c e i l m o s t`，**全字符串**；坐标 6 位小数；`t` 毫秒；`c` 是 GPS 序号不是秒偏移 |

三个语义陷阱：`totalRange` 是阶段累计里程（不是单次门槛）、`startQualifiedTimeSetting` 单位是分钟、
`okPointList`/`uploadType`/`appType`/`channel`/UA 这几项服务端并不交叉校验（回显证据见对照表）。

---

## 八、数据脱敏

抓回来的每一条真实记录都在入库前过一遍 `tools/sanitize.py`，变换是**确定性且可复现**的：

| 对象 | 处理 |
|---|---|
| 坐标 | `lat → −lat`、`lon → lon − 100`。`cos(lat)` 不变 ⇒ 任意两点 haversine 距离**完全不变**（实测差 < 1e-8 m），所有里程/配速/速度基线仍然成立，但地点已不在原地 |
| 记录 ID / 账号 ID | 由 sha256 派生 19 位假 ID，同一原值在所有文件里映射到同一假值 |
| `identify` UUID | uuid5 派生，保留原大小写风格（便于验证"大小写不敏感"这条结论） |
| 昵称 / `userName` | 换成占位值 |
| 文档示例 | 同上规则逐处替换；`docs/字段对照.md` 由 `tools/render_field_doc.py` 重新渲染 |

```bash
python tools/sanitize.py --check   # 只报告还残留多少处敏感值
python tools/sanitize.py           # 就地脱敏（幂等，重复跑不会二次变形）
```

脱敏后的样本放在 `tests/fixtures/`，是判定层回归测试的取证基础。

---

## 九、执行层：批量编排怎么不翻车

```
① 只读预检    python run.py check --type sun
② 生成+门禁   python run.py backfill --plan plan.json --dry-run
③ 分段提交    python run.py backfill --plan plan.json --submit --max 3 --per-day 3 --window 6-9
④ 复核        python run.py progress
```

纪律写在代码里，不靠人记：

| 机制 | 行为 |
|---|---|
| fail-fast | 首条不是 `sportStatus=1` 就停整批 |
| `--max N` / `--per-day N` | 本次最多成功 N 条 / 每天最多 N 条，超了睡到次日窗口 |
| `--window 6-9` | 只在指定小时区间提交，让 `addTime` 与 `endTime` 同区间 |
| `--gap-min/--gap-max` | 两条之间 40–90 分钟随机 |
| `--resume` + 台账 | `logs/backfill_done.json` 记已成功日期，进程被杀不重复提交 |
| 提交中断 | 网络超时**不重试**（服务端可能已建记录），停止并提示核对 |
| 盯守 | `--watch --interval 1800`：解禁瞬间自动开始；会话复用，不每轮重登 |

`occupied_dates()` 同时拉 `status=1` 和 `status=0`，所以"提交超时但服务端已建记录"不会重复提交。

---

## 十、测试与工具

```bash
python -m pytest tests -q          # 113 个用例（含 30 条沙盘用例：判定反证 / 真实记录回放 / 协议契约 / 多校）
python tools/audit_gates.py        # 14 条真实记录回放（5 条合格 0 误杀 + 被拒记录命中对应闸门）
python tools/render_field_doc.py   # 重新生成 docs/字段对照.md
python tools/sanitize.py --check   # 脱敏残留检查
python run.py selftest --count 5   # 生成器 + 门禁端到端
python run.py sandbox --count 5    # 内存服务端：登录→人脸→提交→回读→盘点，全程离线
python run.py sandbox --count 5 --reject-demo   # 额外跑判定反证 + 5 条真实合格记录回放
python run.py sandbox --province 广东省   # 全省 106 所逐校跑，产出 logs/sandbox_province.json
python run.py verify-plan         # 独立复核 plan.json
```

`tests/test_legacy_equivalence.py` 用 `tests/fixtures/legacy_golden.json` 比对几何/分桶/清洗/
重采样/校验/提交要素/环境报文/人脸换算/加解密九个模块的数值 —— 冻结基线由
`tools/snapshot_legacy.py` 生成，用来证明重构没有改变行为。`track/verify_plan.py` 则是刻意做成的
第二实现（自带 haversine、不复用 `compliance`），两份实现给出同一个结论才算通过。

---

## 十一、常见问题

**`Invalid access token`**：多进程各登录一次会互相踢 token。单进程单次登录跑完，或用
`sdk/client.py`（带 `sdk/resilient.py` 的重试包装）。

**`您的账号…错误登录次数过多，已被冻结`**：登录太频。冻结期重试返回「用户名或密码错误」，
很容易误判成密码错。等报错里的剩余秒数 +45 s，并复用会话。

**`syntax error, expect [, actual {`**：该记录的 JSON 列存的不是数组，通常是把 dict 直接塞进
点串数组。`tsn/compliance.py` 的 `protocol` 档会拦住。

**控制台乱码**：`chcp 65001` + `PYTHONIOENCODING=utf-8`。

**`getExerciseSetting` 返回「已被拉入禁跑黑名单」**：账号级处罚（客户端 remark 后缀 `-黑名单15`），
与设备/学校/跑型都无关，只能等；`app/backfill.py --watch` 会替你等。注意封禁只挡
`getExerciseSetting`，不挡 `addExerciseRecord` —— 封禁期提交仍会成功并继续累计处罚。

**提交成功但 `reachRange=0.000`**：该条未计入成绩。用 `reachRange == sportRange` 判断，
别只看 `status`。

---

## 十二、逆向是怎么做的（方法可复用）

1. **静态**：APK 是网易易盾加固（`libxeno_native.so` 21 MB + `libSecShell.so` + `libtsnsecret.so`），
   `classes.dex` 只声明 3.1 MB 数据段、541 个壳类，jadx 读不出业务代码。
2. **字符串**：路由、签名/加密的 native 源码路径（`TsnJNISecret/.../Sign.cpp`、`ClientEncrypt.cpp`）
   都在 `.so` 的 ASCII 区，`zipfile` 拆包 + 正则即可拿到完整路由表。
3. **资源**：`resources.arsc` + `res/**/*.xml` 有 2000+ 条中文串（「未在随机人脸识别点的 100 米范围内
   完成验证」「达标步数」等），是反推判定规则的金矿。
4. **动态**：Frida 一挂就自杀（易盾），`/proc/pid/mem` 读也自杀 → 放弃运行期 dump，
   改用「静态字符串 + 服务端记录」两源合证。
5. **前端**：H5 主包（`/static/js/index.*.js`）与各校管理后台（`<code>.tsnkj.com/js/login.js`）
   是**未加固**的，直接可读；`login.js` 只是 obfuscator.io 混淆，用「括号配对提字符串表 →
   `vm` 里跑 IIFE 拿旋转表 → 回填 `dec(0xNNN,'key')` → 折叠字符串拼接」四步就能还原，
   顺手能挖出 RSA 前缀、MFA 状态机、验证码前置等客户端侧契约。
6. **验证**：用 `getExerciseRecord` 取回的历史记录反推每一条阈值，再用 `tools/audit_gates.py`
   回放验证 —— 规格文档里每条阈值都能指到具体记录。**新加的闸门必须能通过 5 条真实合格记录的回放**
   （`--reject-demo` 里的「真实『合格』记录回放」段），否则就是加错了。

详见 [`docs/逆向笔记.md`](docs/逆向笔记.md)、[`docs/判定链路.md`](docs/判定链路.md)、
[`docs/重构说明.md`](docs/重构说明.md)、[`docs/沙盘复现.md`](docs/沙盘复现.md)、
[`docs/服务端情报.md`](docs/服务端情报.md)。

---

## 十三、许可与边界

本仓库代码以 GPL-3.0 发布（见 `LICENSE`）。协议细节来自对客户端与服务端报文的观察，用于
互操作与安全研究。仓库内不含任何真实账号、学号、姓名、人脸与坐标；脱敏规则与校验脚本见
第八节。运行所需的个人信息全部放在 `config.json` / `account.txt`（已 gitignore），
请只对你自己的账号使用。

[`docs/服务端情报.md`](docs/服务端情报.md) 只登记**匿名、只读**观察到的事实（域名形态、被 WAF
挡住的路径、可未授权读取的接口、官方规则原文），不含任何凭据、cookie、token 或可利用的写入通道；
其中唯一一处真实 IP 已脱敏。该文档的目的是说明「哪些判定规则是官方写明的」，不是提供攻击面。
尝试对真实服务器做未授权写入不在本仓库范围内。
