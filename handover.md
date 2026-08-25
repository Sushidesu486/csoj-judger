# OJ 合规审查器 — 交接文档

> 写于 2026-08-24。如果通过新 session 接手，先读这份，再看 `~/.claude/projects/-Users-shouss-Project-ZJU-ZJUSCT/memory/` 下三份记忆（`oj-plat101-architecture.md`、`llm-gateway-new-api.md`、`oj-llm-audit-tool-plan.md`）。

## 1. 目标

开发一个工具，用 LLM 审查 HPC101 课程 OJ 上学生提交的代码，判定是否存在不合规行为，**只生成报告**（不改 `is_valid`，不自动作废成绩）。报告按学生用户名分目录落盘到 NFS。

## 2. 集群现状（已摸清，SSH 到 m601）

### 2.1 OJ 服务
- **plat101**：镜像 `harbor.clusters.zjusct.io/library/plat101:0.6.141`（distroless，无 shell，不能 `kubectl exec` 进去看文件系统），部署在 `plat101-system` ns，5 副本，`svc/plat101:80`，对外 https://platform.s.zjusct.io，DB 是 `plat101-db`（Postgres，库名 `plat101`，用户 `plat101`）。
- **Lab 定义**：集群级 CRD `ojlabs.plat101.zjusct.io/v1alpha1`（`OJLab`，简写 `ojl`）。`kubectl get ojlabs` 列出 8 个 lab。CR 自带 `statement`/`submissions.home.required`/`allow`/`scoring`/`workflow[]`。
- **本工具不需要调 k8s API**：每个提交对应的 lab 完整定义存在 DB 的 `oj_submission_runs.lab_definition`（jsonb，是整份 OJLab CR 快照，含 `spec.submissions.home.required/allow`、`spec.scoring`、`spec.workflow`）。只读 DB 就够。

### 2.2 提交存储
- **NFS**：`/tank/hpc101/submissions`（server `root.clusters.zjusct.io`）。
- OJ 部分在 `.oj` 子目录：`/tank/hpc101/submissions/.oj/submissions/<submission-uuid>/{input,work,logs,result}/`。
  - `input/` = 学生上传文件（只读），如 `input/student/moe_opt.cpp`。
  - `result/result.json` = 评测结论。
  - `logs/step-N-attempt-M.log` = 每步评测日志。
  - `work/` = 评测 scratch（build/run 产物，含 `.csoj/result.json`）。
- 约 3495 个提交目录。
- **写报告目录**：`/tank/hpc101/submissions/.oj/audit-reports/<owner>/<lab_id>__<sid前8位>__<score>.json`（按用户名分目录，已确认）。

### 2.3 DB schema（核心表，都在 schema `public`）
- `oj_submissions`：`id`(uuid)、`lab_id`、`owner`、`status`(Success/Failed/Cancelled/Running/Queued/SystemError)、`score`、`is_valid`(bool)、`invalid_reason`、`invalidated_by`、`input_manifest`(jsonb，含 `files[]`：part/path/size/sha256，**顶层 `sha256` = 整份提交的哈希 = `input_digest` 列**)、`input_files`、`input_bytes`、`submitted_at`、`finished_at`、`counts_toward_limit`、`effective_deadline`。
- `oj_submission_runs`：`submission_id`、`state`、`result_info`(jsonb)、`failure_class/reason`、`trigger_kind`(initial/rejudge)、`triggered_by`、**`lab_definition`(jsonb，整份 OJLab CR 快照)**。
- `oj_user_lab_best_scores`：`(owner, lab_id)` 主键，`score`、`submission_id`、`submission_run_id`。
- `oj_score_history`：`owner`、`changed_at`、`total_score`、`lab_id`、`source_submission_id`、`source_run_id`、`reason`。
- `oj_settings`：singleton（`show_deadline_timeline`）。
- `students`：`username`、`grp`（分组）。
- **合规标记机制**：`oj_submissions.is_valid=false` + `invalid_reason` 就是"作废成绩"的标志。现有作废样例：`存在非法优化`、`级联作废`、`hack OJ`、`劣化baseline`。管理员通过 HTTP `POST /api/admin/oj/submissions/:id/validity {valid,reason}` 回写（本工具不用，只读+报告）。

### 2.4 `input_digest` 是什么（已确认）
= `input_manifest->>'sha256'`，对整份提交所有文件内容（按 path 排序）算的 sha256。**两个学生 input_digest 相同 = 上传的每个文件每个字节都完全一样**。已用 NFS 逐文件独立 sha256 复核验证（84 文件全相同，content-blob sha256 一致）。

