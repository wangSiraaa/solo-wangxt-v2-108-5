# 烘焙批次曲线对比系统

面向烘焙负责人的**过程记录**工具：并排查看豆温、环境温度与操作事件（回温点、一爆、风门变化、出锅），
而不是用成品评分替代过程。系统**不连接真实烘焙机**，数据来自带噪声、不均采样与探针失联的合成生成器。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Svelte 4 + Vite + ECharts 5 |
| API | FastAPI（Pydantic 校验） |
| 计算 | NumPy：温升率、插值、阶段指标，全部为纯函数 |
| 存储 | PostgreSQL（原始采样、下豆点、人工标记），SQLAlchemy ORM |
| 测试 | pytest，同一套用例在 SQLite 与 PostgreSQL 上运行 |

## 数据与口径（重要）

### 温升率 RoR —— 窗口必须说明
采样间隔不均（1–5 s 抖动），因此不用相邻点差分。在每个**实测**时刻 t，取居中时间窗
`[t−W/2, t+W/2]`（默认 W=30 s）内的实测豆温点做普通最小二乘直线拟合，取斜率换算 °C/min。
- 至少 4 个实测点、时间跨度 ≥10 s 才给出 RoR，否则为 null（不编造）；
- **插值点不参与拟合**；探针失联的宽缺口处 RoR 直接断档；
- 序列边缘窗口被截断，返回值带 `ror_edge=true` 标记；
- 前端另有一个“显示平滑”参数（居中均值），只作用于展示曲线，窗口本身随接口参数和图表标题一起返回。

### 缺测与插值 —— 插值段不冒充实测
- `samples` 表**只存实测**：探针失联时豆温为 NULL，绝不回写；
- 查询时对 ≤`max_gap_fill_s`（默认 45 s）的内缺口做**相邻实测点线性插值**，
  逐点带 `is_interpolated=true`，图上为**虚线+空心菱形**，图例单列“插值段（非实测）”；
- 超过桥接上限的缺口与端点缺测**不填充**，曲线断档；缺测段在“缺测与插值审计”表逐条列出（通道、时长、处理方式）。

### 事件 —— 人工修正并保留来源
- 事件为只追加（append-only）。人工提交同类型事件时，旧行置 `superseded=true` 并记录
  `superseded_by_id`，不删除；自动建议记 `source=auto`，人工记 `source=manual`+`created_by`；
- 风门变化允许多条并存（离散操作点），金色虚线标出。

### 发展时间比 —— 明确区间
| 指标 | 区间 |
|---|---|
| 脱水期 drying | 下豆 charge → 回温点 turning_point |
| 梅纳期 maillard | 回温点 → 一爆开始 first_crack_start |
| 发展期 development | 一爆开始 → 出锅 drop |
| 一爆持续 | 一爆开始 → 一爆结束 |
| 总时长 total | 下豆 → 出锅 |
| **发展时间比 DTR** | development / total |

边界事件缺失时指标为 `null`（不猜测），并返回每个锚点的来源以便审计。

### 双批次对比 —— 不宣称因果
两批次按开火/下豆时刻对齐叠加；风门变化前后的形态变化仅供观察，接口和界面都附带声明：
无对照、无重复、无统计检验，**不构成因果结论**。

### 批次组比较（3–8 批）—— 版本化快照，聚合不是测量也不是因果
负责人可建立 3–8 个批次的组，选择**明确的事件锚点**（默认一爆开始）与分析参数
（RoR 窗口/平滑、长断档桥接上限、网格步长、实测支持半径）。每次分析可固化为
**不可变组快照**（版本号、spec/result 双 SHA-256），快照中冻结：

- **成员集合**（batch id + 名称 + 合成来源 provenance）与排序；
- **锚点版本**：使用的是哪一条事件行（event id、source=auto/manual、t_s），
  之后人工修正事件只会 supersede 旧行——旧快照仍按冻结的锚点行完整回放；
- **计算口径**：`τ = t − 锚点 t_s`；网格点仅当**每个入组成员在 ±支持半径内各有一个
  非插值实测豆温样本**时才给出中位趋势与离散带，否则该点断档（不插值硬补、不外推）；
- **离散带**：成员间经验 Q25/Q75（IQR）与 min/max，3–8 个样本只代表观察散布，
  **不是置信区间、不是统计检验、不是任何一次真实测量**；
- **排除记录**：缺锚点 / 锚点落在长断档 / 锚点落在插值段 / 锚点附近无实测支持，
  逐条给出原因与细节（缺口跨度、桥接上限、最近实测距离），绝不静默丢弃；
- 每条成员的**原始曲线**（实测引导线、虚线空心菱形插值段、长断档断裂身份、RoR）
  原样保留并可在图上切换。

**并发与修正安全**：组带 `revision`，两个编辑端基于同一旧版本修改成员/参数时，先到者
成功、后到者收到 409（不产生重复成员、不静默覆盖）；同一批次重复加入在 schema 层即拒绝。
成员的关键事件被人工修正后，旧快照仍可回放，组对该快照标记 `stale` 与具体原因，
可一键固化新版本快照。刷新、下载导出与 `POST /api/group-replay` 离线重算都从同一份
spec 复现同一成员集合、参数、排除记录与聚合结果（哈希比对）。

