# Platform OJ-checker 管理页调研

调研日期：2026-08-25

## 结论

可以在现有 Platform 中加入 OJ-checker 管理页，而且产品落点很自然：Platform 已经是 OJ 的管理员控制面，已有同源 React SPA、服务端管理员鉴权、Submission 详情/归档下载，以及人工作废和恢复计分接口。

推荐的边界不是让浏览器读 CephFS，也不是把 checker 直接改造成公网管理服务，而是：

```text
Browser
  -> Plat101 /api/admin/oj/checker/*
       - 复用现有 session、requireAdmin 和 Origin 防护
       - 将 OJ Submission 与 checker finding 组合成稳定 DTO
  -> 集群内 oj-checker-report-api（ClusterIP，仅内部访问）
       - 只读 checker 报告 PVC
       - 不持有 Plat101/OJ DB 写权限
  -> CephFS report PVC
```

第一版应定位为“人工复核工作台”：浏览、筛选、聚合和确认线索；不负责自动定案，不允许 LLM/checker 自动作废成绩。人工确认后仍调用 Plat101 已有的 validity endpoint。

OJ-checker 的核心批处理链路已经能产出有用的抄袭线索，但系统还不能称为完整的生产管理系统：缺少稳定的查询 API、永久存储声明、人工复核状态与操作审计、审查调度接口；最高分提交的合规审查覆盖率也仍明显不足。

## 一手来源与版本

### Platform

- 官方仓库：`git@github.com:ZJUSCT/plat101.git`
- 调研 commit：`af80977a4b989e85d34545f7264af15a9d7b01dd`
- 2026-08-25 生产镜像：`harbor.clusters.zjusct.io/library/plat101@sha256:58c943a32448211aa92efafd3fbb5fb206b0364baeb49ba6e508d8bda6d79b7e`
- 该镜像的 SLSA provenance 指向上述仓库和 commit；调研时远端 `main` 也是同一 commit。

本文中的 `plat101/...` 行号均对应这个 commit。

### OJ-checker

本文中的 `OJ-Arbiter/...` 行号对应当前未提交工作树。调研只新增本文，没有修改业务代码，也没有 commit 或 push。

## 1. Platform/OJ 当前结构

Plat101 不是“独立前端 + 独立 API”两套服务，而是 Go 后端和 React SPA 同仓、同镜像、同域部署：

- Dockerfile 先用 Node 构建 SPA，再把 `web/dist` 复制进 Go build，最后生成一个 distroless 单二进制镜像（`plat101/images/plat101/Dockerfile:3-28`）。
- Go 使用 `embed.FS` 嵌入 SPA（`plat101/web/embed.go:5-13`）。
- 后端注册 `/api/*` 后，把其余无扩展名路径回退到 `index.html`，支持 browser router（`plat101/internal/server/server.go:207-237`）。

因此页面和公共 API 都应优先在 `ZJUSCT/plat101` 中实现，不需要新建一个公开前端 Deployment。

### 前端技术栈

- React 18、TypeScript、Vite。
- React Router 6。
- TanStack React Query。
- Tailwind CSS 4。
- Vitest + Testing Library。

依赖和构建脚本见 `plat101/web/package.json:6-40`；Vite 同时配置了 React、Tailwind、开发期 `/api`/`/auth` 代理和 jsdom 测试环境（`plat101/web/vite.config.ts:1-9`）。

### 页面和路由组织

页面集中在 `web/src/pages`，请求与 DTO 集中在 `web/src/api.ts`，OJ 领域前端类型集中在 `web/src/oj.ts`，全局 UI/菜单集中在 `web/src/ui.tsx`。

现有 OJ 管理路由：

```text
/admin/oj
/admin/oj/submissions
/admin/oj/submissions/:submissionId
```

路由表见 `plat101/web/src/main.tsx:38-68`。`/admin/oj` 的标题栏已经放置“评测提交管理”按钮，适合并排增加“OJ Checker”入口（`plat101/web/src/pages/AdminOJ.tsx:233-253`）。

建议新增：

```text
/admin/oj/checker
/admin/oj/checker/runs/:runId
/admin/oj/checker/findings/:findingId
```