### 2.5 LLM 网关 new-api
- ns `new-api`，`svc/new-api:3000`，OpenAI 兼容。对外 newapi.clusters.zjusct.io。本工具用集群内 DNS：`http://new-api.new-api.svc.cluster.local:3000/v1`。
- Bearer token 鉴权。可用模型：`gpt-5.6-terra`、`gpt-5.6-sol`、`kimi-k3`、`codex-auto-review`、`gpt-5.6-luna`、`glm-5.2`、`gpt-5.5`。
- **主力用 `glm-5.2`**（已确认）：非流式可用，但正文在 `message.reasoning_content`（`content` 是 null），取值要 `msg.get("content") or msg.get("reasoning_content")`。
- `gpt-5.5`/`gpt-5.6-*` 只支持流式（`"Stream must be set to true"`），留作二审。
- **`/v1/embeddings` 无可用渠道**（text-embedding-3-*/bge-m3/embedding-2 全 503 `model_not_found`）→ 相似检测只能用本地 minhash，不能用 embedding。

## 3. 已确认的设计决策

1. **四类违规**：硬编码/作弊评测、劣化 baseline、违反实验约束、抄袭/高度相似。
2. **报告落地**：仅生成报告（不自动作废）。按用户名分目录。
3. **数据接入**：直读 DB + NFS（不走 HTTP API）。
4. **主力模型**：glm-5.2。
5. **调度**：系统时间每晚 23:30 跑一次全量。
6. **去重**：跨夜缓存（同一 digest 跨夜复用旧判定）。

## 4. 关键发现：最高分口径会漏掉抄袭（重要！）

已验证的字节级抄袭案例：lab4-cpu 的 `h3250104303` ↔ `h3250103195`（共享 digest `36d7e0a81a354630b9bc2270faef9f9515b1fbef5532580fb63aa052fcb6ae62`，84 文件逐字节相同，前者 8/22 12:54 提交 81 分，后者 8/23 21:21 提交 72 分，后者抄前者）。

但这两人各自的**最高分提交** digest 不同（h3250104303 最高 107 分 digest `3d0d95f2...`，h3250103195 最高 84 分 digest `15285a88...`）——那个共享的抄袭版本对两人都不是最高分。

**结论：单一代码审查和抄袭检测该用不同口径**（待用户最终拍板，见第 7 节）：

| 职责 | 口径 | 候选量 | 跨人相同 digest 组 |
|---|---|---|---|
| 单代码审查（硬编码/劣化baseline/违反约束）| 每(owner,lab) 最高分，Success+is_valid+score≥60 | 425 条 / 413 唯一 digest | 1（hello-world 假阳性）|
| 抄袭检测（Layer0 相同digest + Layer1 minhash）| 全部 Success+is_valid+score≥60 | 2472 条 / 1720 唯一 digest | 3（含 lab4-cpu、lab5 真抄袭）|

各 lab 候选量（最高分口径）：lab2 110、lab3 90、lab2-riscv 76、lab4-cpu 50、lab5 45、lab4-gpu 40、lab3p5 8、hello-world 5。

## 5. 三层去重 + 相似检测设计

- **Layer 0 精确去重**：NFS 上维护索引 `audit-reports/.index.json`（`input_digest → 旧 verdict + 审查时间`）。同一 digest 跨夜只调一次 LLM，后续克隆旧判定。当夜内精确重复也省。
- **Layer 1 指纹相似**：去注释/空白归一化 → token 化 → 滑动 shingle → **minhash**（标准库 `hashlib` 即可，不依赖 embedding）。近邻对（>0.85）是改写抄袭候选。
- **Layer 2 LLM 裁决**：仅对 Layer1 命中的相似对 + Layer0 跨人相同 digest 组调 LLM 二审，判"真抄袭 vs 巧合/公共 baseline"。

## 6. 已就绪的集群基础设施

在 `csoj-judger` ns（空 ns，本工具专用）：
- Secret `oj-audit-db`（key `url`，plat101-db 连接串副本）。
- Secret `oj-audit-llm`（key `base-url`=`http://new-api.new-api.svc.cluster.local:3000/v1`，`api-key` 仅保存在 Kubernetes Secret 中，不写入仓库）。
- ConfigMap `oj-audit-one-script`（单提交审查脚本，见下）。
- ConfigMap `oj-audit-probe`（早期 probe）。

运行环境镜像：`harbor.clusters.zjusct.io/public/devbox:4`（Python 3.13.5，有 pip，无 psycopg2/httpx，需 `pip install --user psycopg2-binary httpx`；HOME 要设 `/tmp`，PYTHONUSERBASE 设 `/tmp/pylibs`，否则 pip 写 `/.local` 权限拒绝）。

NFS 挂载权限：用 `runAsUser: 65532, runAsGroup: 1000, fsGroup: 1000`（和 plat101 一致，能读 `.oj`）。写 `audit-reports` 时挂整个 `/tank/hpc101/submissions` 为 rw。

