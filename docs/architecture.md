# Architecture Proposal

状态：Draft，等待确认测试 seam 后进入实现。

本文记录当前 batch、相似检测和同步单提交审查架构。单提交违规优化审查拟迁移到
隔离 Agent Job，控制面、异步 API、tool broker 和安全边界见
[`agent-review-design.md`](agent-review-design.md)；代码查重仍沿用本文的相似检测链路。

## 总体数据流

```mermaid
flowchart LR
    DB[(plat101 DB\nread only)] --> P[Planner\nindex 0]
    NFSR[(Submission + baseline NFS\nread only)] --> P
    P --> M[Immutable run manifest\n+ task list]
    M --> W0[oj-checker shard 0]
    M --> W1[oj-checker shard 1]
    M --> WN[oj-checker shard N]
    NFSR --> W0
    NFSR --> W1
    NFSR --> WN
    W0 --> LLM[new-api / LLM]
    W1 --> LLM
    WN --> LLM
    W0 --> NFSW[(Audit reports NFS\nread/write)]
    W1 --> NFSW
    WN --> NFSW
```

外部调用者只需要理解一个深模块：`AuditRunner.run(request) -> RunSummary`。快照一致性、双语料集、源码打包、baseline 差异、缓存、分片、重试和原子落盘都属于它的 implementation。

## 模块与 seam

### `AuditRunner`

外部 seam。CLI、Kubernetes Job 和本地测试通过同一个 interface 发起批次。

```python
run(request: AuditRequest) -> RunSummary
```

`AuditRequest` 只表达审查意图：模式、lab/owner/limit 过滤、cutoff、阈值、并发度和 dry-run。它不暴露 SQL、NFS 路径拼接或 LLM 请求细节。

### `SubmissionCatalog`

内部真实 seam，用于取得一致快照。

```python
snapshot(request: SnapshotRequest) -> SubmissionSnapshot
```

Adapters：

- `PostgresSubmissionCatalog`：生产只读事务。
- `InMemorySubmissionCatalog`：测试固定样例。

该 module 隐藏双语料集 SQL、run snapshot 选择和数据库类型转换。

### `SubmissionStore`

内部真实 seam，用于从 manifest 安全构建 source bundle。

```python
load_bundle(submission: Submission, policy: SourcePolicy) -> SourceBundle
```

Adapters：

- `NfsSubmissionStore`：生产 NFS。
- `FixtureSubmissionStore`：测试 fixture。

该 module 隐藏路径校验、多文件预算、baseline 定位、token 差异和编码错误处理。

### `SimilarityDetector`

算法 module，不为尚不存在的算法提前增加 adapter。它提供一个深 interface：

```python
detect(bundles: Iterable[SourceBundle], policy: SimilarityPolicy) -> CandidateSet
```

implementation 负责整份 digest、baseline-delta digest、MinHash/LSH、精确 Jaccard、同 lab 限制和候选去重。未来确实引入 AST 或其他算法时，再把该 seam 提升为可替换 adapter。

### `Reviewer`

内部真实 seam，每次只审一个稳定任务。

```python
review(task: AuditTask) -> ReviewResult
```

Adapters：

- `OpenAICompatibleReviewer`：new-api，支持 `content`/`reasoning_content`。
- `ReplayReviewer`：本地测试和历史结果重放。

prompt 构造、schema 校验、重试与速率限制都留在 module implementation 内，调用方不解析原始 HTTP JSON。

### `FileReportStore`

首版只有一个 filesystem implementation，根路径可指向 NFS 或测试临时目录，因此不引入额外 adapter seam。它负责内容寻址、原子写入、断点续跑和派生索引。

### `ComplianceReportQuery`

报告 API 的外部 seam 是单提交查询/现场审查：

```python
get_submission_report(submission_id: str) -> ComplianceReport | None
launch_single_review(submission_id: str) -> ReviewLaunchResult
```

HTTP adapter 只接受一个规范 UUID。`launch_single_review` 通过
`AuditRequest.submission_id` 进入现有 `AuditRunner`，因此现场审查不会退化
成全 lab 批次，也不会创建跨学生 plagiarism 任务。报告查询实现隐藏
`owners/` 目录、缓存刷新和内部 JSON schema，只向调用方返回稳定的 verdict、
证据摘要和 provenance。

## Source bundle 策略

“最大文件”不是合法的审查策略。每个 source bundle 应包含：

- lab statement、required/allow、workflow 和 result 摘要。
- manifest 中所有相关的学生可修改源码。
- `compile.sh`、`run.sh`、`CMakeLists.txt`、配置文件等执行上下文。
- 每个文件相对 baseline 的真实 delta。
- 在总预算内为 delta 保留前后上下文。

相似指纹使用 delta token；LLM 裁决使用 delta 加有限上下文。这样既降低公共 baseline 噪声，也不会把“文件被修改”误称为“文件内容全是学生改动”。

NFS adapter 不使用字符串拼接后直接 `open()`：submission ID 必须是规范 UUID，manifest path 必须是无空段、`.`、`..`、反斜杠或 NUL 的相对 POSIX 路径；根目录、中间目录和最终文件逐级使用 `openat` + `O_NOFOLLOW`，并只接受普通文件。所有相关文件保留元数据，内容超过预算时显式标记截断原因。

## Indexed Job 执行模型

一个 CronJob 创建一个 `completionMode: Indexed` Job，建议初始 `completions: 4`、`parallelism: 4`：