Submission 不必再建 checker 专用详情页；finding 中双方 Submission ID 直接链接到现有 `/admin/oj/submissions/:submissionId` 即可。

最小前端改动面：

- `web/src/main.tsx`：注册路由。
- `web/src/api.ts`：Checker DTO 与请求函数。
- 新增 `web/src/pages/AdminOJChecker.tsx`，后续可拆 run/finding 子页面。
- `web/src/pages/AdminOJ.tsx`：增加入口按钮。
- 如需从身份菜单直接进入，再修改 `web/src/ui.tsx`。

## 2. 管理员身份与权限守卫

浏览器通过 `/api/me` 获得 `user`、`admin` 和 `features.oj` 等信息；前端类型见 `plat101/web/src/api.ts:37-46`，后端响应见 `plat101/internal/server/me.go:68-89`。

当前菜单只在 `me.admin && me.features.oj` 时显示 OJ 管理入口（`plat101/web/src/ui.tsx:295-341`）。但是路由表没有 `AdminRoute` 一类的客户端 guard；知道 URL 的普通用户仍可打开页面壳，随后由 API 拒绝。这不是安全漏洞，因为真正边界在服务端：

- `sessionFrom` 接受 session cookie 或 bearer token；无效认证返回 401（`plat101/internal/server/server.go:119-142`）。
- CLI token 只允许 job/queue/partition/event API，不能用于管理员 API（`plat101/internal/server/server.go:137-149`）。
- `requireAdmin` 对非管理员返回 403（`plat101/internal/server/admin.go:9-22`）。
- 通用前端请求在 401 时跳转登录，其他错误（包括 403）作为 `ApiFailure` 交给页面显示（`plat101/web/src/api.ts:20-35`）。

所有新增 Checker handler 都必须首先调用 `requireAdmin`。前端隐藏入口只能改善体验，不能承担授权。

可以额外增加一个轻量前端 `AdminOnly` 壳来避免普通用户看到错误闪烁，但它不改变服务端安全要求。

## 3. 当前 OJ Submission/admin API

Plat101 已有完整的 OJ 管理 API 注册点（`plat101/internal/server/oj_admin.go:29-43`）：

```text
GET    /api/admin/oj/labs
PUT    /api/admin/oj/settings
POST   /api/admin/oj/labs
GET    /api/admin/oj/labs/{id}
PUT    /api/admin/oj/labs/{id}
DELETE /api/admin/oj/labs/{id}

GET    /api/admin/oj/submissions
GET    /api/admin/oj/submissions/{id}
GET    /api/admin/oj/submissions/{id}/archive
POST   /api/admin/oj/submissions/{id}/cancel
POST   /api/admin/oj/submissions/{id}/rejudge
POST   /api/admin/oj/submissions/{id}/validity
POST   /api/admin/oj/rejudge
```

提交列表支持 `owner`、`lab`、`state`、`limit`、`offset`，limit 最大 200；handler 和参数约束见 `plat101/internal/server/oj_admin.go:357-400`，store 选项见 `plat101/internal/db/oj.go:155-162`。

现有管理页面已经能够：

- 按用户、Lab、运行状态筛选；
- 查看 Submission 与 Runs；
- 取消、按原配置/当前配置重测；
- 下载提交归档；
- 查看 Step Jobs；
- 作废或恢复计分。

页面实现见 `plat101/web/src/pages/AdminSubmissions.tsx:16-74`；前端 API 包装见 `plat101/web/src/api.ts:561-592`。

### validity endpoint 可以直接复用

请求：

```http
POST /api/admin/oj/submissions/{id}/validity
Content-Type: application/json

{"valid": false, "reason": "教师人工复核确认：……"}
```

服务端规则：

- 必须是管理员；actor 从当前 session 取得，不能由浏览器伪造。
- 作废时 reason 必填。
- 成功后发布 `oj-scores` 与 `oj-submissions` 事件。
- 恢复使用 `{"valid": true, "reason": ""}`。

