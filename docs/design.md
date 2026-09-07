# Ascend NPU CI 失败监控与报告工具 — 设计文档

- 版本: v0.1（草案）
- 日期: 2026-09-03
- 状态: 待评审

## 1. 背景与目标

vLLM 的 Buildkite CI（对外展示于 https://ci.vllm.ai ）中有一个 **Ascend NPU Test** 任务，用于验证 vLLM 主仓 PR 与 vllm-ascend 的兼容性。该任务失败时，需要人工去 CI 页面翻找日志、定位兼容性相关输出，效率低且容易遗漏。

本软件目标是**自动化**这一流程：

1. 每天定时获取 CI 中 `Ascend NPU Test` 任务的执行结果；
2. 发现失败任务时，拉取失败日志，提取其中 `vllm PR compatibility with vllm-ascend` 相关的日志段落；
3. **仅当段落中确认存在 break 时**，整理成表格（含 PR 当前状态），生成以日期命名的报告文档。

## 2. 需求分析

### 2.1 功能需求

| 编号 | 需求 | 说明 |
| --- | --- | --- |
| FR-1 | 定时拉取 CI 结果 | 每天固定时间（默认，可配置）获取最近 24h 的 `Ascend NPU Test` job 状态 |
| FR-2 | 失败日志获取 | 对 state 为 `failed` / `soft_failed` / `timed_out` 的 job，下载其完整日志 |
| FR-3 | 日志段落提取 | 定位日志中 `vllm PR compatibility with vllm-ascend` 起始的段落，截取到下一个段落标记或日志结尾 |
| FR-4 | break 内容提取与**收录过滤** | 解析段落中的 `Result: BREAKS FOUND` 与逐条 break finding；**仅存在 break 的记录才进入报告表格**，失败但无 break（如 infra 故障、`Result: PASS/REVIEW`、分析器错误）只计入统计，不进表格 |
| FR-5 | PR 状态获取 | 由 `commit_sha` 经 GitHub API 反查关联 PR，获取当前状态（Open / Merged / Closed）、标题、作者，写入表格 |
| FR-6 | 报告生成 | 生成 Markdown 报告，**表格仅含存在 break 的条目**并带 PR 状态，文件名以日期命名（如 `report-2026-09-03.md`） |
| FR-7 | 幂等与去重 | 同一 job 同一天重复运行不产生重复条目；跨天运行可回补漏报 |
| FR-8 | 运行通知（可选） | 有新 break 时输出退出码/摘要，便于接入 IM webhook（后续扩展） |

### 2.2 非功能需求

- **可靠性**：网络/API 失败需指数退避重试；单次运行失败不影响下次调度。
- **幂等**：以 Buildkite job id 为唯一键做去重。
- **可配置**：org/pipeline/job 名、日志段落标记、break 词表、输出目录均可配置。
- **零侵入**：只读访问，不写任何 CI 侧数据。

## 3. 数据源调研与选型

### 3.1 调研结论