DB 连接串在 pod 里要把 host 补全 DNS：`url.replace("@plat101-db:","@plat101-db.plat101-system.svc.cluster.local:")`。

## 7. 已写出的代码

### 7.1 单提交审查脚本（已跑通，产出了第一份报告）
位置：ConfigMap `oj-audit-one-script`（`run.py`）。逻辑：
1. 取最新一条 `status='Success' AND is_valid=true` 提交（这是早期版本，用的"最新"而非"最高分"，需改成最高分）。
2. 从 `oj_submission_runs.lab_definition` 取 statement/required/allow/workflow。
3. 从 `input_manifest->files` 挑审计目标文件（优先 required 列表里的源码文件，按大小取最大）。
4. NFS 读代码 + `result/result.json`（代码截断 100KB）。
5. 构造 prompt（四类违规），调 glm-5.2。
6. 解析 JSON verdict，写报告到 NFS。

第一份测试报告（lab4-gpu, h3250105245, score=0）：`/tank/hpc101/submissions/.oj/audit-reports/lab4-gpu__h3250105245__f5e95285.json`。LLM 判定质量不错（识别出 GPU lab 提交了纯 OpenMP CPU 代码）。

> 这份脚本只是原型，正式 checker 要重写：双口径查询 + 三层去重 + 四类审查 + 报告落盘 + CronJob。

## 8. 已知坑

- **lab4 多文件 lab**：单审主文件不够，应把 `compile.sh`/`run.sh`/`CMakeLists.txt` 一起送 LLM。
- **statement 截断**：原型截到 6000 字，lab4 不够，LLM 抱怨"缺少 statement.md 具体约束"。
- **glm-5.2 取值**：正文在 `reasoning_content`。
- **devbox 镜像**：HOME/PYTHONUSERBASE 必须设到可写目录。
- **NFS 写权限**：挂 `/tank/hpc101/submissions`（rw）而非 `.oj`（ro），因为报告目录在 `.oj` 下但要写。

## 9. 下一步（待办）

1. **拍板口径**：是否采用第 4 节的"双口径"（单审最高分 + 抄袭检测全部≥60）？这是写正式 checker 前必须定的。
2. **写正式 `audit_checker.py`**：含
   - 双口径 DB 查询（参数化，避免 SQL 注入/列名误用——之前踩过坑，`%s` 占位符别和 jsonb key 混）。
   - Layer 0 索引文件读写（`.index.json`）。
   - Layer 1 minhash（归一化 + shingle + 128 个 hash）。
   - Layer 2 LLM 二审 prompt。
   - 单审 prompt（四类，statement 不截或截大些，多文件 lab 合并送）。
   - 报告落盘按用户名分目录 + 跨人相似汇总。
   - 容错：单条失败不影响整体，超时重试。
3. **打包成 CronJob**：schedule `30 23 * * *`（系统时间，集群时区确认是 UTC，23:30 是 UTC 还是本地？需确认集群时区）。镜像用 devbox 或自建含依赖的镜像。
4. **小规模验证**：先单 lab（lab2）全量跑，看 LLM 判定质量，再铺开。
5. **测试集**：把字节级抄袭案例（digest `36d7e0a8...`）作为 Layer 0 的标杆测试（用户说先不用固化）。

## 10. 常用命令速查

```bash
# SSH 进集群
ssh m601

# 看 OJ lab
kubectl get ojlabs
kubectl get ojlab lab2 -o yaml

# 连 DB（pod 内）
DBPOD=$(kubectl -n plat101-system get pod -l app.kubernetes.io/name=plat101-db -o jsonpath="{.items[0].metadata.name}")
kubectl -n plat101-system exec "$DBPOD" -- psql -U plat101 -d plat101 -c "\dt"
# 注意：psql 列名/值用单引号，jsonb key 用 'key'，shell 里转义麻烦，写脚本用 psycopg2 参数化更稳

# 查最高分口径候选
kubectl -n plat101-system exec "$DBPOD" -- psql -U plat101 -d plat101 -c "
SELECT DISTINCT ON (owner, lab_id) owner, lab_id, id, score, input_digest
  FROM oj_submissions
 WHERE status='Success' AND is_valid=true AND score>=60
 ORDER BY owner, lab_id, score DESC, submitted_at DESC;"

# 跑审查器（临时 pod 模式，正式版会换成 CronJob）
# 见 ConfigMap oj-audit-one-script + Pod oj-audit-one 的 yaml 模板（在 /tmp/audit_one_pod.yaml）

# 读报告
# NFS 上 /tank/hpc101/submissions/.oj/audit-reports/<owner>/*.json
```

## 11. 联系点 / 待问用户

- 口径是否双口径（第 4、9 节）。
- CronJob 时区（UTC vs 本地 +8）。
- 报告是否需要汇总页（HTML/索引）方便浏览，还是只看 JSON。