见 `plat101/internal/server/oj_admin.go:639-670`。PostgreSQL 在事务中更新 `is_valid`、`invalid_reason`、`invalidated_by`，然后重算 best score/score history（`plat101/internal/db/oj_pg.go:720-772`）。测试覆盖普通用户被拒、原因必填、作废和恢复（`plat101/internal/server/oj_admin_test.go:21-41,68-87`）。

有一个现有展示缺口：DB 模型已有 `InvalidReason` 和 `InvalidatedBy`（`plat101/internal/db/oj.go:69-100`），但公开的 `ojSubmissionResponse` 只返回 `IsValid`，没有返回这两个字段（`plat101/internal/server/oj_submissions.go:64-88,771-780`）。Checker 页面上线前应补齐，否则无法清楚显示“谁因何处理了该提交”。

## 4. Checker 报告 API 应放在哪里

### 推荐：Plat101 公共 API + 内部 report service

公开 API 应放在 Plat101：

```text
/api/admin/oj/checker/*
```

原因：

1. Plat101 已拥有浏览器 session、管理员权限、Origin 防护和 OJ Submission 链接。
2. 前端与 API 同源，不需要增加 CORS、第二套登录或公开 ingress。
3. 所有处分性动作继续走 Plat101 现有 validity endpoint，actor 和成绩重算语义保持唯一。

但 Plat101 不应直接理解 checker 的内部目录树。建议在 `csoj-judger` namespace 部署小型 `oj-checker-report-api`：

- 挂载 checker CephFS PVC：查询路径可按只读语义使用，现场审查路径需要
  追加写入不可变单提交报告；禁止覆盖既有 finding/run 文件；
- 提供稳定、分页的内部 JSON API；
- 不对公网创建 HTTPRoute/Ingress；
- 通过 NetworkPolicy 只允许 Plat101 访问；
- Plat101 校验管理员后代理/整形响应。

当前报告 PVC 位于 `csoj-judger`，而 Platform 位于 `plat101-system`。PVC 是 namespace-scoped，不能把现有 PVC 名直接写到另一 namespace 的 Deployment。内部 service 也避免 Platform 直接依赖 checker 的文件命名和 schema 版本。

### 不推荐方案

- **浏览器直接访问 CephFS/S3：** 会绕过管理员鉴权，暴露学生身份、源码证据和内部报告结构。
- **Platform 直接挂 checker PVC：** 当前 PVC 跨 namespace 不可直接复用；即使重新 provision，共享底层目录也会把文件解析、路径安全和 schema 兼容耦合进 Platform。
- **直接公开 checker API：** checker 当前是 batch CLI，没有用户/session/CSRF 安全模型；为它新建公网身份系统没有必要。
- **Platform 直连 checker 的只读 OJ DB 账号：** checker finding 的权威数据是不可变报告，不应在展示时重新跑相似度或从 DB 推导模型结论。

## 5. 建议的只读 API 与数据模型

### 外部 Plat101 API

第一版只读：

```text
GET /api/admin/oj/checker/summary
GET /api/admin/oj/checker/runs?lab=&status=&limit=&cursor=
GET /api/admin/oj/checker/runs/{runId}
GET /api/admin/oj/checker/findings?type=&lab=&owner=&decision=&humanStatus=&minConfidence=&limit=&cursor=
GET /api/admin/oj/checker/findings/{findingId}
GET /api/admin/oj/checker/submissions/{submissionId}
```

`submissions/{id}` 返回该提交关联的所有 checker 结果和 review identity，不替代 OJ Submission 详情。

人工复核状态稳定后再增加：

```text
PATCH /api/admin/oj/checker/findings/{findingId}
```

body 只允许 `humanStatus`、`comment`。处分动作不放进这个 endpoint，仍单独调用 validity API。

### 建议 DTO

