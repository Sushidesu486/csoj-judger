# OJ Arbiter Specification

状态：Draft，基于 2026-08-25 的讨论与集群核验。

## 目标

构建可本地版本管理、可测试、可扩展，并能以专用 Pod 在 m601 上运行的 OJ 合规审查系统。

系统只生成报告，覆盖：

1. 硬编码或作弊评测。
2. 劣化 baseline。
3. 违反实验约束。
4. 抄袭或高度相似。

## 已确认约束

- 在当前目录使用 Git 管理项目。
- 运行时直接读取 plat101 PostgreSQL 与提交 NFS。
- 不调用 OJ 写接口，不修改 `is_valid`、成绩或任何 OJ 表。
- LLM 通过集群内 OpenAI-compatible new-api 调用。
- LLM 审查工作负载使用专用 `oj-checker` Pod，并固定调度到 `m601.clusters.zjusct.io`。
- 报告写入 `/tank/hpc101/submissions/.oj/audit-reports`。
- 定时任务按北京时间每天 23:30 运行时，应显式配置 `timeZone: Asia/Shanghai`。
- 昨日探索性运行已对最高分语料集中 MinHash 相似度大于或等于 0.7 的 429 个候选对进行 LLM 裁决，产生 3 个待人工复核的抄袭候选。

## 数据选取

正式系统采用两个语料集：

- 单提交合规审查：每个 `(owner, lab_id)` 最高分的有效成功提交，默认 `score >= 60`。
- 抄袭检测：全部有效成功且 `score >= 60` 的提交，避免遗漏发生在非最高分历史版本中的复制行为。

每个批次必须记录 UTC cutoff，并在 `REPEATABLE READ READ ONLY` 事务中构建一致快照。

## 安全要求

- 生产凭据使用专用数据库角色 `oj_checker_ro`，只授予 `oj_submissions`、`oj_submission_runs` 的 `SELECT`。
- 所有 Pod 同时使用连接参数 `default_transaction_read_only=on` 和显式只读事务，作为数据库授权之外的第二、第三道防线。
- SQL 必须固定并参数化；调用方不得传入列名或 SQL 片段。
- Submission 和 baseline NFS 挂载只读；报告根目录单独读写挂载。
- Pod 禁用 ServiceAccount token 自动挂载、提权和多余 Linux capabilities。
- 日志与报告不得包含数据库密码、LLM token 或完整学生源码。
- LLM 结果不得自动触发成绩作废。

## 功能要求

### 快照与源码包

- 从 DB 读取提交、运行快照、lab 约束和 manifest。
- 校验 manifest 路径，拒绝绝对路径和目录穿越。
- 为每种 lab 组装多文件 source bundle，不能只选择最大文件。
- 从 baseline 计算真实差异，同时保留裁决所需的有限上下文。

### 相似检测

- Layer 0：跨学生相同整份提交 digest。
- Layer 1：跨学生相同 baseline delta。
- Layer 2：MinHash/LSH 产生近似候选，并以精确 shingle Jaccard 复核阈值。
- 所有阈值和 tokenizer/policy 版本必须进入批次清单和缓存键。
- 相似检测必须按 lab 隔离，并排除公共 baseline 噪声。

### LLM 审查

- 支持单提交四类审查和提交对抄袭裁决两种任务。
- 输出必须通过版本化 schema 校验。
- 兼容 `message.content` 与 `message.reasoning_content`。
- 支持超时、有限重试、并发限制、逐任务失败和断点续跑。
- cache key 至少包含任务类型、提交 digest、source fingerprint、规则版本、prompt 版本、模型和模型参数。

### 报告

- 每个任务结果不可变、可独立重试，并使用原子写入。
- 每个批次保留 manifest、代码版本、输入 cutoff、统计和失败摘要。
- 按 owner/lab 生成方便人工浏览的派生索引。
- 抄袭报告必须保留双方提交、时间顺序、相似信号、LLM 证据和人工复核状态。

## 非目标

- 自动作废成绩或回写 OJ。
- 在 OJ DB 中创建队列表或保存报告。
- 首版构建 Web 管理界面。
- 把一次 LLM verdict 当作最终处分依据。

## 单提交报告 API

Checker 提供一个集群内、单提交范围的 HTTP API，供后续 Platform 管理页或
其他受控调用方使用。API 不接受 lab、owner 或 submission ID 数组，也不接受
源码路径。

```text
GET  /v1/compliance/submissions/{submission_id}
POST /v1/compliance/reviews
     {"submission_id": "<uuid>"}
```

`GET` 只读取已有的不可变单提交合规报告；`POST` 现场启动一次只针对该
Submission 的合规审查，不生成 plagiarism 任务。两者都只返回
`compliant`、`violation` 或 `inconclusive` 语义，不能把缺少报告或审查失败
解释为合规。API 服务只挂载报告 PVC 和提交输入，只通过只读 DB 账号取得
现场审查所需的 Submission 元数据。

## 验收顺序

1. 本地单元测试通过。
2. m601 smoke-test Pod 证明数据库会话为只读，并能读取一个限定提交的元数据和文件。
3. canary 批次仅处理一个 lab、少量任务，验证报告 schema 与断点续跑。
4. 重放昨日候选和已知非最高分相同 digest 案例。
5. 再启用每天 23:30 的全量/增量 CronJob。
