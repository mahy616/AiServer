# AI C# 脚本助手

为流程图式视觉检测软件提供自然语言生成、修改、解释 C# 脚本的服务。当前交付为 Python＋FastAPI＋SQLite 服务与 C# .NET 8 控制台 Demo，模型使用本机 Ollama／Qwen3-VL 4B。

**唯一主工程：`D:\github\AiServer`，当前分支 `codex/production`。** A 线代码已合入，B 线模型与修改已保留；`D:\github\AiServer-A` 仅作历史保留。后续在同一工程中开发、测试和发布，不再分别推进 A/B。

## 文档入口

| 文档 | 内容 |
|---|---|
| [主方案](docs/主方案.md) | 已确认范围、架构和重要决策 |
| [项目进度](docs/项目进度.md) | 真实交付、证据与尚未完成项 |
| [正式工程运维](docs/正式工程运维.md) | 启动、Demo、凭据、容量、备份、发布及 5080 部署 |
| [实施规划](docs/实施规划.md) | 五阶段推进与代码边界 |
| [业务契约](contracts/api/v1/README.md) | 唯一 `/api/v1` HTTP/JSON 契约 |
| [跨会话协作](docs/跨会话协作.md) | 后续维护规则 |

## 启动

```powershell
Set-Location 'D:\github\AiServer'
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' '.\scripts\server\Run.py'
```

正式入口读取 `config/server.local.json`，缺省回到 `config/server.ollama.example.json`；API 托管唯一 Ollama，不要同时启动旧模型脚本。默认 API 为 `http://127.0.0.1:18080`。首次其他机器部署须先准备 profile 指定的引擎与权重，详见运维文档。

`server/src/aiserver` 为服务；`tools/ClientDemo` 为解耦的通信／文件／命令入口；`contracts` 为协议；`config` 为非私有模板与锁；`scripts` 为启动和交付；`eval` 为历史能力材料；`.local` 为忽略的私有运行状态。

普通请求使用保守容量估算，超限明确拒绝，禁止引擎自动裁剪／移位；实际 token 与估算分别记录。生成成功不代表 SDK、编译或业务验证通过。SDK 和完整客户端后续接入。

原 [A 线](docs/实现线A-客户端服务端与公网.md)、[B 线](docs/实现线B-模型能力与推理.md) 与 `docs/专项` 保留为历史设计和证据，实际运行以当前源码、契约和运维文档为准。