```ts
type CheckerRunSummary = {
  runId: string;
  cutoff: string;
  generatedAt: string;
  labs: string[];
  model: string;
  basisCommit: string;
  rulesVersion: string;
  taskCount: number;
  completed: number;
  inconclusive: number;
  failed: number;
  cacheHits: number;
};

type FindingEvidence = {
  submissionId: string;
  path: string;
  description: string;
};

type HumanReview = {
  status: "pending" | "confirmed" | "dismissed";
  actor?: string;
  comment?: string;
  updatedAt?: string;
};

type ComplianceFinding = {
  id: string;
  type: "compliance";
  runId: string;
  labId: string;
  owner: string;
  submissionId: string;
  score: number;
  decision: "compliant" | "violation" | "inconclusive";
  confidence: number;
  categories: string[];
  summary: string;
  evidence: FindingEvidence[];
  reviewIdentity: string;
  humanReview: HumanReview;
};

type PlagiarismFinding = {
  id: string;
  type: "plagiarism";
  runId: string;
  labId: string;
  owners: [string, string];
  submissionIds: [string, string];
  submittedAt: [string, string];
  decision: "plagiarism" | "independent" | "inconclusive";
  relationship: "exact" | "near_identical" | "minor_edit";
  similaritySignal: "exact_submission" | "exact_delta" | "minhash";
  jaccard: number;
  confidence: number;
  summary: string;
  evidence: FindingEvidence[];
  reviewIdentity: string;
  humanReview: HumanReview;
};
```

ID 应是稳定的内容标识，例如 review key，不要使用目录序号。列表 API 必须分页；61 个 owner pair 已经适合 UI，但具体历史 submission pair 会更大，不能一次返回所有证据正文。

### 与当前报告格式的映射

Checker 已经写入：

- run manifest/result/attempt/summary（`OJ-Arbiter/src/oj_checker/report_store.py:23-122`）；
- owner 派生索引（`OJ-Arbiter/src/oj_checker/report_store.py:124-143`）；
- plagiarism 派生索引（`OJ-Arbiter/src/oj_checker/report_store.py:145-162`）；
- 不可变、内容寻址 review cache（`OJ-Arbiter/src/oj_checker/review_ledger.py:107-160`）。

派生抄袭报告已有 `submission_ids`、`owners`、提交时间、signal、Jaccard 和 `human_review_status: pending`（`OJ-Arbiter/src/oj_checker/runner.py:381-419`）。run summary 已有任务、cache、LLM、completed/inconclusive/failed 计数（`OJ-Arbiter/src/oj_checker/runner.py:421-444`）。

目前的 `human_review_status` 只是每份 JSON 中的初始字符串，不是可变、并发安全的人工状态库。不要直接覆盖不可变 finding JSON；应将人工状态单独存入 Plat101 PostgreSQL 或 checker 专用小表，并记录 actor、时间和 comment。

## 6. 页面交互建议

### 首页 `/admin/oj/checker`

- 批次概览：模型、cutoff、baseline commit、规则版本、完成/失败/inconclusive、cache hit。
- Tabs：`疑似抄袭`、`规范违规`、`未完成/失败`、`审查批次`。
- 默认进入疑似抄袭，并按 owner pair 聚合；先显示高 Jaccard/exact，再显示历史命中较多的关系。
- 筛选：Lab、owner、decision、human status、最低 confidence、similarity signal。

### 抄袭 finding

- 顶部显示双方账号、提交 ID、分数、时间线和相似信号。
- 显示 relationship、Jaccard、模型 confidence、summary。
- evidence 按双方文件路径成对展示。
- 提供“打开 Submission 管理页”和“下载提交归档”，复用现有页面/API。
- 显示同一 owner pair 的全部历史 submission pair，避免把 15 个版本对误解成 15 对学生。

### 规范 finding

- 显示最高分 submission、规则类别、实验依据和具体文件证据。
- 将 `hardcoded_or_checker_abuse`、`constraint_violation` 等类别做可读标签，但保留原始枚举。
- 显示证据是否完整；inconclusive/failed 不得包装成“无违规”。

### 人工操作

- `确认违规`、`排除`、`待补证据`应写人工审查状态和备注。
- “作废成绩”必须是独立的危险操作：二次确认、强制填写理由、明确显示 submission ID 和学生账号。
- 禁止默认勾选或批量作废；第一版甚至可以只提供跳转到现有 Submission 页，由管理员在那里执行作废。