- ci.vllm.ai 是 [vllm-project/vllm-dashboard](https://github.com/vllm-project/vllm-dashboard)（Next.js 部署在 Vercel）。其数据链路为：
  - **Databricks SQL Warehouse**（Fivetran 同步的 `vllm_data_warehouse.buildkite.*` 表：build / build_job / pipeline，dashboard 服务端持有 `DATABRICKS_TOKEN`）
  - **Buildkite REST/GraphQL API**（失败 job 生命周期、队列深度，需要 `BUILDKITE_API_TOKEN`）
  - **Postgres/Supabase**（队列与 GPU 采样）
- dashboard 对外暴露的 **`/api/jobs`、`/api/jobs/runs`** 等 Next.js 路由本身不校验用户身份（服务端自行持有凭证），公开可调用——实测确认。这是"先查 job 失败记录、再拉日志"方案的直接依据。
- `Ascend NPU Test` 是 vLLM 主 pipeline 中的一个 Buildkite step：
  - 定义：`.buildkite/hardware_tests/ascend_npu.yaml`，`label: "Ascend NPU Test"`，`key: ascend-npu-test`，**`soft_fail: true`**（失败时 job 状态为 `soft_failed`，必须纳入失败判定）
  - 执行脚本：`.buildkite/scripts/hardware_ci/run-npu-test.sh`（构建 Ascend NPU 镜像，clone vllm-ascend，运行兼容性/推理 sanity check，并输出兼容性日志）

### 3.2 选型对比（已按用户方案修订：Dashboard 索引 → Buildkite 日志）

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| A. **Dashboard `/api/jobs/runs` 查失败索引 + Buildkite API 拉日志** | 索引侧零 token（已实测线上 200）；dashboard 仓库同步了全部 job 运行记录，天然覆盖"并非每个 build 都有该 job"的动态 pipeline 场景；`job_id`/`web_url`/`state`/`commit_sha` 字段齐全，与 Buildkite 日志端点直接衔接 | 依赖 dashboard 部署可用性；Fivetran→Databricks 同步有延迟；`soft_failed` 状态被其查询过滤（见 5.1 交叉校验） | **采用（索引主数据源）** |
| B. Buildkite REST API 按 build 扫描 | 官方接口、状态实时 | 每日全量 build 详情分页扫描；动态生成的 job 是否存在需要自行枚举判断；token 必需 | 降级为**日志抓取 + soft_failed 交叉校验** |
| C. 抓取 ci.vllm.ai 页面 SSR/HTML | — | 脆弱，无必要（API 直接可用） | 不采用 |

**线上实测记录（2026-09-04）**：`GET https://ci.vllm.ai/api/jobs/runs?jobName=Ascend%20NPU%20Test&pipeline=CI&branch=main&startDate=2026-08-28&endDate=2026-09-04` → HTTP 200，返回 81 条 `failed` 运行，字段：`job_id, web_url, state, started_at, finished_at, duration_secs, commit_sha, build_created_at`；`web_url` 形如 `https://buildkite.com/vllm/ci/builds/85905?jid=<job_id>`，build 号可直接解析。

### 3.3 关键 Buildkite API

| 用途 | 端点 |
| --- | --- |
| 列出 build | `GET /v2/organizations/{org}/pipelines/{pipeline}/builds?branch=main&state=failed&created_from=...` |
| build 详情（含 jobs 列表） | `GET /v2/organizations/{org}/pipelines/{pipeline}/builds/{number}` |
| job 日志 | `GET /v2/organizations/{org}/pipelines/{pipeline}/builds/{number}/jobs/{job_id}/log` |

基址 `https://api.buildkite.com`，鉴权 `Authorization: Bearer $BUILDKITE_API_TOKEN`。默认 `org=vllm`、`pipeline=ci`（已通过真实 build 链接 https://buildkite.com/vllm/ci/builds/87028 确认，可配置，vllm-ascend 等其它 pipeline 复用同一套逻辑）。

> 说明：Buildkite API 支持分页（`per_page`，`page`），job log 端点返回 `content`（纯文本）及分页游标，需循环取全。

## 4. 总体架构

```
┌────────────┐   ┌─────────────────────────┐   ┌──────────────────┐   ┌─────────────┐   ┌──────────────┐
│  Scheduler │──▶│  Collector（失败索引）  │──▶│  Log Fetcher     │──▶│  Analyzer   │──▶│  Reporter    │
│ (cron/计划 │   │ ① Dashboard /api/jobs/  │   │ Buildkite API    │   │ 段落提取 +  │   │ Markdown 表格│
│  任务等)   │   │    runs 查 job 运行记录 │   │ 按build号+job_id │   │ 结构化解析  │   │ 按日期落盘   │
└────────────┘   │ ② Buildkite builds 扫描 │   │  抓日志+重试限速 │   └──────┬──────┘   └──────┬───────┘
                 │    交叉校验 soft_failed │   └────────┬─────────┘          │                 │
                 └───────┬─────────────────┘            │                    │                 │
                         │                              │                    │                 │
                         ▼                              ▼                    ▼                 ▼
                    ┌─────────────────────────────────────────────────────────────────────┐
                    │        Store（本地状态：SQLite/JSON，记录已处理 job、原始日志快照）   │
                    └─────────────────────────────────────────────────────────────────────┘

外部依赖：ci.vllm.ai（无需鉴权，仅读失败索引）  |  api.buildkite.com（需 token，读日志+校验）
```

- **单进程 CLI 工具** + 外部调度（Windows 任务计划 / crontab / GitHub Actions schedule），不内置常驻服务，降低部署成本。
- 本地状态库用于幂等去重与日志快照留档（便于事后审计与重新解析）。

## 5. 模块设计

### 5.1 Collector（失败索引）

输入：配置（dashboard URL、jobName、pipeline、branch、时间窗）。
**设计动机**：`Ascend NPU Test` 并非每个 build 都有（pipeline 由 ci_config 动态生成），因此**不按 build 枚举任务**，而是以 job 为查询主体——先从 Dashboard 查到该 job 的全部失败运行记录，再定向拉日志。两路合并：

1. **主路——Dashboard `/api/jobs/runs`**（无需鉴权，已实测）：
   `GET {dashboard}/api/jobs/runs?jobName=Ascend NPU Test&pipeline=CI&branch=main&startDate={d}&endDate={d+1}`
   注意参数语义：`pipeline=CI` 是 dashboard 里的 pipeline **名称**（非 Buildkite slug）；`startDate/endDate` 为 `YYYY-MM-DD`，endDate 为闭区间按天取整；返回每条运行含 `job_id / web_url / state / started_at / finished_at / duration_secs / commit_sha / build_created_at`。
2. **辅路——Buildkite builds 扫描交叉校验**（需 token，可用 `state=failed,failing` 过滤缩小范围）：
   - 补 `soft_failed`：dashboard 的 runs 查询状态过滤为 `IN ('passed','failed','failing','broken','timed_out')`，**不包含 `soft_failed`**，而 Ascend job 是 `soft_fail: true`，可能产生 soft_failed 状态；
   - 补**同步延迟**：Fivetran→Databricks 仓库入库有分钟级~小时级延迟，当天的失败可能查不到，Buildkite 实时侧兜底。
3. **合并去重**：以 `job_id`（Buildkite 全局 UUID）为主键合并两路结果，`build_number` 从 `web_url`（`/builds/{number}?jid=...`）解析；辅路记录来源标记 `source ∈ {dashboard, buildkite, both}` 写入审计。
4. **同 PR 去重（按需求：多次执行只看最新一次）**，两层：
   - **Pass 1（PR 解析前，零成本）**：同一 `commit_sha` 的多次运行（如同一 build 的 rerun / 多 job）只保留 `build_number` 最新的一次，避免无谓的日志拉取；
   - **Pass 2（PR 解析后）**：不同 commit 解析到同一 PR（force-push 换 sha、merge 后重跑）时，按 PR 分组只保留最新一次；被取代的旧记录不进主表/附录，仅在日志记录 `superseded`；
   - PR 反查失败的记录不参与 Pass 2（无法判定同 PR，各自独立保留）；
   - 报告统计行同时展示原始失败数与去重后分析数。
5. 失败判定：`state ∈ {failed, soft_failed, timed_out, broken, failing}`。
6. 输出 `FailedJob` 列表：

```python
@dataclass
class FailedJob:
    job_id: str          # Buildkite 全局唯一 job id，去重主键
    build_number: int    # 从 web_url 解析
    pipeline: str
    branch: str
    commit_sha: str | None    # dashboard 直接提供，同时用于 GitHub 反查 PR
    pr_number: str | None
    pr_title: str | None
    pr_url: str | None
    pr_state: str | None      # open / merged / closed（GitHub API 实时状态）
    pr_author: str | None
    web_url: str              # Buildkite job 页面链接（dashboard 直接提供）
    state: str
    source: str               # dashboard / buildkite / both
    finished_at: datetime
```

PR 解析与状态获取（GitHub API；认证后限速 1000–5000 次/小时，匿名 60 次/小时——**配置方式见 7.1**）：

1. **主路**：`GET https://api.github.com/repos/vllm-project/vllm/commits/{commit_sha}/pulls`（Accept: `application/vnd.github+json`）——由 dashboard 返回的 `commit_sha` 直接反查该 commit 关联的 PR，返回含 `number/title/state/merged/html_url/user.login`；`state=open` 时 PR 状态记 `Open`，`merged=true` 记 `Merged`，其余记 `Closed`；
2. **兜底**：GitHub 反查失败（如 force-push 后 sha 悬空）时回退 build message 中的 `(#1234)` / 分支名 `pull-request/NNN` 解析出 PR 号，再调 `GET /repos/vllm-project/vllm/pulls/{number}` 取状态；
3. PR 状态取**查询时刻的实时值**——即"该 PR 现在是开着、已合入还是已关闭"，用于判断 break 的处置优先级（已合入的 PR 带 break 优先级最高）。解析失败则留空，不影响表格其它列。

### 5.2 Log Fetcher（日志抓取）

- 对每个 `FailedJob` 调 job log 端点，循环分页取完整 `content`；
- 指数退避重试（初始 2s，最多 5 次），429 读取 `Retry-After`；
- 原始日志按 `store/logs/{date}/{build_number}_{job_id}.log` 落盘快照；
- 去重：`Store` 中已存在的 `job_id` 直接跳过下载（支持 `--refetch` 强制重取）。

### 5.3 Analyzer（日志分析）

#### 5.3.1 已核实的日志实际格式（源码级确认，非猜测）

`Ascend NPU Test` job 内部执行链：`run-npu-test.sh` 构建镜像后在容器内跑
`pytest -v -s tests/e2e/vllm_interface/`，其中
`test_vllm_pr_interface_compatibility.py`（vllm-ascend 仓）调用
`vllm_interface_contracts/range_analysis.py` 完成兼容性分析并打印。

**⚠ 2026-09-04 真实日志实测修正**（与源码推导的差异，Analyzer 已适配）：

1. Buildkite 返回的每行日志带时间戳注解前缀：`\x1b_bk;t=<epoch_ms>\x07`（OSC 序列，BEL 结尾）——
   Analyzer 先清洗 ANSI（CSI + OSC + 孤立 BEL）再做行锚定正则匹配，否则 `+++`/`**Result:`/`### N.` 全部匹配失败；
2. finding 标题的 relation 与 contract 之间**有空格**：真实格式为
   `### 1. P1 direct_call / call_target_presence`（`/` 两边可有可无空格），正则用 `[^/\s]+\s*/\s*\S+`；
3. Buildkite REST 的 `state` 查询参数**只接受单值**（`state=failed,failing` 返回 422），交叉校验按 state 逐个请求；
4. GitHub 匿名限速实测 60 req/h，一个窗口的失败数即可打满（71 失败 > 60）——
   PR 反查结果持久缓存到 store 的 `pr_cache` 表（sha → PR 快照），重跑/同 PR 重复失败零配额消耗；
   限速 403 时明确告警并提示配置 `GITHUB_PR_TOKEN`（见 7.1），同一运行内短路后续调用。

**输出结构**（`**Result: PASS**` / `**Result: REVIEW**` / `**Result: BREAKS FOUND**` 三态）：

```
+++ vLLM PR compatibility inputs
vllm_pr_base_sha=<old_sha>
vllm_pr_head_sha=<new_sha>
vllm_ascend_sha=<ascend_sha>
+++ vLLM PR compatibility timings            # 仅分析有 stderr 时打印
[vllm-interface] <label>: <elapsed>s
...
+++ vLLM PR compatibility result for vllm-ascend
# vLLM PR Compatibility with vllm-ascend

**Result: BREAKS FOUND**                     # 三种取值：BREAKS FOUND / REVIEW / PASS

- This PR: `<old_sha>` -> `<new_sha>`
- vllm-ascend revision: `<ascend_sha>`
- Breaks introduced by this PR: <N>
- Distinct vLLM API changes causing breaks: <M>
- Items for review: <R>
- Distinct vLLM API changes for review: <S>

## Breaks introduced by this PR

### 1. <priority> <relation>/<contract_kind>
- vLLM API changed by this PR: `<file>:<Owner.name>`
- Affected vllm-ascend code: `<file>:<line>`
- vllm-ascend override path: `a -> b -> c`        # 可选
- Compatibility impact: <一句话影响描述>

## Items for review                              # review 项与 break 条目同构，可并存

### 1. <priority> <relation>/<contract_kind>
- vLLM API changed by this PR: `<file>:<Owner.name>`
- Affected vllm-ascend code: `<file>:<line>`
- Review reason: <为什么需要人工复核>              # review 条目特有字段
- Compatibility impact: <一句话影响描述>
...

FAILED ... - Failed: this vLLM PR introduces an interface break in vllm-ascend
```

要点：

- `+++ ` 前缀是 **Buildkite 日志折叠语法**，在 ci.vllm.ai 上呈现为可折叠段落——即用户所说的 "vllm PR compatibility with vllm-ascend" 段落，实际标记是 `+++ vLLM PR compatibility ...` 三段。
- result 段落内容本身就是一份结构化 Markdown 报告，`Result` 行给出结论分类，`### N.` 为逐条 break finding，每条自带 priority/relation/contract_kind/影响文件/impact 字段。
- 分析器 returncode==1 时 pytest 失败，日志末尾出现 `this vLLM PR introduces an interface break in vllm-ascend`；returncode 为其它非零值时为 `interface analysis failed with exit code <n>`（分析器自身故障，不是兼容性 break）。

#### 5.3.2 解析设计

基于上述格式做**结构化解析**，通用正则仅作兜底：

1. **段落提取**：按行扫描，匹配正则 `^\+\+\+ vLLM PR compatibility (.+)$`（可配置），切分为 inputs / timings / result 三段；未命中时记录 `section_not_found` 警告并回退为全文匹配。
2. **结果分类**：在 result 段中匹配 `^\*\*Result: (BREAKS FOUND|REVIEW|PASS)\*\*$`，作为该 build 的结论字段；额外识别日志中的 pytest 失败行（`interface break` vs `analysis failed`）区分"真 break"与"分析器故障"。
3. **break 条目提取**：用固定字段正则逐条解析 `### N. <priority> <relation>/<contract_kind>` 小节，抽取 `priority`、`relation`、`contract_kind`、`vLLM API`、`Affected vllm-ascend code`、`Compatibility impact`、override path（可选）字段。解析失败的行回退为原始行文本保留。
4. **兜底 break 匹配**：对无法结构化的段落按 `config.break_patterns`（默认 `\bbreak\w*\b`，忽略大小写）匹配，保留前后 N 行上下文。输出结构：

```python
@dataclass
class BreakFinding:
    build_number: int
    job_id: str
    result: str | None        # BREAKS FOUND / REVIEW / PASS / None(未识别)
    index: int | None         # 条目序号（结构化解析成功时）
    priority: str | None
    relation: str | None      # override / direct_call / direct_import / ...
    contract_kind: str | None
    vllm_api: str | None      # vLLM API changed by this PR
    affected_code: str | None # Affected vllm-ascend code
    impact: str | None        # Compatibility impact
    raw_text: str             # 原始片段（结构化失败时的整段/上下文）
```

5. **收录判定（进表格的硬性门槛）**：一条失败记录进入报告主表当且仅当 `result == "BREAKS FOUND"` 且解析出 ≥1 条 break finding。其它情况（PASS/REVIEW、分析器错误、section_not_found）只统计不进主表。该判定在 Analyzer 输出中固化为 `included_in_table: bool` 字段，Reporter 不再做二次推断。

### 5.4 Reporter（报告生成）

- 输出目录：`reports/`（相对路径在加载时锚定到工具目录，与运行 cwd 无关）；
- **文件名以时间窗命名**：`report-{window}.md`，`{window}` 为 UTC 窗口标识（如 `report-20260903T0000Z-20260904T0000Z.md`）。同一窗口重跑覆盖同名文件（数据补齐后的最新版），不同窗口生成新文件、互不覆盖；
- 报告标题含时间窗（`# Ascend NPU Test 失败报告 — 2026-09-03T00:00Z ~ 2026-09-04T00:00Z`），正文首节给出双时区窗口详情；
- 报告结构：

```markdown
# Ascend NPU Test 失败报告 — 2026-09-03T00:00Z ~ 2026-09-04T00:00Z

查询时间窗：2026-09-03 00:00Z ~ 2026-09-04 00:00Z（UTC） / 2026-09-03 08:00 ~ 2026-09-04 08:00（北京时间，UTC+8）

统计：过去 24h 共发现 N 次 Ascend NPU Test 失败运行，其中 K 次确认存在 break（已收录表格）。

## Break 汇总表（仅收录确认存在 break 的记录）

| Build | PR | PR 状态 | PR 标题 | Breaks | Priority | vLLM API 变更 | vllm-ascend 受影响代码 | 影响说明 | Review reason | 日志链接 |
| ----- | -- | ------- | ------- | ------ | -------- | ------------- | ---------------------- | -------- | ------------- | -------- |
| #12345 | [#22331](https://github.com/vllm-project/vllm/pull/22331) | **Merged** | [Bugfix] ... | 2 | P0 | `vllm/xxx.py:Owner.method` | `vllm_ascend/xxx.py:123` | This PR changes the return contract ... | The override does not accept the new optional parameter `skip_rows` ... | [job](https://buildkite.com/vllm/ci/builds/12345#...) |

- **收录规则（硬性）**：只有 Analyzer 判定 `Result: BREAKS FOUND` 且成功解析出至少一条 break finding 的记录才进入本表；失败但无 break（`Result: PASS/REVIEW`、分析器错误 `interface analysis failed`、`section_not_found`）一律不进表，仅计入统计与附录。
- **Review reason 列**：日志 result 段的 `## Items for review` 条目与 break 条目同构、可与 break 并存（实测 BREAKS FOUND 日志也常带 review 项）。解析时按 `##` 父段落拆分为 breaks 与 review items 两类，本列展示该记录 review 条目的 `Review reason:` 内容（去重、超 110 字符截断）；无 review 项时为 `—`。附录 A 同样带此列（REVIEW 记录在此可读）。统计行额外给出"待复核项 N 条"。
- **PR 状态列**：取 GitHub 查询时刻的实时状态——`Open`（待合入，PR 作者可修）/ `Merged`（已合入 main，break 已进入主干，处置优先级最高）/ `Closed`（已放弃）。PR 状态排序建议：Merged > Open > Closed。
- 同一 PR 多个失败 build 时合并为一行（Breaks 取条目并集，Build 列列出全部关联 build 号），避免表格被同一 PR 刷屏。

## 附录 A：失败但无 break 的记录（不进主表，仅备查）

| Build | PR | PR 状态 | Job 状态 | 原因 | 链接 |
| ----- | -- | ------- | -------- | ---- | ---- |
| #12350 | #22340 | Open | soft_failed | Result: REVIEW（1 项待复核） | [job](https://buildkite.com/vllm/ci/builds/12350#...) |

## 附录 B：解析异常（结构化失败/section_not_found/unparseable_url，需人工核查）

### Build #12345 / PR #22331

```
<line 1023> ... compatibility check ...
<line 1024> ... result: broken with vllm-ascend v0.9.x ...
```
```

- 主表列直接来自 5.3 节结构化解析出的真实字段（PR 状态/Breaks/Priority/impact 等），无需人工归纳；
- 结构化解析失败或 review 类条目进附录 B/A，保留上下文原文；
- 无 break 的运行生成仅含统计行的空报告（可选关闭），保证"每天有产出"。

### 5.5 Store（状态存储）

- v1 用 SQLite 单文件（`store/state.db`）：

| 表 | 字段 | 用途 |
| --- | --- | --- |
| `processed_jobs` | `job_id PK, build_number, state, first_seen, report_date` | 幂等去重 |
| `runs` | `run_id, started_at, finished_at, builds_scanned, failures, findings` | 运行审计 |

- 原始日志快照按目录落盘，与 DB 解耦，便于重新分析。

## 6. 调度与运行方式

| 部署形态 | 做法 | 适用 |
| --- | --- | --- |
| **GitHub Actions schedule（推荐）** | `cron` 触发，报告 commit 回仓库，见 6.1 | 无常驻机器时 |
| 本机/服务器 | Windows 任务计划程序 / crontab：`0 9 * * * ascend-ci-report run`，报告直接落本地 `reports/` | 日常使用 |
| 手动 | `ascend-ci-report run [--date ...] [--backfill N]` | 补数、调试 |

- 时间窗与调度周期解耦：每次运行扫描"上次成功运行时间 → 现在"（首次默认 24h），避免漏报；
- 失败自动重试一次；连续失败通过非零退出码暴露给调度器。

### 6.1 GitHub Actions 部署设计（报告存储方案）

报告存放位置对比：

| 方案 | 存储位置 | 可浏览性 | 保留期 | 结论 |
| --- | --- | --- | --- | --- |
| **commit 回仓库**（`reports/report-{window}.md`，窗口键控命名） | 仓库工作树，随 git 版本化 | 直接在 GitHub 网页浏览/搜索/对比 diff | 永久 | **采用（主方案）** |
| GitHub Pages | 独立静态站点 | 浏览器直达 | 随 Pages | 可选增强（报告目录够用，v1 不做） |

commit 回仓库要点：

1. **权限**：workflow 设 `permissions: contents: write`，用内置 `GITHUB_TOKEN` 即可 push 回本仓（无需 PAT）；内置 token 的 push **不会触发其它 workflow**，天然避免循环触发；
2. **空提交抑制**：`git diff --cached --quiet || git commit ...`，无 break 时不产生空提交；
3. **同窗口重跑**：直接覆盖同名 `report-{window}.md` 后新提交（该文件的 git diff 即"窗口内数据补齐后的变化"），不做 amend；不同窗口各为新文件互不覆盖；
4. **报告索引**：可选维护 `reports/README.md` 索引表（日期 × break 数 × 链接），每次运行追加更新，方便从仓库首页点进最新报告；
5. **历史追溯**：`reports/` 目录天然构成时间线，`git log reports/` 或 Blame 即可回看任何一天的失败情况。

workflow 参考实现：

```yaml
name: ascend-ci-report
on:
  schedule:
    - cron: "0 1 * * *"      # UTC 01:00 = 北京时间 09:00
  workflow_dispatch:          # 手动触发/补数入口，支持配置时间窗
    inputs:
      date:
        description: '查询日期（窗口起始日，UTC，YYYY-MM-DD）。留空 = 按增量调度'
        required: false
        default: ''
      backfill_days:
        description: '回溯天数 N（从现在向前回溯 N 天）。与"查询日期"二选一'
        required: false
        default: ''
      refetch:
        description: '忽略已处理记录，重新拉取日志并覆盖分析'
        type: boolean
        required: false
        default: false
      merge:
        description: '汇总已有日报（搭配 backfill_days 使用，缺失天自动补充）'
        type: boolean
        required: false
        default: false
permissions:
  contents: write
defaults:
  run:
    working-directory: tools/ascend-npu-report   # 工具自包含目录（第 10 节）
jobs:
  report:
    runs-on: linux-aarch64-a2b3-8   # 自托管 ARM64 runner
    timeout-minutes: 15
    env:                                        # job 级：run 步骤的进程可读
      BUILDKITE_API_TOKEN: ${{ secrets.BUILDKITE_API_TOKEN }}
      # 认证 GitHub API：Actions 内置 token 自动注入（1000 req/h），无需创建任何 secret；
      # 本地运行时设 GITHUB_PR_TOKEN 为个人 PAT（fine-grained，Public repos 只读，无需 scope）→ 5000 req/h
      GITHUB_PR_TOKEN: ${{ github.token }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install .
      - name: Run report
        run: |
          ARGS=""
          if [ -n "${{ inputs.date }}" ]; then
            ARGS="$ARGS --date ${{ inputs.date }}"
          elif [ -n "${{ inputs.backfill_days }}" ]; then
            ARGS="$ARGS --backfill ${{ inputs.backfill_days }}"
          fi
          if [ "${{ inputs.refetch }}" = "true" ]; then
            ARGS="$ARGS --refetch"
          fi
          if [ "${{ inputs.merge }}" = "true" ]; then
            ARGS="$ARGS --merge"
          fi
          ascend-ci-report run $ARGS
      - name: Commit reports
        working-directory: ${{ github.workspace }}   # git 操作回仓库根
        run: |
          git config user.name "ascend-report-bot"
          git config user.email "bot@users.noreply.github.com"
          git add tools/ascend-npu-report/reports/
          git diff --cached --quiet || git commit -m "ascend report: $(date -u +%F)"
          git push
```

注意：Actions 的 `schedule` 是尽力而为（高峰期可能延迟十几分钟到更久），对本任务（日粒度）无影响；私有仓每月有免费 runner 分钟额度，日跑一次（分钟级任务）远在额度内。

### 6.2 手动触发与时间窗配置（workflow_dispatch）

Actions 页面 → ascend-ci-report → Run workflow，三个输入（均留空 = 正常每日增量，与定时任务行为一致）：

| 输入 | CLI 映射 | 语义 |
| --- | --- | --- |
| `date` | `--date` | 查询窗口起始日（**UTC**，`YYYY-MM-DD`），窗口 = 该日 00:00Z 起 24h。想看北京时间 D 日全天应填 D-1 日（北京 08:00 = UTC 前日 24:00） |
| `backfill_days` | `--backfill` | 从现在向前回溯 N 天（与 date 二选一，**date 优先**） |
| `refetch` | `--refetch` | 勾选后忽略已处理记录，重新拉取窗口内日志并覆盖分析 |

典型场景：

| 场景 | date | backfill_days | refetch | 说明 |
| --- | --- | --- | --- | --- |
| 日常手动跑（补昨天） | 留空 | 留空 | ☐ | 等价定时增量 |
| 补特定日期 | `2026-09-01` | 留空 | ☐ | 查 09-01T00:00Z ~ 09-02T00:00Z 窗口 |
| 补最近 3 天 | 留空 | `3` | ☑ | 生成 3 个窗口文件；已处理 job 需 refetch 才会重算 |
| 只刷新分析（解析逻辑变更后重出报告） | `2026-09-03` | 留空 | ☑ | 已有快照日志重分析，不重拉 |

填写规则与代价：

1. **幂等**：同窗口重跑只覆盖同名报告文件，不产生重复记录；无变化时空提交抑制跳过 commit；
2. **refetch 的代价**：重拉窗口内全部失败 job 日志（约 1~2 分钟/天 + Buildkite API 消耗），且这些 job 重算后仍标记为已处理——常规增量不会重复碰它们，因此 refetch 是"重算"而非"额外处理"；
3. **水位安全**：手动补历史日期不影响增量水位（水位取运行完成时刻，见 5.1）；
4. **格式校验**：date 必须严格 `YYYY-MM-DD`，否则参数校验失败、run 直接报错退出（不会静默跑错窗口）。

### 6.3 汇总模式（--merge）

当需要跨多天汇总 break 报告时，使用 `--merge` 模式。该模式从已有日报的 Break 汇总表中解析并合并数据，避免重复调用 API。

**使用方式：**

```bash
ascend-ci-report run --backfill 7 --merge
```

**执行流程：**

1. 计算日期范围 `[now - N days, now]`
2. 过滤：移除 <= 2026-09-03 的日期（CI 未上线 vllm-interface）
3. 对范围内每个日期：
   - 文件存在 → 直接使用
   - 文件不存在 → 对该天跑完整 pipeline（单天 backfill）
4. 解析所有日报的 Break 汇总表
5. 按 build number 去重 + 按 PR number 去重（同 PR 保留最新 build）
6. 渲染汇总报告，写入 `reports/merged-report-{window}.md`

**输出格式：**

- 文件名前缀 `merged-report-`，window 为整个汇总范围
- 只包含 break 主表，不含附录 A/B
- stats 行："汇总 N 天日报，共 M 条 break 记录（去重后 K 条）"

**限制：**

- `--merge` 与 `--refetch` 互斥
- `--merge` 必须搭配 `--backfill` 或 `--date`
- 2026-09-03 及之前的日期自动跳过

**workflow 触发：**

Actions 页面 → ascend-ci-report → Run workflow，勾选 `merge` 并填写 `backfill_days`。

## 7. 配置设计

`config.yaml`（支持环境变量覆盖敏感项）：

```yaml
dashboard:
  base_url: https://ci.vllm.ai         # 无需鉴权
  runs_endpoint: /api/jobs/runs
  pipeline: CI                          # dashboard 侧的 pipeline 名称（非 Buildkite slug）
  branch: main
buildkite:
  api_token_env: BUILDKITE_API_TOKEN   # 仅用于拉日志 + soft_failed 交叉校验，read_builds scope
  org: vllm
  pipeline_slug: ci
  branch: main
  cross_check: true                    # 开关：是否做 Buildkite 侧扫描校验
job:
  name: "Ascend NPU Test"              # Dashboard jobName 参数（精确匹配）
  step_key: ascend-npu-test            # Buildkite 侧交叉校验时的匹配键
  failure_states: [failed, soft_failed, timed_out, broken, failing]
analysis:
  section_start_patterns:
    - '^\\+\\+\\+ vLLM PR compatibility (?P<section>inputs|timings|result for vllm-ascend)$'
  result_line_pattern: '^\\*\\*Result: (?P<result>BREAKS FOUND|REVIEW|PASS)\\*\\*$'
  finding_header_pattern: '^### (?P<index>\\d+)\\. (?P<priority>\\S+) (?P<relation>\\S+)/(?P<contract>\\S+)$'
  field_patterns:
    vllm_api: '^\\s*- vLLM API changed by this PR: `(?P<value>.+)`$'
    affected_code: '^\\s*- Affected vllm-ascend code: `(?P<value>.+)`$'
    impact: '^\\s*- Compatibility impact: (?P<value>.+)$'
  pytest_fail_patterns:
    real_break: 'this vLLM PR introduces an interface break in vllm-ascend'
    analyzer_error: 'interface analysis failed with exit code'
  break_patterns: ['\\bbreak\\w*\\b']       # 仅作兜底
  context_lines: 2
github:
  token_env: GITHUB_PR_TOKEN           # Actions 内置 token 自动注入；本地用个人 PAT
  repo: vllm-project/vllm
report:
  output_dir: reports
  filename: "report-{date}.md"
  table_inclusion: breaks_only         # 硬性规则：仅存在 break 的记录进主表
  write_empty_report: true
schedule:
  lookback_default_hours: 24
```

### 7.1 敏感信息管理（token 不外泄）

原则：**config.yaml 与代码中永远只有"环境变量名"，绝无 token 值**；真实值只存在于两处——GitHub Actions Secrets（CI 运行时）与本机环境变量/`.env`（本地运行时）。

GitHub Actions 侧：

1. **存放**：`BUILDKITE_API_TOKEN` 配置在仓库 *Settings → Secrets and variables → Actions*，写入后不可再查看（只能替换），静态加密存储；GitHub API 限额通过 `GITHUB_PR_TOKEN` 解决——workflow 里已注入 Actions 内置 token（`${{ github.token }}`，1000 req/h），**无需创建任何 secret**；本地跑批时自行设置个人 PAT 环境变量；
2. **注入**：workflow 中仅以 `${{ secrets.XXX }}` 引用，运行时作为环境变量注入 runner 进程——YAML 仓库文件里永远不出现明文；
3. **日志脱敏**：GitHub 对 secret 值在全部日志输出中自动打码为 `***`；工具侧仍遵守双保险——token 只从 `os.environ` 读取，不打印、不写日志、不进报告，错误信息中不含 URL 查询参数与 header；
4. **fork 隔离**：公开仓库下，fork PR 触发的 workflow **拿不到 secrets**（schedule/workflow_dispatch/push 不受影响），天然防止外部 PR 偷取 token；
5. **误提交防护**：GitHub secret scanning + push protection 会拦截含常见 token（含 GitHub PAT）的提交；仓库另加 CI 检查扫描 diff 中的 `BUILDKITE_API_TOKEN=` 等字面量兜底。

本地/服务器运行侧：token 放环境变量或 `.env` 文件（`.gitignore` 必须包含 `.env`、`store/`、`logs/`），启动时由 pydantic-settings 加载。

> 说明：报告 commit 回仓库使用 Actions 内置 `GITHUB_TOKEN`（运行时临时签发，job 结束即失效），不属于需要长期保管的 secret。

## 8. 错误处理与可观测性

| 场景 | 处理 |
| --- | --- |
| Dashboard API 5xx/超时/失败索引为空 | 指数退避重试 ≤ 5 次；仍失败则降级走 Buildkite builds 扫描（token 必需），并在审计中记录降级 |
| Buildkite API 401/403 | 直接失败退出并提示 token 问题（不重试） |
| Buildkite API 429/5xx/网络 | 指数退避重试 ≤ 5 次，429 读 `Retry-After` |
| build 号解析失败（web_url 格式变化） | 该条目跳过拉日志、标注 `unparseable_url`，进报告"人工核查"小节 |
| 日志中未找到段落标记 | 报告中标注 `section_not_found`，附 job 链接供人工核查 |
| 报告写入失败 | 退出码非 0，下次运行时间窗自动覆盖补写 |

日志：标准输出 + `logs/run-YYYYMMDD.log`（运行级，非 CI 日志），记录扫描数、命中数、API 用量。

## 9. 测试策略

- **单元测试**：PR 号解析、段落提取（含标记缺失回退）、break 正则（覆盖 break/broken/breakdown、大小写、行首行尾）、报告渲染快照测试；
- **契约测试**：用 Buildkite API 录制的 JSON fixture（build 列表、job 详情、log 分页）做离线集成测试；
- **端到端**：`--backfill 1` 对真实 API 跑一天窗口，人工核对报告与 ci.vllm.ai Jobs 页面一致。

## 10. 技术栈与项目结构

- Python 3.11+，依赖最小化：`httpx`（HTTP + 分页/重试）、`pydantic-settings`（配置）、`rich`（CLI 输出，可选）；
- SQLite 用标准库 `sqlite3`，Markdown 用模板渲染，不引入重依赖。

**多工具布局约定**：本仓后续会上线多个工具，约定根目录下 `tools/<tool-name>/` 为**每个工具的完全自包含目录**——代码、配置、测试、文档、CI workflow、运行产物（报告/状态/日志）全部收在自己的目录内，工具之间零共享代码（重复优于早抽象）、零路径交叉。新增工具时复制骨架即可，删除工具时整目录移除。

```
main2main-automator/
├── docs/                          # 仓库级文档
│   └── design.md                  # 本设计文档
└── tools/
    └── ascend-npu-report/         # 本工具的独立根目录
        ├── ascend_report/         # Python 包
        │   ├── __init__.py
        │   ├── cli.py             # 入口：run / backfill
        │   ├── config.py          # 配置加载
        │   ├── dashboard.py       # ci.vllm.ai /api/jobs/runs 客户端（失败索引）
        │   ├── buildkite.py       # Buildkite API 客户端（日志抓取 + 交叉校验）
        │   ├── github.py          # PR 反查（commit_sha → PR 状态）
        │   ├── analyzer.py        # 段落提取 + 结构化解析 + 收录判定
        │   ├── reporter.py        # 报告渲染
        │   └── store.py           # SQLite 状态与日志快照
        ├── tests/                 # 本工具专属测试
        ├── workflow/
        │   └── ascend-ci-report.yml   # GitHub Actions 定义（6.1）
        ├── reports/               # 报告输出（report-YYYY-MM-DD.md，commit 回仓）
        ├── store/                 # state.db + logs/ 运行产物（.gitignore）
        ├── config.yaml
        ├── .gitignore
        └── pyproject.toml         # 独立安装包，CLI 入口 ascend-ci-report
```

要点：

- `reports/` 放在工具目录内而非仓库根，多工具产物互不可见；GitHub Actions 上传/提交路径均以此为基准（见 6.1）；
- `pyproject.toml` 每工具独立，依赖互不影响（不建仓库级单一大包）；
- workflow 文件必须放仓库根的 `.github/workflows/` 才生效——实现时在根目录放一个薄薄的入口 workflow，仅指向本工具目录执行（`defaults.run.working-directory: tools/ascend-npu-report`），本工具目录内的 `workflow/ascend-ci-report.yml` 作为其唯一事实来源（复制或 symlink），避免根目录被多工具 workflow 刷屏。

## 11. 实施里程碑

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| M1 | Dashboard 客户端 + 失败索引采集（含 Buildkite 交叉校验） | 能列出指定时间窗内全部 `Ascend NPU Test` 失败（含 soft_failed），并给出 build 号与 job_id |
| M2 | 日志抓取 + Analyzer | 段落提取与 break 匹配在真实日志 fixture 上通过 |
| M3 | PR 状态获取 + Reporter + Store + 调度接入 | 主表仅含存在 break 的条目且带实时 PR 状态（Open/Merged/Closed）；每日自动产出 `reports/report-*.md`，重跑幂等 |
| M4（可选） | IM webhook 通知、多 pipeline（vllm-ascend 仓）扩展 | — |

## 12. 风险与开放问题

1. **Dashboard API 属非契约化接口**：`/api/jobs/runs` 是 dashboard 自用路由，无稳定性承诺（当前已实测可用）。缓解：接口为开源项目（vllm-dashboard）的一部分，版本可控；客户端做成薄适配层，响应结构变化时只需改一处。若未来 dashboard 增加鉴权或下线路由，降级路径为 Buildkite builds 扫描（已在 5.1/第 8 节设计）。
2. **Fivetran→Databricks 同步延迟**：dashboard 索引入库有分钟~小时级延迟，当天临近期末的失败可能查不到。缓解：辅路 Buildkite 扫描交叉校验 + 次日运行的时间窗重叠（lookback 取"上次成功运行时间"而非固定 24h）。
3. **`soft_failed` 被 dashboard 过滤**：runs 查询状态列表不含 `soft_failed`，soft-fail 场景依赖 Buildkite 辅路捕获（已在 5.1 说明）。
4. **Buildkite token**：仅 `read_builds` scope；org/pipeline slug 已确认（`vllm/ci`），拿到 token 后用一次 `GET /v2/organizations/vllm/pipelines/ci/builds?per_page=1` 验证权限即可。
5. **pipeline/step 改名**：Dashboard 侧按 `jobName` 精确匹配，Buildkite 侧按 `step_key` 匹配，均配置化；审计表记录未命中情况。
6. **段落标记漂移**：日志格式已源码级核实（vllm-ascend `test_vllm_pr_interface_compatibility.py` + `range_analysis.py`），但上游代码变更仍可能改变输出。缓解：标记/字段全部走配置化正则；结构化解析失败时回退兜底 break 正则并在报告中显式标注，不会静默漏报。
7. **日志体量**：job 日志含完整 docker build 输出，可能达数万行/数十 MB。已设计分页拉取 + 本地快照落盘 + 段落定位（先找 `+++` 标记再做行级解析），避免全量驻留内存。
8. **PR 关联解析**：主路用 `commit_sha` 反查 GitHub（可靠）；force-push 导致 sha 悬空时回退 build message/分支名解析，PR 号解析失败的记录允许 PR 状态列为空并保留日志链接兜底，不阻塞报告生成。
9. **GitHub API 限速**：匿名 60 次/小时，一个窗口的失败数即可打满。已缓解：workflow 注入 Actions 内置 token（1000 req/h）+ `pr_cache` 持久缓存 + 限速短路；本地 backfill 大量补数时设置个人 PAT（5000 req/h）。
10. **REVIEW 类结果的处置**：`Result: REVIEW` 表示有需要人工复核的接口变化但非确定性 break——按硬性收录规则不进主表，仅入附录 A；是否升级进主表可后续配置。