### 本地确定性对照批次 —— 仅本地、仅演示
界面按钮 ② / `POST /api/seed-control` 在本地生成第三条**完整**批次
（`SYN-CONTROL-LOCAL-01`，固定 recipe+seed、无探针缺测、关键事件齐全、确定性可复现），
使现有两条示例批次无需导入其他题产物即可组成三条完整批次的组。该批次与含它的一切结果
都带机器可读来源标记（`is_synthetic` / `synthetic_kind=deterministic_control` /
`generator` 含 recipe 与 seed），并在**组快照、图例、导出载荷和独立重算结果**中贯穿
同一声明：**本地合成演示，不是真实稳定性结论，不来自真实烘焙机，也不是上传数据**。
接口幂等（同名同 seed 重复调用返回同一行）。


## 快速开始

### 方式一：本地

    # 终端 1 —— API（需要先有 PostgreSQL，或用 SQLite 做本地演示）
    cd backend
    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    # 默认连接 postgresql+psycopg2://roast:roast@localhost:5432/roast
    # 仅本地无 PG 时：export DATABASE_URL="sqlite:///./dev.db"
    uvicorn app.main:app --reload --port 8000

    # 终端 2 —— 前端
    cd frontend
    npm install
    npm run dev        # http://localhost:5173 （/api 已代理到 8000）

打开页面后点 **① 生成两个合成批次**：A 批在 300 s 有关一次风门（70%→40%），B 批无风门变化，
两批均含测量噪声、不均采样、一次短失联（5 s，插值桥接）和一次长失联（56 s，断档不桥接）。
再点 **② 生成本地确定性对照批次**：无缺测、关键事件齐全、固定 recipe+seed，
与前两条组成至少三条完整批次，用于批次组比较演示（全程本地、不连机、不上传）。

### 方式二：docker compose

    docker compose up --build
    # web: http://localhost:5173  api: http://localhost:8000/docs

## 验证（对应需求中的验收项）

    pytest                       # SQLite
    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

界面“缺测与插值审计 · 导出可复现”面板一键完成：
1. 导出 JSON（原始采样 + 全量事件含已取代行 + 参数 + 阶段指标）；
2. 调 `/api/recompute` 从原始数据独立重算，逐指标比对（脱水/梅纳/发展/一爆/总时长/DTR）；
3. 再用翻倍窗口、不同平滑重取曲线，逐点比对原始豆温/环温**完全不变**。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/batches` | 批次列表（含 provenance 字段） |
| POST | `/api/seed` | 生成两个合成批次 |
| POST | `/api/seed-control` | 本地生成确定性对照批次（幂等，完整 samples/全事件） |
| GET | `/api/batches/{id}/series?window_s&display_smooth_s&max_gap_fill_s` | 曲线+RoR+指标 |
| GET/POST | `/api/batches/{id}/events[?include_history=true]` | 事件列表/人工修正（只追加） |
| GET | `/api/compare?a=&b=` | 双批次叠加（含非因果声明） |
| GET | `/api/batches/{id}/export` | 自包含导出 |
| POST | `/api/recompute` | 从导出载荷独立重算全部派生指标 |
| POST | `/api/groups` | 建组（3–8 成员 + 锚点 + 参数，重复成员→422） |
| GET | `/api/groups` / `/api/groups/{id}` | 组列表 / 组详情（实时预览 + 各快照过期信息） |
| PUT | `/api/groups/{id}/members` | 改成员（`expected_revision` 乐观锁，冲突→409） |
| PUT | `/api/groups/{id}/params` | 改锚点/参数（同样乐观锁） |
| POST | `/api/groups/{id}/snapshots` | 固化不可变快照（成员+锚点版本+参数+排除+结果，双哈希） |
| GET | `/api/groups/{id}/snapshots/{v}` | 回放快照（自动重算校验 + staleness） |
| GET | `/api/groups/{id}/snapshots/{v}/export` | 快照自包含导出（含完整 samples/事件史/合成声明） |
| POST | `/api/group-replay` | 仅从 spec 离线独立重算（不访问数据库） |

## 验收对应

1. **三条批次一爆对齐**：①②生成 A/B/对照 → 批次组视图勾选三批、锚点“一爆开始”→
   固化快照：中位趋势 + Q25/Q75 离散带带成员明细；勾选“叠加成员原始曲线”可切回每条
   原始曲线（含插值菱形与长断档断裂）。
2. **缺锚点/长断档排除**：把某成员一爆事件改到长断档内（如 450 s）或删掉一爆事件后
   重算，该成员被排除并注明原因（缺口跨度/桥接上限），不用插值硬补，聚合只用其余成员。
3. **重复/并发**：同一批次勾选两次建组返回 422；两端同 revision 保存成员，后者收到
   409，先到修改保留、不静默覆盖。
4. **修正与过期**：修正已入组成员的一爆事件后，旧快照照常回放（哈希一致）并对当前组
   显示过期原因，可固化 v2 新快照；两版并存。
5. **可复现**：页面“导出快照 + `/api/group-replay` 重算比对”显示哈希一致；下载的 JSON
   自包含完整原始 samples、事件史、参数与排除记录，刷新页面结果不变。

## 目录

    backend/app/  config.py models.py analysis.py groups.py synth.py schemas.py main.py
    frontend/src/ App.svelte lib/RoastChart.svelte lib/GroupChart.svelte lib/api.js
    tests/        test_analysis.py test_api.py test_groups.py（双后端同一套用例）