## 7. OJ-checker 当前是否完备

### 已具备

- 对每位学生最高有效成功提交做合规审查；抄袭侧保留全部合格历史提交。
- baseline delta、exact digest/delta、MinHash/LSH、精确 Jaccard 和 LLM 结构化裁决。
- 完整 review identity、明确结论 cache、失败/inconclusive 不缓存、断点续跑。
- 只读 DB、只读 submission 输入、安全路径加载和不可变报告写入。
- 已经能生成按 owner 和 plagiarism 聚合的人工复核材料。

当前实现边界由 `OJ-Arbiter/docs/architecture.md:190-194` 明确记录：仍是单 Pod 正式链路，Indexed Job 多 Pod 分片和夜间 CronJob 尚未启用。

### 还不完备

1. **合规审查覆盖率不足。** 2026-08-25 批次共有 948 个任务，明确完成 437、inconclusive 151、failed 360；抄袭 finding 已形成 261 个 submission pair/61 个 owner pair，但合规侧只有 12 条明确违规线索。该批次自己的 warning 也声明所有结论仅是人工复核线索。来源：批次 `full-20260825-122549` 的 `batch-summary.json` 与 `plagiarism-shortlist.md`，权威副本位于 `csoj-judger/oj-checker-reports-20260825:/reports/batches/full-20260825-122549/`。
2. **没有稳定查询 API。** 当前权威接口是文件目录和 JSON，不适合浏览器筛选、分页和 schema 演进。
3. **没有真正的人工状态存储与审计。** `pending` 是派生 JSON 初始值，不能安全地由多管理员更新。
4. **没有生产调度控制面。** 当前 CLI/临时 Job 可以运行，但没有管理员可见的排队、取消、重试和进度模型；Indexed Job/CronJob 也未上线。
5. **报告存储尚未正式 GitOps 化。** 本轮结果在临时命名的 CephFS PVC；原 submissions NFS 已满，不应再作为正式报告盘。
6. **快照重放仍需强化。** 已观察到固定 submission cutoff 后，关联 active run/lab definition 仍可能变化；管理页必须显示 manifest 中冻结的 basis，而不能只展示 OJ 当前状态。
7. **缺少面向 UI 的兼容 schema。** 内部 review/cache JSON 是执行实现格式，不宜直接承诺为长期公网 API。

因此，更准确的状态是：**抄袭复核的离线引擎已可用，合规审查和平台化管理仍在工程化阶段。**

## 8. 部署与 GitOps 位置

### Platform 当前部署

源码内有：

- Deployment/Service：`plat101/deploy/deployment.yaml:15-205`；
- HTTPRoute：`plat101/deploy/httproute.yaml:1-30`；
- RBAC：`plat101/deploy/rbac.yaml`。

现有 HTTPRoute 把 `platform.s.zjusct.io`、`plat101.clusters.zjusct.io` 和 `clusters.zju.edu.cn` 的根路径都转给同一个 Plat101 service，因此新增同源页面和 `/api/admin/oj/checker/*` 不需要新增 HTTPRoute（`plat101/deploy/httproute.yaml:14-30`）。

2026-08-25 集群只读观察：

- namespace：`plat101-system`；
- Deployment `plat101`：5 replicas，生产镜像使用 digest pin；
- HTTPRoute `plat101` 已存在；
- Deployment managed fields 是 `kubectl-client-side-apply`、`kubectl-patch`、`kubectl-set`，没有 Argo manager。

相邻 `argo-cd.clusters.zjusct.io` 仓库及 Argo Application 列表中没有 Plat101 application。也就是说，当前 Platform 本体仍由 `ZJUSCT/plat101/deploy` 清单以 imperative 方式管理；源码清单的 2 replicas/tag 也与线上 5 replicas/digest 存在漂移（`plat101/deploy/deployment.yaml:22-23,77-80`）。

### 推荐归属

