# Domain Context

本文档固定项目中的领域词汇。代码、测试、报告和运维文档应使用这些名称。

- **Submission（提交）**：`oj_submissions` 中的一次不可变上传及其 NFS 输入目录。
- **Snapshot（快照）**：在一个 UTC cutoff 下，通过同一只读、可重复读事务取得的审查输入集合。
- **Single-review corpus（单审语料集）**：每个 `(owner, lab_id)` 的最高分有效成功提交，默认要求 `score >= 60`。
- **Plagiarism corpus（抄袭语料集）**：全部有效成功且达到分数阈值的提交，包括不是个人最高分的历史提交。
- **Baseline（基线）**：课程提供的起始代码，用于排除公共模板内容。
- **Source bundle（源码包）**：一次提交中与审查有关的多文件集合，包含源码、构建脚本、运行脚本、约束和裁剪元数据。
- **Baseline delta（基线差异）**：学生提交相对对应 baseline 的真实 token/文本改动，而不是“整个被修改文件”。
- **Audit task（审查任务）**：可稳定哈希、可缓存、可独立重试的单提交审查或提交对裁决。
- **Candidate（候选对）**：由相同 digest、相同 baseline delta 或近似指纹产生的跨学生提交对。
- **Verdict（审查结论）**：带 schema 版本、证据、置信度和模型元数据的 LLM 输出。
- **Audit run（审查批次）**：同一快照、规则版本、模型配置和代码版本下的一组任务。
- **Shard（分片）**：Indexed Job 中由固定 completion index 负责的一组任务。
- **Report（报告）**：写入 NFS 的不可变任务结果或派生索引；报告不等于纪律处分结论。
- **Review cache（审查缓存）**：按任务内容、规则、prompt 和模型共同寻址的不可变结果，不能只按 `input_digest` 寻址。
