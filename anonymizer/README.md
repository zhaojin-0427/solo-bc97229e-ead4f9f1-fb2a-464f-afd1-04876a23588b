# 热线转写脱敏批处理平台

客服语料工程师上传热线转写（UTF-8 NDJSON），填写 Python `re` 正则规则与替换串，
由后台任务逐条、逐规则做替换，产出可下载的完整脱敏副本。Web 层与后台 worker
分进程运行，长耗时正则、取消和重启都不会阻塞页面或其它任务。

平台还提供**重复语料核查**：自动索引全部已完成副本中的非空 `text`，按逐字相同
归组，列出重复至少两次的文本及其出处（会话号、任务、数据行号），帮助语料工程师
发现重复转写、避免训练时重复加权。核查只读索引，不修改副本。

## 一、启动（Docker Compose，可直接演示）

```bash
cd anonymizer
docker compose up --build
# 浏览器打开 http://localhost:8000
```

- `web` 容器：Gunicorn 托管 Flask，提供上传 / 进度 / 取消 / 下载 / 重复核查页面与 JSON API。
- `worker` 容器：单进程后台轮询任务，独立处理替换并管理正则子进程；
  同时在后台线程为历史已完成副本补建重复语料索引。
- 两者共享命名卷 `anon_data`（挂载到 `/data`），SQLite、上传原件、输出副本、
  临时残件都在其中；容器重建后完成的副本仍然保留。

停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

### 一分钟演示（上传 → 脱敏 → 重复核查）

1. 打开 `http://localhost:8000`，上传 `demo/sample.ndjson`，规则粘贴 `demo/rules.json`。
2. 任务详情页等待状态变为“已完成”，可下载完整脱敏副本。
3. 再上传一次同一文件（制造跨批重复），等待完成。
4. 点击顶部导航“重复语料核查”：样例中的重复行（同批两次 + 跨批各一次，
   共 4 次）会列为一组，点“查看来源”可见每条的会话号、任务号与数据行号。

也可以运行 `scripts/demo_curl.sh` 用 curl 走通同一流程。

## 二、输入与规则

### NDJSON（≤ 5 MiB，UTF-8，每行一个 JSON 对象）

```json
{"session_id":"sess-1001","text":"客户来电，手机号13812345678。"}
```

- 必需字段：匿名会话号 `session_id`（也兼容 `id`）与字符串 `text`。
- 保留：会话号、原始行序、完全重复的行、对象内其它字段。
- 任一行非合法 JSON / 非 UTF-8 / 缺字段 / 空行 → **整批失败**，不产生任何输出。

### 规则（JSON 数组，按数组顺序依次执行）

```json
[
  ["1[3-9]\\d{9}", "<手机>"],
  ["\\d{17}[\\dXx]", "<身份证>"],
  ["[\\w.+-]+@[\\w-]+\\.[\\w.-]+", "<邮箱>"]
]
```

- 每项是 `["Python re 模式", "替换串"]`，替换串支持 `\1` 反向引用（JSON 写 `\\1`）。
- 合法正则一律受理，**不会因“可能很慢”被拒绝**；代价由硬性超时兜底。
- 每条记录、每条规则限时 **2 秒**。超时在独立子进程中被真正 `SIGKILL` 终止，
  随后整批失败；新任务照常处理。
- 正则语法错误同样在写入任何输出前导致整批失败。

`demo/` 目录附带可直接上传的样例：

| 文件 | 用途 |
| --- | --- |
| `demo/sample.ndjson` + `demo/rules.json` | 正常脱敏演示（含重复行、无敏感信息行） |
| `demo/sample-timeout.ndjson` + `demo/rules-timeout.json` | 灾难性回溯触发 2 秒超时、整批失败演示 |

## 三、任务状态与并发语义

状态机：`pending → running → completed`，旁路 `cancelling → cancelled`、
`failed`、以及重启恢复产生的 `interrupted`。

- **后台处理**：上传只入队并落盘原件，替换全部在 worker 中进行；页面与其它
  请求不受影响。
- **取消 / 发布竞争**：取消先被受理则结果永不发布；结果先生效（`completed`）
  则取消被拒绝。该竞争由 SQLite 的条件更新串行化，二者只有一个会赢。
- **停止与清理**：超时或取消都会先真正停掉替换、删除临时残件，然后才进入终态。
- **下载门禁**：只有 `completed` 可下载完整副本；未完成、失败、取消、中断一律
  返回 403/不可下载，绝不暴露部分结果。
