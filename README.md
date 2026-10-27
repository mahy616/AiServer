# AiServer

代码推理服务与OpenCode接入工具。客户端统一使用OpenCode，5080运行AiServer／Ollama／模型；服务端使用Python／FastAPI／SQLite，负责鉴权、模型能力、排队、生成、停止、结果与监控。

OpenCode在本机组织项目文件、用户需求、会话历史和工具结果，通过OpenAI兼容Chat Completions接口调用AiServer。模型返回工具调用后，OpenCode在本机读取／编辑文件或执行命令，再将结果发给模型；5080不直接访问本机项目磁盘。

OpenCode使用AiServer签发的独立API key，本机密钥加密保存。服务端原生HTTP／SSE协议与旧模型配置保留兼容；API及OpenCode接入见接口文档，多模型准备见运维文档。

## 使用入口

- 服务管理：双击根目录 `ServerManager.cmd`，选择本机Python和私有配置，加载后管理服务、账号、配置与备份；“运行监控”查看活跃账号、任务性能与硬件资源。
- 服务更新包：双击 `BuildUpdate.cmd`，生成仅含程序的完整小包；0.9.6覆盖后停服运行`Remove-DeepSeek.cmd`清理DeepSeek，步骤见[运维](docs/正式工程运维.md#096-移除deepseek与四模型评测)。
- 切换Coder：更新并补齐依赖后运行 `Install-Qwen25Coder7B.cmd`，默认80K窗口／8K最大输出，输入按完整窗口预算校验、Q5权重＋Q8 KV；准备、128K调整与回退见[运维](docs/正式工程运维.md#切换qwen25-coder-7b07)。
- 5080试用14B：`Install-Qwen25Coder14B.cmd`准备Q4_K_M权重／Q4 KV，校验80K窗口全GPU驻留后切换；安装、容量检查和回退见[运维](docs/正式工程运维.md#5080试用qwen25-coder-14b08)。
- 多模型选择与9B对比：`Enable-CoderModels.cmd`注册已准备的7B／14B，`Install-Qwen35.cmd`追加官方Qwen3.5-9B，`Compare-Models.cmd`比较已准备模型；操作和测试范围见[运维](docs/正式工程运维.md#多模型选择与qwen35-9b092)。
- 5080增加Qwen3.8：双击`Install-Qwen38.cmd`下载固定版本并全GPU预检；步骤见[运维](docs/正式工程运维.md#5080增加qwen38-27b)，已有部署的16K输出调整与思考模式见[升级说明](docs/正式工程运维.md#097-输出预算与思考模式)。
- 已下载8B导入：双击 `Install-Qwen3VL8B.cmd`，保留原4B并自动注册 `qwen3-vl:8b`；交付与前提见[运维](docs/正式工程运维.md#一键导入已下载的-qwen3-vl8b)。
- 本机OpenCode：使用自己加密保存的AiServer API key，运行 `OpenCode-AiServer.cmd`，或桌面的“OpenCode - AiServer”；本机连接、密钥设置与文件工具说明见[运维](docs/正式工程运维.md#本机opencode执行端)。
- C#脚本评测：本机运行 `scripts/validation/Benchmark-CSharp.py`，5080运行 `scripts/validation/Collect-CSharpBenchmark.py`；客户端只生成Markdown报告，服务端只采集数据，0.9.4计数资源升级、四模型普通／thinking共12题的方案和两端命令见[运维](docs/正式工程运维.md#单文件c脚本评测)。
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

本工程保持单服务进程、单生成位；图片数量和上下文限制从models接口读取。开发模板为单图，5080四图仍需实机验证。保守容量估算与实际token计数分别记录，超限明确拒绝。
