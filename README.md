# main2main-automator

vllm/vllm-ascend main2main automator

## 工具列表

每个工具是 `tools/` 下的自包含目录（代码、配置、测试、workflow 副本），互不依赖。

### ascend-npu-report — Ascend NPU Test 失败监控与 break 报告

每日定时巡检 vLLM CI 中的 `Ascend NPU Test`，在失败日志中提取 **vLLM PR 与 vllm-ascend 的接口兼容性分析结果**，将确认存在 break 的 PR 整理成表格报告。

- **数据源**：[ci.vllm.ai](https://ci.vllm.ai/jobs) 失败索引（免 token）+ Buildkite API 交叉校验与日志拉取（需 token）
- **报告内容**：主表仅收录解析出 break 的记录（PR 编号/状态、break 数、vLLM API 变更、vllm-ascend 受影响代码、影响说明、日志链接）；同 PR 多次执行只保留最新一次
- **PR 状态**：由 commit 反查 GitHub 实时状态（Open / Merged / Closed），已合入 PR 带 break 优先处理
- **部署**：GitHub Actions 每日 09:00（北京时间）定时运行，报告 commit 回本仓 `tools/ascend-npu-report/reports/`
- **本地运行**：

  ```bash
  cd tools/ascend-npu-report
  pip install -e .
  export BUILDKITE_API_TOKEN=...   # Buildkite，read_builds scope
  python -m ascend_report.cli run --date 2026-09-03

  # 汇总模式：合并已有日报，缺失天自动补充
  python -m ascend_report.cli run --backfill 7 --merge
  ```

- 详细设计：[docs/design.md](docs/design.md)
