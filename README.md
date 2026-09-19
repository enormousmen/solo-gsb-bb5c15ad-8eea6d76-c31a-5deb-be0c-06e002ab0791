# 共享 JSON 配置三方合并 API

基于 **Python 3.11 + FastAPI + SQLite** 的共享 JSON 配置存储。文档更新携带
`base_revision`、`request_id` 与 [RFC 7386 JSON Merge Patch](https://www.rfc-editor.org/rfc/rfc7386)；
当基线过期（服务端已有更新修订）时，服务端对 **基线 / 当前版 / 来稿** 做递归三方比较：

- 双方修改的路径互不重叠，或改动结果相同 → **自动重放（replay）**，生成新修订；
- 同一路径被双方改成不同结果 → **409 冲突**，返回路径（RFC 6901 JSON Pointer）
  及三方值，**绝不写入**；
- 同一 `request_id` + 相同内容 → **幂等**，直接返回原结果；
- 同一 `request_id` + 不同内容（patch 或 base_revision 变化）→ **409**。

需要一次发布多个互不相关的文档时，可用 **`POST /releases` 原子发布**：
请求携带 `release_id` 与 2～20 项文档更新（`document_id`、`base_revision`、
`patch`、`author`，且 `document_id` 互不重复），服务端在**同一个 SQLite 写事务**内
逐项复用上面的三方合并与契约校验：

- 任一文档不存在、基线无效、路径冲突或契约校验失败
  → `422 release_failed`，`failures` 按输入顺序汇总每项的结构化错误，
  **所有文档均不产生修订**，且 `release_id` 不被占用（修正后可用同 ID 重试）；
- 全部通过 → 一次性写入各文档新修订，返回每项最终的 `revision` / `content`；
- `release_id` 对**规范化整批请求**（含项顺序与 `author`）持久化幂等：
  相同请求重放原结果，不同请求返回 `409 release_id_conflict`；失败不占用 ID。

每个文档还可以挂载一个**可选的 [JSON Schema 2020-12](https://json-schema.org/draft/2020-12) 契约**：

- 设置契约时先校验 Schema **本身**是否合法，并在**同一事务**内验证当前文档；
  任一不通过都返回 `422` 且**不替换**既有契约；
- 文档存在契约时，PATCH 先完成 Merge Patch 与三方重放，再校验**最终候选内容**；
  失败返回 `422 schema_validation_failed`，错误按实例路径稳定列出，
  不写入修订、不占用 `request_id`（修正后可用同 ID 重试）；
- 删除契约后，PATCH 立即恢复为无契约的原更新行为。

## 一键启动

需要 Docker 与 Docker Compose v2：

```bash
docker compose up -d --build
```

容器启动时自动建表（FastAPI lifespan 执行 `CREATE TABLE IF NOT EXISTS`），
无需任何初始化命令。SQLite 数据持久化在命名卷 `config-data`（容器内 `/data`）。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷
```

## 配置项

全部通过环境变量配置（均有默认值，可直接使用 `.env`，模板见 `.env.example`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONFIG_PORT` | `8000` | 宿主机映射端口（容器内固定监听 8000） |
| `CONFIG_DB_PATH` | `/data/config.db` | SQLite 数据库文件路径，父目录自动创建 |
| `CONFIG_HOST` | `0.0.0.0` | 监听地址 |
| `CONFIG_HISTORY_MAX_LIMIT` | `200` | 修订历史单页返回上限 |

## 访问地址与端口

默认端口 `8000`：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/docs | Swagger UI（可在线调试） |
| http://localhost:8000/redoc | ReDoc 文档 |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查（容器 HEALTHCHECK 也使用它） |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/documents` | 创建文档（修订号从 1 开始） |
| `GET` | `/documents` | 列出文档 |
| `GET` | `/documents/{id}` | 读取当前版本（含 `revision`） |
| `PATCH` | `/documents/{id}` | 提交 Merge Patch（三方合并，存在契约时最终校验） |
| `POST` | `/releases` | 原子发布 2～20 个文档更新（单事务、聚合失败、整批幂等） |
| `GET` | `/documents/{id}/revisions` | 修订历史（支持 `limit`/`offset`/`order=asc|desc`） |
| `GET` | `/documents/{id}/revisions/{rev}` | 查询指定修订的内容与 patch |
| `PUT` | `/documents/{id}/schema` | 设置 / 替换 JSON Schema 2020-12 契约 |
| `GET` | `/documents/{id}/schema` | 查询当前契约（未设置返回 `404 schema_not_found`） |
| `DELETE` | `/documents/{id}/schema` | 删除契约（幂等，删除后恢复原更新行为） |

### 创建文档

```bash
curl -X POST http://localhost:8000/documents \
  -H 'Content-Type: application/json' \
  -d '{"id": "demo", "content": {"a": 1, "b": 2, "c": {"x": 1}}, "author": "alice"}'
# id 可省略，省略时服务端生成；id 重复返回 409 document_exists
```

### 读取文档

```bash
curl http://localhost:8000/documents/demo
# {"id":"demo","revision":1,"content":{"a":1,"b":2,"c":{"x":1}},...}
```

读取后请记下 `revision`，作为下次更新的 `base_revision`。

### 更新（快路径：基线即当前版）

```bash
curl -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-001", "patch": {"a": 100}}'
```

```json
{
  "id": "demo",
  "revision": 2,
  "base_revision": 1,
  "request_id": "req-001",
  "replayed": false,
  "changed": true,
  "idempotent": false,
  "content": {"a": 100, "b": 2, "c": {"x": 1}}
}
```

### 更新（基线过期：路径不冲突 → 自动重放）

在别人基于 rev1 已把 `a` 改成 100（当前 rev2）之后，你仍基于 rev1 只改 `b`：

```bash
curl -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-002", "patch": {"b": 200}}'
```

```json
{ "revision": 3, "replayed": true, "changed": true,
  "content": {"a": 100, "b": 200, "c": {"x": 1}} }
```

三方在对象层级递归比较；重放结果与当前版完全相同时不产生新修订
（`"changed": false`，`revision` 保持当前号）。

### 更新（分歧路径 → 409，且不写入）

rev1 中 `a=1`，服务端已把 `a` 改成 `100`，来稿基于 rev1 要改成 `999`：

```bash
curl -i -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-003", "patch": {"a": 999}}'
```

```json
HTTP/1.1 409 Conflict
{
  "detail": {
    "error": "conflict",
    "document_id": "demo",
    "request_id": "req-003",
    "base_revision": 1,
    "current_revision": 2,
    "conflicts": [
      {
        "path": "/a",
        "values": {"base": 1, "current": 100, "theirs": 999},
        "missing": []
      }
    ]
  }
}
```

- `path` 为 RFC 6901 JSON Pointer，根冲突为 `""`，键中的 `~`/`/` 转义为 `~0`/`~1`；
- `values` 给出该路径上的 **基线值 / 服务端当前值 / 来稿值**；
- 键缺失（被删除）时对应位置为 `null`，并在 `missing` 中标注
  （如 `"missing": ["current"]`，以区别于真实存储的 JSON `null`）；
- 冲突请求在事务内回滚：文档内容、修订号均不变，且该 `request_id` 不被占用，
  调用方修正基线/内容后可用同一 ID 重试。

### 幂等

```bash
# 同 request_id + 同 (base_revision, patch) 再发任意次 → 返回原结果
# 响应中 "idempotent": true，不产生新修订
```

同 `request_id` 但 `base_revision` 或 `patch` 不同 → `409 request_id_conflict`。
指纹为 `sha256(base_revision + 规范化JSON(patch))`，幂等记录按 `(document_id, request_id)` 持久化。

### 原子发布（POST /releases）

一次发布把多个互不重复的文档更新放进**同一个 SQLite 写事务**：
逐项复用单文档 PATCH 的 Merge Patch / 三方重放 / 契约校验逻辑，
**全部通过才提交**，任一失败则整体回滚。每批限 **2～20 项**，
`updates` 中 `document_id` 必须互不重复（同一文档的多项修改请在调用方合并为一个 patch）。

```bash
curl -X POST http://localhost:8000/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "release_id": "release-2026-01-01-001",
    "updates": [
      {"document_id": "demo",  "base_revision": 1, "patch": {"a": 100}, "author": "alice"},
      {"document_id": "other", "base_revision": 3, "patch": {"b": 200}, "author": "bob"}
    ]
  }'
```

```json
{
  "release_id": "release-2026-01-01-001",
  "idempotent": false,
  "results": [
    {"document_id": "demo",  "revision": 2, "base_revision": 1, "replayed": false,
     "changed": true, "author": "alice", "content": {"a": 100, "b": 2}, "created_at": "..."},
    {"document_id": "other", "revision": 4, "base_revision": 3, "replayed": true,
     "changed": true, "author": "bob",   "content": {"b": 200},      "created_at": "..."}
  ]
}
```

`results` 按输入顺序排列；每项字段与单文档 PATCH 响应一致
（`replayed` 表示该项是否走了过期基线三方重放；重放结果与当前版相同的项
`changed=false`、不产生新修订）。发布产生的修订在修订历史中以
`request_id = release_id` 记录，便于从文档侧追溯所属发布。

#### 失败聚合：422 release_failed

任一项不通过都会收集错误并继续处理剩余项（不止于首个失败），
最终按 **输入顺序** 返回 `failures`，HTTP 状态统一为 `422`；
每个失败项自带其对应单文档接口的 `error` 与 `status_code`：

```json
HTTP/1.1 422 Unprocessable Entity
{
  "detail": {
    "error": "release_failed",
    "release_id": "release-2026-01-01-001",
    "reason": "发布中存在失败项，全部文档未产生修订",
    "failures": [
      {"index": 1, "document_id": "ghost", "error": "not_found", "status_code": 404},
      {"index": 2, "document_id": "demo", "error": "unknown_base_revision",
       "status_code": 422, "base_revision": 99, "current_revision": 2,
       "reason": "base_revision 不存在"},
      {"index": 3, "document_id": "other", "error": "conflict", "status_code": 409,
       "base_revision": 3, "current_revision": 5,
       "conflicts": [{"path": "/b", "values": {"base": 1, "current": 9, "theirs": 200},
                      "missing": []}]},
      {"index": 4, "document_id": "bound", "error": "schema_validation_failed",
       "status_code": 422, "base_revision": 1, "current_revision": 1,
       "errors": [{"path": "/a", "validator": "minimum", "message": "...",
                   "schema_path": "/properties/a/minimum"}]}
    ]
  }
}
```

回滚保证：响应 422 时**没有任何文档**产生新修订（包括同批本可成功的项），
且该 `release_id` **不被占用**——修正请求内容后可用同一个 ID 重试。
请求体本身的问题（批大小越界、`document_id` 重复、字段不合法等）
在模型层直接返回 `422` 校验错误，同样不落库、不占用 ID。

#### 整批幂等

指纹为 `sha256(规范化JSON(按序的 [document_id, base_revision, patch, author] 列表))`：
JSON 空白与键顺序不影响判定，但**项顺序、文档集合、基线号、patch、author 的差异
都视为不同请求**。

- 相同请求重放（即使其中基线号已经过期）→ 返回首次发布的原始结果，
  `"idempotent": true`，不重新合并、不产生新修订；
- 同一 `release_id` 对应不同请求 → `409 release_id_conflict`；
- 只有**成功**的发布才持久化幂等记录（失败不占用 ID，见上）；
- 发布幂等记录独立于单文档 `request_id` 表，两者同名也不冲突。

### 修订历史

```bash
curl 'http://localhost:8000/documents/demo/revisions?order=desc&limit=20&offset=0'
curl http://localhost:8000/documents/demo/revisions/3
```

历史列表为轻量条目（修订号、基线号、request_id、是否重放、作者、时间）；
单条修订详情含完整 `content`、原始 `patch` 与重放标记。

## JSON Schema 契约（可选）

每个文档最多一个契约，遵循 [JSON Schema 2020-12](https://json-schema.org/draft/2020-12)
（根为 Schema 对象或布尔 `true`/`false`）。契约存放在独立的 `contracts` 表中，
随 SQLite 数据卷持久化，容器重启不丢失。

### 设置契约

`PUT /documents/{id}/schema`，请求体固定为 `{"schema": <schema>}`：

```bash
curl -X PUT http://localhost:8000/documents/demo/schema \
  -H 'Content-Type: application/json' \
  -d '{"schema": {
        "type": "object",
        "required": ["a", "b"],
        "properties": {
          "a": {"type": "integer", "minimum": 0},
          "b": {"type": "string"}
        },
        "additionalProperties": false
      }}'
# 200 → {"document_id":"demo","schema":{...},"revision":1,"updated_at":"..."}
```

服务端**严格按顺序**执行两步，均在同一写事务中完成（事务内重读当前文档，
避免与并发 PATCH 的竞态）：

1. **校验 Schema 本身**（2020-12 元 Schema）。非法 → `422 invalid_schema`，
   不写入、不替换：

   ```json
   {"detail": {"error": "invalid_schema", "document_id": "demo",
     "reason": "schema 本身不符合 JSON Schema 2020-12",
     "errors": [{"path": "/type", "validator": "anyOf",
                 "message": "...", "schema_path": "/allOf/3/properties/type/anyOf"}]}}
   ```

2. **验证当前文档实例**。不通过 → `422 schema_validation_failed`，
   旧契约（若有）原样保留，文档内容与修订号不变：

   ```json
   {"detail": {"error": "schema_validation_failed", "document_id": "demo",
     "revision": 1, "reason": "当前文档不满足所给 schema，契约未替换",
     "errors": [{"path": "/a", "validator": "type",
                 "message": "'oops' is not of type 'integer'",
                 "schema_path": "/properties/a/type"}]}}
   ```

> **关于 `$ref`**：支持 Schema 内部引用（`{"$ref": "#/$defs/..."}`）与
> 带 `$id` 的文档内解析；服务运行在离线注册表上、**不发起远程检索**，
> 指向外部 URI（或错误内部指针）的 `$ref` 在设置时即以 `invalid_schema`
> （`validator: "unresolvable_ref"`）拒绝，不会留到 PATCH 时才暴露。

### 查询 / 删除契约

```bash
curl http://localhost:8000/documents/demo/schema           # 200 或 404 schema_not_found
curl -X DELETE http://localhost:8000/documents/demo/schema
# 200 → {"document_id":"demo","deleted": true}；重复删除为幂等 no-op，deleted=false
```

### 契约存在时的 PATCH

PATCH 流程变为：**幂等检查 → Merge Patch 应用到基线 → （基线过期时）三方重放 →
契约校验最终候选内容 → 写入**。也就是说，校验发生在合并**之后**，
约束的是即将落库的最终文档，而不是来稿的原始 patch：

```bash
# a 改成字符串且删除必填的 b → 422，一次返回全部错误
curl -i -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-bad", "patch": {"a": "oops", "b": null}}'
```

```json
HTTP/1.1 422 Unprocessable Entity
{"detail": {"error": "schema_validation_failed", "document_id": "demo",
  "request_id": "req-bad", "base_revision": 1, "current_revision": 1,
  "errors": [
    {"path": "",      "validator": "required", "message": "'b' is a required property",
     "schema_path": "/required"},
    {"path": "/a",    "validator": "type",     "message": "'oops' is not of type 'integer'",
     "schema_path": "/properties/a/type"}
  ]}}
```

错误对象字段：

| 字段 | 说明 |
| --- | --- |
| `path` | 错误所在的实例位置，RFC 6901 JSON Pointer，根为 `""` |
| `validator` | 触发失败的 Schema 关键字（`type`/`required`/`minimum`…） |
| `message` | 人类可读的错误说明 |
| `schema_path` | 失败约束在 Schema 内的 JSON Pointer |

错误顺序稳定：按 `path`、再按 `schema_path`、最后按 `message` 排序，
且一次收集**全部**错误（非快速失败）。

语义与回滚保证：

- 校验失败的请求在事务内回滚：**不产生修订、不占用 `request_id`**，
  调用方修正内容后可用同一个 `request_id` 重试；
- 过期基线请求先做三方重放：路径有分歧时仍返回 `409 conflict`
  （冲突优先于契约校验，因为此时不存在合法的候选内容）；可重放但候选不满足契约时才是 `422`；
- 重放结果与当前版完全相同（`changed=false`）时不产生新修订；
- 同一 `request_id` 的幂等重放直接返回原结果，不再重新校验；
- `DELETE` 契约后，PATCH 立即恢复无契约的原更新行为。

## Merge Patch 语义要点（RFC 7386）

- patch 是**对象**时按键合并：键值为 `null` 表示**删除该键**（不是存成 null）；
- patch 为**非对象**（数组 / 字符串 / 数字 / 布尔 / `null`）时**整体替换**文档；
  数组整体替换，不按下标合并；
- 对非对象的目标打对象补丁时，目标先视为空对象；
- 真实的 JSON `null` 作为键值可以正常存储，仅 patch 中的 `null` 才表示删除。

## 状态码

| 状态码 | 场景 |
| --- | --- |
| `200` | 更新成功（含幂等重放）、契约查询 / 设置 / 删除成功 |
| `201` | 文档创建成功 |
| `404 not_found` | 文档不存在（契约接口同理） |
| `404 schema_not_found` | 文档存在但未设置契约（GET） |
| `409 conflict` | 三方合并存在分歧路径（原子发布单项失败时聚合在 `failures` 中） |
| `409 request_id_conflict` | 同一 request_id 对应了不同内容 |
| `409 release_id_conflict` | 同一 release_id 对应了不同的整批请求 |
| `409 document_exists` | 创建时 ID 已存在 |
| `422 release_failed` | 原子发布存在失败项（`failures` 按输入顺序汇总，整批回滚、不占用 release_id） |
| `422 unknown_base_revision` | base_revision 不是该文档的现存修订 |
| `422 invalid_schema` | 设置契约时 Schema 本身不符合 JSON Schema 2020-12 |
| `422 schema_validation_failed` | 当前文档（设置时）或合并后的最终候选（PATCH 时）不满足契约 |

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与更新编排（事务、幂等、冲突响应、契约接口）
│   ├── merge.py       # RFC 7386 打补丁 + 递归三方合并（无框架依赖，可单测）
│   ├── validation.py  # JSON Schema 2020-12 元校验与实例校验（错误稳定排序）
│   ├── db.py          # SQLite 连接、自动建表、文档/修订/幂等/契约表读写
│   ├── schemas.py     # Pydantic 请求模型（额外字段拒绝）
│   └── config.py      # 环境变量配置
├── tests/
│   ├── test_merge.py     # 合并算法单元测试（25 例）
│   ├── test_api.py       # API 集成测试（23 例，独立临时数据库）
│   ├── test_contracts.py # 契约接口与 PATCH 校验集成测试（19 例）
│   └── test_releases.py  # POST /releases 原子发布集成测试（17 例）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export CONFIG_DB_PATH=./data/config.db   # 可选，默认 /data/config.db
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
python3 -m pytest -q
```