1. completion index 0 在只读事务中生成快照、source metadata 和稳定排序的任务清单。
2. index 0 原子写入 `runs/<run_id>/manifest.json` 和 `_READY`。
3. 其他 Pod 等待 `_READY`，不参与规划。
4. 每个任务按稳定哈希分配到一个 completion index。
5. 每个 Pod 跳过已有且 cache key 匹配的结果，完成后写 `shards/<index>.done`。
6. index 0 等待全部 shard done，生成批次 summary 和派生索引。

这一设计不要求 Kubernetes API 写权限或外部消息队列。Pod 设置 `automountServiceAccountToken: false`。

所有 Pod 使用 required node affinity：

```yaml
requiredDuringSchedulingIgnoredDuringExecution:
  nodeSelectorTerms:
    - matchExpressions:
        - key: kubernetes.io/hostname
          operator: In
          values: [m601.clusters.zjusct.io]
```

m601 是 control-plane 节点但当前无 taint，因此必须设置 CPU/内存 requests 和 limits，并限制 LLM 并发。

## 数据库只读防线

开发 `oj-audit-db` 实际使用的 `plat101` 角色具备写权限，不能用于正式审查。
2026-08-25 已创建 `oj_checker_ro` 和 `oj-audit-db-ro`；前者只拥有
`oj_submissions`、`oj_submission_runs`、`oj_user_lab_best_scores` 的
`SELECT`，并已验证无 UPDATE、schema CREATE、superuser、CREATEROLE、
CREATEDB、REPLICATION 或 BYPASSRLS 权限。

首版开发 Pod 同时使用：

- 连接参数：`default_transaction_read_only=on`。
- 批次事务：`BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY`。
- 固定、参数化的 SELECT 查询。
- `statement_timeout`、`lock_timeout` 和 `idle_in_transaction_session_timeout`。

正式 canary 使用 `oj-audit-db-ro`。代码级只读仍必须保留，它是数据库授权之外的额外防线。

## NFS 状态布局

不使用一个并发修改的 `.index.json`。采用不可变、内容寻址的小文件：

```text
audit-reports/
  runs/<run_id>/
    manifest.json
    tasks/<task-key>.json
    results/<prefix>/<task-key>.json
    shards/<index>.done
    summary.json
  cache/review/<schema>/<prefix>/<cache-key>.json
  owners/<owner>/<lab>__<submission-prefix>__<score>.json
  plagiarism/<lab>/<pair-key>.json
```

manifest 冻结所选 submission 的 owner、score、提交时间、input manifest、active run state/result_info，并按内容哈希去重 lab definition，使 worker 无需重新查询 DB。写入流程是同目录临时文件、flush/fsync、原子 hard-link create-only；规划身份相同时复用原始 `generated_at`，不同内容不能覆盖已有 `run_id`。批次文件记录真实 Git commit、UTC cutoff、规则版本、prompt 版本、模型、阈值和 completion 数。

## 建议确认的测试 seam

TDD 只在以下 interface 上验证行为：

1. `SubmissionCatalog.snapshot`：最高分语料集与全部历史语料集不会混淆，快照 cutoff 和只读事务生效。
2. `SubmissionStore.load_bundle`：路径安全、多文件收集和真实 baseline delta；含 `.cu` 的提交不会再被打包成“无 GPU 代码”。
3. `SimilarityDetector.detect`：公共 baseline 不产生候选，非最高分相同 digest 会被发现，阈值和同 lab 限制有效。
4. `Reviewer.review`：结构化 schema、`reasoning_content` fallback、重试和 cache-key 版本失效。
5. `AuditRunner.run`：使用 in-memory/replay adapters 验证一个批次可断点续跑并生成 summary，不测试内部调用顺序。

确认后按以上顺序做垂直切片，而不是一次性写完所有测试。

## 夜间执行

夜间 CronJob 在 `Asia/Shanghai` 02:00 启动，只读取 Plat101 已维护的
`oj_user_lab_best_scores`，不在 checker 内重新判定最高分。它逐条调用常驻
report API 并固定发送 `model=glm-5.3`，因此和管理员现场审查共享同一个串行
锁。CronJob 没有 LLM 凭据、Submission NFS 或常驻 scheduler，也没有调用
次数预算；`concurrencyPolicy: Forbid`、`backoffLimit: 0` 和六小时 deadline
限制重入和失败资源占用。

只有当前最高分 Submission 已存在匹配当前 basis/rules/prompt/schema、且模型
仍在 allowlist 中的 `compliant` 报告时才跳过。违规、证据不足和失败项留到
下一晚再次审查；合规违规结果不会进入跨批次 cache。

## 当前实现边界

当前单 Pod 正式链路已经实现 read-only snapshot、固定 Git review basis、baseline delta、三层相似候选、两类结构化 Reviewer、完整 review identity、不可变 cache/task result、owner/plagiarism 派生报告和断点式跨批次复用。`audit --lab` 不提供 `--limit`；一个 lab canary 会覆盖该 lab 每个学生的最高提交，而抄袭侧始终保留全部合格历史提交。若源码预算导致 delta 不完整，Layer 1/2 会安全跳过并在 manifest、summary 和 CLI 中显式记录 exclusion，不能静默漏审。MinHash 签名按提交使用独立进程并行计算，正式 CLI 默认 8 workers；worker 数只属于执行配置，不参与 review identity。

夜间链路保持单线程，不使用 Indexed Job 多 Pod 分片。部署清单见
`deploy/kubernetes/nightly-cronjob.yaml`；启用日程前应先从该模板创建一次
手工 Job，验证候选数量、跳过条件和 report API 串行行为。
