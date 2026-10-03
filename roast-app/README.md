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

### 批次组（3–8 批）—— 明确锚点、公共实测支持、可版本化快照
负责人可建立 3–8 个批次的组，选择**明确的事件锚点**（回温点/一爆开始/一爆结束/出锅）
与分析参数（RoR 窗口、插值桥接上限、聚合网格步长、锚点实测容差）。

- **聚合口径（严格公共支持）**：各成员按其锚点事件时刻对齐（τ=0）。中位趋势与
  q1–q3 离散带只在某个锚点相对网格点上**每一个纳入成员都有非插值的原始实测样本**
  （网格步长一半以内）时才输出；任一成员缺实测支持的区间直接留空，**不插值、不外推、
  不把插值点计入聚合**。离散带是成员横截面分布，**不是置信区间，也不是统计检验**。
- **排除写明原因，不硬补**：成员缺锚点事件 → `missing_anchor`；锚点 ±容差秒内没有
  实测豆温（锚点落在长断档内）→ `anchor_in_long_gap`，并附最近实测点距离与跨接缺口
  时长。被排除成员的原始曲线仍保留可查。
- **组快照不可变、可版本化**：发布快照固化成员集合、每个成员锚点的**事件行版本**
  （按 event id 钉住，而非只记时间戳）、排除记录、参数、全部原始 samples 与事件历史。
  之后改成员/参数，或对成员的关键事件做人工修正，**旧快照原样可回放**；当前组被标为
  **过期**（列出原因），可再发布新快照版本。
- **并发与重复保护**：`(group_id, batch_id)` 唯一约束 + 提交前去重，重复添加同一批次
  返回 409 且不产生重复成员；所有编辑/发布都带 `base_revision` 乐观锁，两端基于旧
  修订号修改时后者收到 409，绝不静默覆盖。
- **同一份快照重建一切**：刷新、`…/snapshots/{v}` 回放、`…/export` 导出与
  `/api/groups/recompute` 独立重算都从同一冻结文档出发，复现同一成员集合、参数、
  排除记录与字节级一致的聚合结果。

### 本地确定性对照批次 —— 合成来源贯穿始终
点 **② 生成本地对照批次 C**（`POST /api/seed/control`，固定种子 `20260920`）即可在
本地确定性地生成带完整 samples 与关键事件的第三批，使两个既有示例批次无需导入任何
其他题产物就能组成 ≥3 个完整批次。该批次与两条示例批次一样：

- 永久标明 `data_origin=local_synthetic_control`、`is_local_synthetic=true`、
  生成器种子与版本号；该标记贯穿**组快照、图例、导出 JSON 与独立重算**；
- 含它的任何结果都带醒目声明：**本地合成演示，不构成真实稳定性结论**，不得伪装为
  来自真实烘焙机的数据或上传数据（系统根本没有机器连接或上传通道）。

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
再点 **② 生成本地对照批次 C**（固定种子、完全可复现），切到「批次组」视图即可把
A/B/C 三批按一爆对齐审阅中位趋势、离散带与成员明细。

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
| GET | `/api/batches` | 批次列表（含合成来源字段） |
| POST | `/api/seed` | 生成两个合成批次 |
| POST | `/api/seed/control` | 生成**本地确定性对照批次 C**（固定种子） |
| GET | `/api/batches/{id}/series?window_s&display_smooth_s&max_gap_fill_s` | 曲线+RoR+指标 |
| GET/POST | `/api/batches/{id}/events[?include_history=true]` | 事件列表/人工修正（只追加） |
| GET | `/api/compare?a=&b=` | 双批次叠加（含非因果声明） |
| GET | `/api/batches/{id}/export` | 单批次自包含导出 |
| POST | `/api/recompute` | 从单批次导出载荷独立重算 |
| POST/GET | `/api/groups` | 建组（3–8 成员、明确锚点与参数；重复成员 409）/ 列组 |
| GET/PATCH | `/api/groups/{id}` | 当前草稿分析 / 编辑（`base_revision` 乐观锁，过期 409） |
| POST/GET | `/api/groups/{id}/snapshots` | 发布不可变版本 / 列版本（含过期判定） |
| GET | `/api/groups/{id}/snapshots/{v}` | 回放冻结报告（独立重算比对 + 过期原因） |
| GET | `/api/groups/{id}/snapshots/{v}/export` | 快照自包含导出（含全部 samples/事件） |
| POST | `/api/groups/recompute` | 从快照文档完全离线重算（与冻结结果字节级一致） |

## 目录

    backend/app/  config.py models.py analysis.py groups.py synth.py schemas.py main.py
    frontend/src/ App.svelte lib/RoastChart.svelte lib/GroupView.svelte
                  lib/GroupChart.svelte lib/RawMemberCurve.svelte lib/api.js
    tests/        test_analysis.py test_api.py test_groups.py（双后端同一套用例）