- Platform 页面和公开 proxy handler：`ZJUSCT/plat101`。
- 内部 `oj-checker-report-api`、报告 PVC、NetworkPolicy：`csoj-judger` namespace。
- Checker 镜像 build：沿用现有 GitOps Tekton 管道位置，但正式前更新为已验收 commit/image digest。
- 若决定将 Platform 纳入 GitOps，应建立独立 `production/plat101` application，把 Deployment/Service/RBAC/HTTPRoute 一并迁入；不要只把 Checker 接口的 volume/env patch 零散放进 GitOps，而 Platform 主体继续手工管理。

## 9. 安全边界

1. **服务端管理员授权：** 每个公开 handler 必须调用 `requireAdmin`，不能只看前端 `me.admin`。
2. **网络隔离：** report API 只建 ClusterIP，不建公网 route；NetworkPolicy 只允许 Plat101 workload 访问。
3. **最小存储权限：** report API 只能读取既有 finding/run，并以 create-only 方式追加现场单提交报告；禁止覆盖不可变 review/cache。人工状态写入独立表，不修改既有报告。
4. **不暴露凭据：** 浏览器永远不接触 OJ DB、checker DB role、LLM token、Ceph credential 或 Kubernetes token。
5. **不泄露模型内部内容：** API 只返回经过 schema 校验的 verdict/evidence，不返回 raw prompt、raw model stream 或 reasoning content。
6. **路径安全：** finding ID、run ID、submission ID 必须严格解析并映射到 allowlisted 文件；禁止把 query 参数拼成任意 filesystem path。不要提供“读取任意报告路径”的接口。
7. **内容安全：** 学生代码和 evidence 按纯文本渲染；如支持 Markdown，必须禁用原始 HTML/脚本并防止链接注入。
8. **处分边界：** LLM verdict 永远只是线索。只有管理员显式确认后才能调用 validity endpoint；reason 中应包含 finding ID/run ID 以便追溯。
9. **操作审计：** 人工状态改变和作废动作记录 actor、时间、old/new state、comment、finding ID、submission ID；不能依赖浏览器日志。
10. **并发与幂等：** 人工状态更新使用 version/ETag 或数据库条件更新，避免两个管理员互相覆盖。

## 10. 分阶段落地计划

### Phase 0：先稳定 Checker 数据产品

- 为 run、compliance finding、plagiarism finding 定义公开 schema version。
- 将正式报告 PVC 命名、容量、Retain/备份策略纳入 GitOps。
- 生成分页友好的只读索引，避免 report API 每次扫描数千个小文件。
- 重构合规 evidence 分片，优先降低 failed/inconclusive。
- 修复可重放 snapshot，使 active run/lab definition 与 cutoff 一起冻结。

验收条件：相同 run 可稳定重放；finding schema 有 fixtures；报告盘重启/迁移后不丢失；页面不依赖临时 batch 汇总脚本。

### Phase 1：只读人工复核台

- 在 `csoj-judger` 部署内部 report API。
- 在 Plat101 增加 `/api/admin/oj/checker/*` proxy/DTO。
- 新增 `/admin/oj/checker` 页面，先实现抄袭 owner pair、具体 submission pair、规范违规和失败任务浏览。
- 链接现有 Submission 页/归档下载，不在 Checker 页直接修改成绩。

验收条件：普通用户得到 403；管理员可从 finding 定位到双方原提交；大列表分页；删除/重启 Pod 后报告仍可访问。

### Phase 2：人工状态和处分闭环

- 增加独立 human review 表和 audit log。
- 实现 pending/confirmed/dismissed、备注、actor、时间和并发控制。
- 在明确二次确认后调用现有 validity endpoint，并将 finding ID 写入 reason。
- Plat101 Submission 响应补齐 `invalidReason`、`invalidatedBy`。

验收条件：任何成绩变更都能从 Submission 追到管理员和 finding；LLM/checker 账号没有 DB 写权限；不存在批量自动作废路径。

### Phase 3：运行管理

- Checker Indexed Job/CronJob 正式化。
- 页面展示 run 排队、运行、失败、续跑与 cache 命中情况。
- 只有在明确授权和配额控制后，才考虑由页面创建审查 run；启动动作与报告浏览权限分离。

这一阶段之前，审查仍可由运维/教师通过受控 Job 启动，管理页只负责消费结果。
