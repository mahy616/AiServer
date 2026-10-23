# AiServer 服务

正式实现已统一到 `D:\github\AiServer`。运行与部署以 [正式工程运维](../docs/正式工程运维.md) 为唯一操作说明，实际进度见 [项目进度](../docs/项目进度.md)。

- Python 3.12，FastAPI、单 Uvicorn worker、SQLite WAL/FULL、单消费循环与一个生成执行位。
- `app.create_app` 装配 API；`jobs/scheduler.py` 管任务和执行位；`storage/db.py` 管事务、幂等、归属和生命周期。
- `inference/runtime.py` 装配身份锁、材料准备、保守容量和 Ollama；`operations/engine.py` 用 Windows Job Object 托管模型进程树，取消后先重启收尾再继续。
- `context/prompt_builder.py` 冻结实际图文消息，`context/input_budget.py` 只提供标明方法的估算；真实计数来自响应。
- `operations/observability.py` 管元数据日志轮转和限流；公开 API 不提供系统管理操作。
- `inference/fake.py` 仅开发测试使用，Production/Fake 启动被拒绝。

```powershell
Set-Location 'D:\github\AiServer\server'
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' -m pytest -q
```

部署包使用 `scripts/deployment/Build-Release.py`；Windows 开机任务使用 `Install-StartupTask.ps1`。新环境重建、目标 GPU、无人登录开机和外网验收必须在对应环境执行，不能由本机测试结果推定。
