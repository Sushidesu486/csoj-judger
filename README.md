# OJ Arbiter

OJ Arbiter 是 HPC101 OJ 的只读合规审查器。它从 plat101 PostgreSQL 和提交 NFS 读取数据，使用本地相似度算法与集群内 LLM 生成审查报告；它不修改提交有效性，也不向 OJ 数据库写入任何内容。

历史调查和集群现状见 [`handover.md`](handover.md)，需求见 [`docs/spec.md`](docs/spec.md)，模块与 Kubernetes 执行设计见 [`docs/architecture.md`](docs/architecture.md)。

## 安全边界

- OJ 数据库仅用于一致性只读快照。
- 学生提交和 baseline 以只读方式挂载。
- 只有审查报告目录允许写入。
- LLM 结论是人工复核线索，不自动修改 `is_valid` 或成绩。
- 凭据只能来自 Kubernetes Secret 或本地未跟踪环境变量。

## 当前状态

- 本地 Git 仓库已初始化。
- 已实现只读 DB 快照、双语料集、固定 Git baseline/实验文档和真实 baseline delta。
- 相似检测包含整份提交 digest、delta digest、MinHash/LSH 和精确 shingle Jaccard。
- 已实现 `gpt-5.6-luna` 流式结构化审查：每个学生最高提交的合规审查，以及全部历史提交候选对的抄袭裁决。
- Review key 包含提交、delta、HPC101 commit/tree、实验规则、prompt/schema、模型与参数；成功结果写入不可变 ledger，完全相同的后续批次不会再次调用 LLM。
- Manifest 冻结 submission metadata 与 lab snapshot；相同 `run_id` 不允许覆盖不同内容。
- `SubmissionStore.load_bundle` 已实现安全相对路径、symlink 防护、普通文件校验和多文件读取预算。
- 已提供不调用 LLM 的 `doctor`、`plan`、`smoke` 命令，以及正式 `audit` 命令和固定到 m601 的 canary Job 模板。
- 已提供 `report-api` 命令：只读查询单提交合规报告，或现场发起一次且仅针对一个 Submission 的审查；现场审查不生成 plagiarism 任务。
- 集群已创建专用 `oj_checker_ro` 登录角色和 `oj-audit-db-ro` Secret；该角色只拥有 `oj_submissions`、`oj_submission_runs` 的 `SELECT`，且默认事务只读。代码仍保留连接参数与事务级只读作为第二道防线。

## 正式审查入口

`audit` 每次只处理一个 lab。合规语料是该 lab 每个学生的最高有效成功提交；抄袭语料不受最高分筛选影响，包含该 lab 的全部合格历史提交。命令没有 canary `--limit`，避免把“一个 lab canary”误解为只抽查少量学生。

```bash
export DB_URL='postgresql://...'
export LLM_API_KEY='...'
export HPC101_REPOSITORY='../HPC101'
export HPC101_REVISION='c12aaa26a346fd3fd8a39eceb3ac355ba14d19b0'
export OJ_CHECKER_GIT_COMMIT="$(git rev-parse HEAD)"

oj-checker audit --lab lab4-cpu --model gpt-5.6-luna
```

正式入口默认使用 8 个独立进程并行计算各提交的 MinHash 签名；可通过
`--similarity-workers` 调整。该参数只影响候选生成速度，不改变候选身份或审查 cache。

只有 `completed` 且非 `inconclusive` 的结果会进入跨批次 cache。失败、无法解析和 `inconclusive` 都会在后续批次重新审查。LLM 结论始终只是人工复核线索。

## 开发命令

```bash
make check
make zipapp
make container-smoke
scripts/deploy-smoke.sh
```

`deploy-smoke.sh` 会更新开发 ConfigMap、重建 `oj-checker-smoke` Job、等待完成并输出日志。该 Job 不读取 LLM Secret，也不调用 glm。
脚本仅接受干净工作树，并把真实 Git commit SHA 写入本次 manifest。

`make container-smoke` 使用 `uv.lock` 构建面向集群 `linux/amd64` 的生产镜像，并以只读根文件系统和 UID 65532 验证 CLI、项目包及 PostgreSQL 驱动可用。可通过 `IMAGE=...`、`PLATFORM=...` 覆盖本地构建参数。