- **重启恢复**（仅 worker 启动时执行，Web 重启不影响在途任务）：
  - 正在处理（`running/cancelling`）的任务标记为 `interrupted`，不可下载，并清理残件；
  - 从未被领取的 `pending` 任务保持排队，由新 worker 继续处理；
  - 已完成副本原样保留、仍可下载。

## 四、重复语料核查（/duplicates）

- **归组口径**：全部已完成副本内逐字相同且**非空**的 `text` 归为一组，只列出
  累计出现至少两次的组；同一批次内的重复也分别计数。空文本不参与归组。
- **展示**：组列表分页（文本、出现次数、涉及批次数）；组内来源同样分页
  （会话号、任务号、源文件、数据行号）。只读核查，绝不修改副本。
- **索引存储**：索引存于 SQLite（`dup_texts` / `dup_occurrences` /
  `dup_index_batches` 三表），查询只读索引表，**不重扫副本文件**；分组、计数、
  分页全部在 SQL 内完成，内存占用不随总记录数增长。
- **原子提交**：任务成功状态与该批全部索引在**同一事务**提交；失败、取消、
  中断的批次不产生任何索引行。读取副本发生在写事务之外，不占任务库写锁。
- **首次补建**：worker 启动后在后台线程按批次补建全部历史完成副本，脱敏照常
  进行；每批独立事务提交，重启后从下一个未建批次接续，不重复计数。补建期间
  核查页只显示进度（未就绪），**不返回残缺结果**；就绪判定是实时计算的，
  补建期间完成的批次自动纳入。
- **失败可见可重试**：副本读取失败的批次连同原因列在未就绪页，可一键重试；
  只要还有未建或失败的批次，页面就保持未就绪，绝不误报。

## 五、Web 入口与 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 上传表单 + 任务列表（页面自动轮询刷新） |
| POST | `/tasks` | 上传 NDJSON 与规则，创建后台任务 |
| GET | `/tasks/<id>` | 任务详情、进度条、取消 / 下载按钮 |
| GET | `/api/tasks` | 全部任务 JSON |
| GET | `/api/tasks/<id>` | 单任务 JSON（进度、状态、是否可下载） |
| POST | `/tasks/<id>/cancel` | 请求取消 |
| GET | `/tasks/<id>/download` | 下载完整脱敏副本（仅 completed） |
| GET | `/duplicates` | 重复语料核查页（未就绪时显示进度与失败批次） |
| GET | `/duplicates/<text_id>` | 重复组来源（分页列出会话号 / 任务 / 行号） |
| POST | `/duplicates/<task_id>/retry` | 重试失败批次的索引构建 |
| GET | `/api/duplicates/status` | 索引就绪状态 JSON（ready / indexed / total / failed） |

超过 5 MiB 的上传在 WSGI 层直接返回 413。

## 六、本地无 Docker 时的运行与测试

生产依赖固定在 `requirements.txt`（Flask 3 / gunicorn 22）。本机若已安装依赖：

```bash
pip install -r requirements.txt
ANON_DATA_DIR=./data python -m app.worker &      # 后台 worker
gunicorn -b 0.0.0.0:8000 "app.server.app:create_app()"
```

测试仅使用 Python 标准库（Web 层测试内置最小 Flask 兼容 shim，生产仍用真实 Flask）：

```bash
python3 -I scripts/smoke_test.py         # 75 项：执行器/校验/批处理/取消/恢复/重复索引与补建
python3 -I scripts/integration_test.py   # 34 项：真实 worker 子进程、信号、重启、后台补建
python3 -I scripts/web_layer_test.py     # 63 项：WSGI 路由/模板/下载门禁/413/核查页与重试
```

## 七、目录结构

```
anonymizer/
├── app/
│   ├── config.py            # 路径、5MiB、2s、核查分页等配置
│   ├── db.py                # SQLite 任务表、重复索引表与全部状态转换
│   ├── dupindex.py          # 副本解析、批次索引、历史补建循环
│   ├── executor.py          # 子进程正则执行 + 强制超时 kill
│   ├── validation.py        # NDJSON / 规则校验（坏行整批失败）
│   ├── worker.py            # 后台处理、取消、发布、失败收敛、补建线程
│   └── server/
│       ├── app.py           # Flask 路由、下载门禁、重复核查页
│       ├── templates/       # 首页 / 详情 / 错误 / 重复核查 / 来源页
│       └── static/style.css
├── demo/                    # 可上传的样例数据与规则
├── scripts/                 # 标准库测试与离线 Flask shim
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
└── requirements.txt
```
