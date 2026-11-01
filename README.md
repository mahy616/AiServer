# AiServer

代码推理服务与OpenCode接入工具。客户端统一使用OpenCode，5080运行AiServer／Ollama／模型；服务端使用Python／FastAPI／SQLite，负责鉴权、模型能力、排队、生成、停止、结果与监控。

OpenCode在本机组织项目文件、用户需求、会话历史和工具结果，通过OpenAI兼容Chat Completions接口调用AiServer。模型返回工具调用后，OpenCode在本机读取／编辑文件或执行命令，再将结果发给模型；5080不直接访问本机项目磁盘。

OpenCode使用AiServer签发的独立API key，本机密钥加密保存。服务端原生HTTP／SSE协议与旧模型配置保留兼容；API及OpenCode接入见接口文档，多模型准备见运维文档。

## 使用入口

- 服务管理：双击根目录 `ServerManager.cmd`，选择本机Python和私有配置，加载后管理服务、账号、配置与备份；“运行监控”查看活跃账号、任务性能与硬件资源。
- 服务更新包：双击 `BuildUpdate.cmd`，生成仅含程序的完整小包；0.9.12保留现有模型，新增模型按[运维](docs/正式工程运维.md#0912-增加gpt-oss与gemma-4)安装注册。
- 5080增加三款模型：停服后双击`Install-ExtraModels.cmd`，逐款下载校验GPT-OSS-20B MXFP4、Gemma 4-12B Q4_K_M、Gemma 4-26B-A4B Q3_K_M，全GPU预检通过后追加注册；也提供三个独立入口。OpenCode选择与测试见[运维](docs/正式工程运维.md#0912-增加gpt-oss与gemma-4)。
- 5080准备模型：双击`Install-Qwen35.cmd`准备9B；双击`Install-Qwen38.cmd`下载固定版本并全GPU预检；步骤见[运维](docs/正式工程运维.md#5080增加qwen38-27b)。双模型统一16K输出与已有部署迁移见[运维](docs/正式工程运维.md#0911-仅使用9b与27b)。
- 27B升到80K：停服并更新程序后，双击 `Set-Qwen38-80K.cmd`，只备份修改实际注册的27B窗口；9B及其他调优保留。步骤与回退见[运维](docs/正式工程运维.md#27b更新为80k)。
- 5080推荐窗口：`Apply-5080ModelContexts.cmd`备份设置9B 128K／27B 80K，输出统一16K；已有部署应用与回退见[运维](docs/正式工程运维.md#0911-仅使用9b与27b)。
- 5080上下文寻优：停服后运行`Tune-ModelContexts.cmd`，按1K精度搜索双模型全GPU窗口，在95%显存峰值预算内长输入复核后备份写回；步骤与报告见[运维](docs/正式工程运维.md#5080上下文容量实测)。
- 固定128K检查：停服后运行`Test-ModelContexts128K.cmd`，逐个实测已注册模型，通过的自动备份设置128K，失败的保留原窗口并报错；见[运维](docs/正式工程运维.md#固定128k测试并设置)。
- 本机OpenCode：使用自己加密保存的AiServer API key，运行 `OpenCode-AiServer.cmd`，或桌面的“OpenCode - AiServer”；本机连接、密钥设置与文件工具说明见[运维](docs/正式工程运维.md#本机opencode执行端)。
- C#脚本评测：本机运行 `scripts/validation/Benchmark-CSharp.py`，5080运行 `scripts/validation/Collect-CSharpBenchmark.py`；客户端只生成Markdown报告，服务端只采集数据，0.9.4计数资源升级、双模型角色计划与普通／thinking方案和两端命令见[运维](docs/正式工程运维.md#单文件c脚本评测)。
- 服务端命令行：

```powershell
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' '.\scripts\server\Run.py'
```

入口优先读取 `config/server.local.json`，缺省使用 `config/server.ollama.example.json`。API默认 `http://127.0.0.1:18080`，公网地址为 `https://szzj.nat200.top`；部署与验证边界见[项目进度](docs/项目进度.md)。

## 按任务查阅

| 任务 | 入口 |
|---|---|
| 启动、账号、诊断、备份、5080更新 | [正式工程运维](docs/正式工程运维.md) |
| 已实现内容、验证结果、待验证事项 | [项目进度](docs/项目进度.md) |
| 初版开发、联调与部署中的问题和经验 | [初版开发与部署复盘](docs/初版开发与部署复盘.md) |
| 理解软件架构、代码设计、关键调用链与维护方法 | [代码与软件工程导读](docs/代码与软件工程导读.md) |
| 业务背景资料、知识库与按需加载的讨论结论 | [工程导读7.5](docs/代码与软件工程导读.md#75-业务资料与知识库接入方向)；初测状态见[项目进度](docs/项目进度.md#背景资料压缩候选筛选) |
| API、认证机制及OpenCode接入 | [接口文档](contracts/api/v1/README.md) |
| 服务端模块与本地回归 | [服务端说明](server/README.md) |
| 第三方组件与上游技术资料 | [第三方引用](docs/第三方引用.md) |

客户端接入交付副本只在服务端接口、认证、模型能力字段、SSE／错误语义等需要客户端改代码的内容发生变化时同步；部署、模型性能和验证记录不触发该文档更新。

新会话读本文件后按任务进入对应模块，无需通读其他文档。

## 源码地图

| 目录 | 职责与主要入口 |
|---|---|
| `server/src/aiserver/` | `app.py`装配API；`api/`校验与SSE；`jobs/scheduler.py`串行调度；`storage/`持久化与迁移 |
| 服务端 `inference/`、`context/`、`operations/` | Ollama适配、消息冻结与容量估算、引擎／隧道托管、诊断与key鉴权 |
| `scripts/client/` | OpenCode启动、独立密钥加密保存、连接配置与真实本地文件工具检查 |
| `server/tests/` | 服务端本地回归工具，不随正式程序分发 |
| `contracts/api/v1/`、`contracts/examples/` | API文档、机器Schema、公开请求／响应与OpenCode配置样例 |
| `config/` | 公开服务／推理模板、模型身份锁；`*.local.json`为私有配置 |
| `scripts/server/`、`scripts/deployment/`、`scripts/validation/` | 正式启动／管理、白名单打包／开机任务、离线契约检查 |
| `scripts/inference/` | 锁定引擎与模型的首次准备工具，按需使用 |
| `.local/` | 忽略的本机OpenCode、模型、凭据、运行数据与生成产物，不是源码入口 |

本工程保持单服务进程、单生成位；默认9B／27B，可按需追加GPT-OSS与两款Gemma，均先开放文字与工具调用。上下文、输出和思考控制从models接口读取。保守容量估算与实际token计数分别记录，超限明确拒绝。
