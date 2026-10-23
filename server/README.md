# A-1 / A-2 本机 Fake 原型

A 工作目录：`D:\github\AiServer-A`，分支 `line-a`。本轮按服务端语言的优先建议采用 Python＋FastAPI；这是原型工程选择，未改写主方案的用户确认状态。C# 控制台 Demo 使用现有 .NET 8 SDK，不是 .NET Framework 4.7.2 宿主插件。

实现：真实 FastAPI／SQLite WAL、单消费循环、用户令牌、事务幂等、状态／结果／查回／列表／停止、重启恢复标记、PNG 验证与原字节 hash、正文过期。配置只允许 Development/Fake；没有接入 Ollama、NATAPP 或真实宿主。

## 运行

依赖已在 AGENTS 指定解释器中存在，本轮没有安装包。观察到的版本见 requirements.lock.txt，测试依赖见 requirements-dev.lock.txt。全新环境重建尚未实测。

在客户端终端初始化测试用户并把令牌留在进程环境，不输出到报告：

```powershell
Set-Location 'D:\github\AiServer-A'
$env:PYTHONPATH = 'D:\github\AiServer-A\server\src'
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' -m aiserver user-add tester
$env:AISERVER_TOKEN = & 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' -m aiserver token-create tester
```

另一终端运行 API（默认 127.0.0.1:18080，单进程，禁用代理头与访问日志）：

```powershell
Set-Location 'D:\github\AiServer-A'
& '.\scripts\server\Run-Fake.ps1'
```

回到客户端终端：

```powershell
& '.\scripts\validation\Build-Demo.ps1'
$env:AISERVER_BASE_URL = 'http://127.0.0.1:18080'
dotnet '.\tools\ClientDemo\bin\Debug\net8.0\ClientDemo.dll' ready
dotnet '.\tools\ClientDemo\bin\Debug\net8.0\ClientDemo.dll' submit '.\contracts\examples\text-request.json'
dotnet '.\tools\ClientDemo\bin\Debug\net8.0\ClientDemo.dll' submit '.\contracts\examples\image-request.json'
dotnet '.\tools\ClientDemo\bin\Debug\net8.0\ClientDemo.dll' resume '416417b5-e936-4cde-b0df-969281936318'
```

Demo 在网络提交前把原请求写入 .local/client/requests，终态结果写入 results，按服务提示轮询，查询失败按 3/6/12/30 秒退避并遵守 Retry-After。提交为 120 秒、查询为 15 秒。提交响应丢失先用原编号查回；未查到才重试同一请求。返回脚本不会执行。

停止：`cancel <jobId>`；开发验证可用 `submit <file> --cancel-on-running`。修改输入或主动重做必须使用新 clientRequestId。复用样例编号意味着查回原任务。

可选配置：复制 config/server.example.json 到本地配置，修改路径后通过 `Run-Fake.ps1 -ConfigPath <绝对路径>` 加载；已知设置允许 AISERVER_大写字段名覆盖。不热重载，不启动第二个 worker。默认数据目录根据源码位置解析，不依赖当前目录。

本地管理：`user-add`、`token-create`、`token-revoke <keyId>`、`user-disable <userId>`、`recovery-confirm --note <说明>`。后者必须停止 API、归档残留并核实旧资源后执行；Fake 通过不代表真实 GPU 已停止。维护命令不启动 HTTP。

## 复验

```powershell
Set-Location 'D:\github\AiServer-A\server'
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' -m pytest -q --basetemp 'D:\github\AiServer-A\.local\pytest' --junitxml='D:\github\AiServer-A\.local\reports\G0-G2-tests.xml'
Set-Location 'D:\github\AiServer-A'
& 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' '.\scripts\validation\Smoke-Fake.py'
```

smoke 在随机回环端口运行真实 HTTP、临时 SQLite 和编译后的 C# Demo；令牌仅在内存环境传递，结束后关闭服务。实际结果见 docs/验证记录/G0-G2-A-Fake-20261023.md。

## B 接入点与后续限制

公共提交为 `272b22b`：contracts/api/v1、contracts/examples、inference/contracts.py。Budget 暴露已验证 accepted_input_types，prepare 返回可保存的精确 PreparedInput；Provider 使用同一材料并传播取消。ready 取 profile 与预算器能力的交集。真实适配可包装已有 PreparedPrompt/Generation；未验证输入能力为空，返回 INPUT_BUDGET_UNVERIFIED。

A-4 在 create_app 的 profile/budget/provider 唯一装配点接入 B，再验证身份、预算、图文映射与真实停止。当前配置拒绝非 Fake，不能改 provider 字符串就宣称真实接通。

A-3 待补：完整竞争屏障矩阵、磁盘／结果保存故障、WAL 备份及迁移、清理边界、日志轮转、请求限流、真实上传时限、NATAPP 和 Windows 托管。运行中不会自动恢复失败推理，也不会自动执行返回内容。当前可供本机研发验证，尚未达到公网持续内测条件。
