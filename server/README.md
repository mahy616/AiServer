# AiServer 服务端

Python 3.12／FastAPI／SQLite通用图文推理服务。版本以[pyproject.toml](pyproject.toml)为准。运行和更新见[正式工程运维](../docs/正式工程运维.md)，对接见[公共协议](../contracts/api/v1/README.md)。

## 模块定位

- `app.py:create_app`：API、生命周期、认证、模型查询和任务接口。
- `api/body.py`、`validation.py`、`streaming.py`：有界JSON／图片校验、纯ASGI SSE、心跳和断线取消。
- `api/openai.py`：/v1/models和/v1/chat/completions，OpenAI请求／响应、工具调用及结果回传；复用认证与持久队列。
- `jobs/scheduler.py`、`storage/db.py`：单生成位、幂等／归属／取消、输出先保存再发布、正文清理；SQL按版本迁移并提前备份。
- `context/prompt_builder.py:prepare_messages`、`input_budget.py`：保留客户端角色和图文顺序，冻结请求并估算容量，不注入业务提示词。
- `context/coder_budget.py`：Coder实际模板只渲染＋锁定分词器预算，包含工具；预算与引擎实际usage分开。模型准备／切换入口和调节步骤见运维的Coder章节。
- `inference/runtime.py`、`ollama.py`：冻结推理配置，校验身份，读取有界NDJSON，区分实际计数与估算。
- `inference/strata.py`：单个固定Strata模型的本机Chat Completions／SSE适配、工具历史、思考、身份与窗口检查，以及托管进程的停止恢复；与Ollama在启动时二选一。
- `inference/registry.py`：多个独立锁定模型共享一个Ollama进程与生成位，按model路由到各自配置与容量检查。
- `operations/service.py`、`engine.py`、`tunnel.py`、`processes.py`：统一托管API／模型／NATAPP，锁与进程身份、Windows Job树收尾、连接器退避重启。
- `storage/db.py:token_create/tokens/token_revoke/authenticate_details`：账号与API key管理；schema v6在备份旧库后增加正文／思考分块通道，保留账号、key与任务。历史v5密码移除迁移继续保留。`operations/diagnostics.py`负责启动前检查与安全错误报告。
- `operations/monitoring.py`：7天性能历史只读查询、带身份的本地实时快照、后台Windows／GPU采集与HTTPS健康自检；不提供公网管理接口。
- 根目录 `scripts/server/Run.py`、`Manage.py`、`ServerManager.py`：正式入口、管理员CLI、本机Tk管理界面。

Fake用于自动化测试，Production禁用。兼容旧配置的 `system_prompt` 字段不参与请求；历史业务提示词和评估路径已移除。

## 本地检查

使用临时库、Fake、模拟引擎／隧道、受控辅助进程和本机回环，不连接真实模型或公网：

```powershell
Set-Location 'D:\github\AiServer'
New-Item -ItemType Directory -Path '.local\test-runs' -Force | Out-Null
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' -m pytest -q server/tests --basetemp ('D:\github\AiServer\.local\test-runs\' + [guid]::NewGuid().ToString())
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' scripts/validation/Check-Contracts.py
```

依赖以 `requirements.lock.txt`／`requirements-dev.lock.txt` 为准。日常打包使用根目录 `BuildUpdate.cmd`，不包含测试、OpenCode程序／会话、私有配置或模型。
