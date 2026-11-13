# 罕见病人群组合计发布核查系统

罕见病研究数据员在公布若干人群组的病例合计前，用本系统核对：把各组人数当作未知
非负整数，仅以**公开总人数、历次已公布合计、本次申请合计**建立整数约束，若任何
**保密组（真实人数 1～4）**在全部可行底表中的人数都相同，则拒绝发布并指出组名。

- 后端：FastAPI + SQLite（持久化底表与发布历史，重启沿用）
- 核查引擎：SciPy `milp`（HiGHS 混合整数线性规划），对每个保密组双向探测可行解
- 并发：进程内锁把「核查 + 落库」串行化，并发申请按**实际发布次序**累计核查，
  不会出现各自通过、合起来泄密
- 失败语义：被拒绝、求解无定论、保存失败时均不发布本次合计，历史不变

## 目录结构

```
app/
  main.py        FastAPI 路由、CSV 校验、发布串行化与错误语义
  solver.py      SciPy MILP 保密核查（可行底表枚举判定）
  database.py    SQLite 持久化（底表 / 发布历史 / 进程锁）
  static/index.html  单页网页（上传、勾选申请、结果与历史）
sample/sample_groups.csv   可演示组合泄露的样例底表（5 组，总人数 35）
scripts/demo.py  自动演示差分攻击被拦截的脚本（仅标准库）
tests/test_app.py  16 项端到端/单元测试
Dockerfile, docker-compose.yml, requirements.txt
```

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据库位于命名卷 `rare_data`（容器内 `/data/rare.db`），重启容器后底表与
发布历史保持不变。样例 CSV 只读挂载在容器内 `/sample/sample_groups.csv`。

## 本地运行（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
APP_DB_PATH=./data/rare.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 底表 CSV 规则

- 首行表头必须为 `group,count`（UTF-8，带 BOM 也可）；
- 2～16 个**唯一**组名；人数为 0～1000 的整数；
- 首次上传后底表固定，再次上传返回 409；
- 真实人数 1～4 的组自动标记为保密组；总人数视为公开信息；
- 演示/测试可用 `POST /api/reset` 清空底表与历史后重新上传。

## 核查原理

设各组未知人数为非负整数 x₁…xₙ，每次申请时建立等式约束：

1. `Σxᵢ = T`（公开总人数）；
2. 对每条发布历史 `S → a`：`Σ_{i∈S} xᵢ = a`；
3. 本次申请 `S' → a'` 临时加入同一约束体系。

对每个保密组分别求解两个 MILP：`xᵢ ≥ 真实值+1` 与 `xᵢ ≤ 真实值−1`。
两个方向都不存在可行整数解时，说明该组人数在**全部可行底表**中被唯一确定，
即本次发布会泄露其人数 → 拒绝并返回组名。求解器超时或返回不确定状态时，
按「无定论」处理（HTTP 409，`X-Disclosure-Result: inconclusive`），同样不发布。

## 网页演示：组合泄露（差分攻击）

样例底表（保密组为「溶酶体贮积症」3 人、「肌萎缩侧索硬化」2 人）：

| 组 | 人数 |
| --- | --- |
| 肺动脉高压 | 12 |
| 溶酶体贮积症 | **3（保密）** |
| 遗传性血管性水肿 | 7 |
| 肌萎缩侧索硬化 | **2（保密）** |
| 遗传性共济失调 | 11 |

1. 勾选「肺动脉高压、溶酶体贮积症、遗传性血管性水肿」申请 → 合计 22，
   约束不足，**通过**并进入发布历史；
2. 再勾选「肺动脉高压、遗传性血管性水肿」申请 → 合计 19。
   两条合计联立相减（22−19=3），「溶酶体贮积症」在所有可行底表中都只能是 3 人，
   **被拒绝**并明确指出组名，历史保持 1 条不变。

命令行自动演示（服务启动后）：

```bash
BASE_URL=http://127.0.0.1:8000 python scripts/demo.py
```

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 底表（含保密标记）与全部发布历史 |
| POST | `/api/upload` | multipart 上传 CSV，首次上传固定底表；重复上传 409 |
| POST | `/api/publish` |  body `{"groups": [...]}`；通过则入库，泄露 422，无定论 409 |
| POST | `/api/reset` | 演示用：清空底表与历史 |

发布被拒绝时响应头携带 `X-Disclosure-Result: rejected` 与百分号编码的
`X-Exposed-Groups`；无定论时携带 `X-Disclosure-Result: inconclusive`。

## 测试

```bash
pip install pytest httpx
pytest -q
```

覆盖：CSV 校验与边界、底表固定、单组/差分组合泄露识别、拒绝后历史不变、
并发申请串行累计（重复运行稳定）、求解无定论不发布、重启持久化，
并包含与穷举可行域交叉验证一致的 MILP 单元测试。
