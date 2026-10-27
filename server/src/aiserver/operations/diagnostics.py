"""Allowlisted local startup diagnostics; never echo private configuration values."""
import json
import socket
import sqlite3

PROBLEMS = {
    "QWEN35_BUDGET_UPGRADE_REQUIRED": "9B需要升级完整提示计数。停服后运行scripts/inference/Repair-InferenceRuntime.py --server-config 原服务JSON，再启动；不改变权重、KV或窗口。",
    "TOKENIZER_MISSING_OR_INVALID": "固定tokenizer缺失或摘要不符；停服后运行scripts/inference/Repair-InferenceRuntime.py --server-config 原服务JSON补齐9B计数资源。",
    "DATA_DIRECTORY_IN_USE": "数据目录正被旧服务或维护操作占用。请从原控制台或计划任务停止它，不要删除instance.lock文件。",
    "API_PORT_IN_USE": "API端口已被占用。请先停止原服务；若确需换端口，须同步NATAPP转发配置。",
    "ENGINE_PORT_IN_USE": "Ollama端口已有实例。请检查托盘Ollama或旧模型启动器，停止确认属于旧部署的实例后重试。",
    "ENGINE_EXECUTABLE_MISSING": "推理配置的ExecutablePath不存在。检查server.local.json的inference_config，以及该推理文件内的Ollama程序路径。",
    "ENGINE_PROCESS_EXITED": "Ollama在启动阶段退出。请检查该主机引擎依赖、运行权限和数据目录logs/engine.log。",
    "ENGINE_STARTUP_TIMEOUT": "Ollama启动超时。请检查推理配置的BaseUrl、引擎程序及logs/engine.log。",
    "MODEL_IDENTITY_CHECK_FAILED": "模型身份校验失败。检查推理配置中的引擎版本、模型名、模型目录及模型锁是否匹配，不能仅通过改锁绕过校验。",
    "FULL_GPU_REQUIRED": "完整上下文未能确认全GPU驻留，服务已停止。请检查显存、NumCtx／NumBatch和engine.log；不会自动降低窗口或接受CPU卸载。",
    "STARTUP_ACCESS_DENIED": "启动账户无权读取程序／配置或写入数据目录。请检查5080上的路径和运行账户权限。",
    "STARTUP_FILE_MISSING": "启动所需文件缺失。检查推理配置、模型锁、契约JSON和SQL迁移文件是否已复制到对应路径。",
    "STORAGE_INIT_FAILED": "数据库初始化／迁移失败。检查数据库权限、完整性及迁移前备份，保留原库，不要删除后重建。",
    "STORAGE_SCHEMA_UNSUPPORTED": "数据库schema版本不被当前源码支持。请核对复制的服务版本；不要删除或重建原数据库。",
    "INFERENCE_PROFILE_INVALID": "推理配置的上下文／输出／预留预算或单执行位参数不兼容，请检查inference_config指向的文件。",
    "PROCESS_JOB_FAILED": "Windows进程树托管失败，请检查运行账户权限及外层任务／进程管理限制。",
    "DEPENDENCY_MISSING": "所选Python缺少启动依赖。请在维护页为该Python安装server/requirements.lock.txt。",
    "CONFIG_INVALID_JSON": "JSON配置无法解析。请检查服务／推理配置及模型锁的JSON格式。",
    "CONFIG_FIELDS_INVALID": "配置字段或取值不兼容，请检查下方字段名，保留原数据与模型路径。",
    "STARTUP_FAILED": "启动阶段失败，请按显示的阶段检查本机状态；没有足够证据判定必须修改server.local.json。",
}
STAGES = {"data-lock":"数据独占锁", "logging":"日志目录", "storage":"数据库初始化／迁移",
          "engine":"Ollama启动／模型身份校验", "scheduler":"任务调度恢复",
          "api-bind":"API监听端口", "configuration":"服务配置／推理资料"}
EXACT = {
    "QWEN35_BUDGET_UPGRADE_REQUIRED":"QWEN35_BUDGET_UPGRADE_REQUIRED",
    "TOKENIZER_MISSING_OR_INVALID":"TOKENIZER_MISSING_OR_INVALID",
    "TOKENIZER_MISSING_OR_INVALID: tokenizer identity mismatch":"TOKENIZER_MISSING_OR_INVALID",
    "Data directory already has an active instance":"DATA_DIRECTORY_IN_USE",
    "Engine port is occupied by an unmanaged instance":"ENGINE_PORT_IN_USE",
    "Engine executable missing":"ENGINE_EXECUTABLE_MISSING",
    "Managed engine exited during startup":"ENGINE_PROCESS_EXITED",
    "Managed engine startup timed out":"ENGINE_STARTUP_TIMEOUT",
    "MODEL_IDENTITY_CHECK_FAILED":"MODEL_IDENTITY_CHECK_FAILED",
    "FULL_GPU_REQUIRED":"FULL_GPU_REQUIRED",
    "Unsupported schema":"STORAGE_SCHEMA_UNSUPPORTED",
    "Invalid context/output partition":"INFERENCE_PROFILE_INVALID",
    "This scheduler requires a single engine execution slot":"INFERENCE_PROFILE_INVALID",
    "Models must share the same Ollama process configuration":"INFERENCE_PROFILE_INVALID",
    "Duplicate model id":"INFERENCE_PROFILE_INVALID",
    "Cannot establish managed engine job":"PROCESS_JOB_FAILED",
    "Cannot attach engine to managed job":"PROCESS_JOB_FAILED",
}


class StartupFailure(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic["code"])


def diagnostic(error, phase="configuration"):
    if isinstance(error, StartupFailure):
        return error.diagnostic
    chain=[]
    current=error
    for _ in range(8):
        if current is None or any(current is previous for previous in chain): break
        chain.append(current)
        current=current.__cause__ or current.__context__
    code=next((EXACT[str(item)] for item in chain if str(item) in EXACT),None)
    if code is None:
        if any(isinstance(item,PermissionError) for item in chain): code="STARTUP_ACCESS_DENIED"
        elif any(isinstance(item,ModuleNotFoundError) for item in chain): code="DEPENDENCY_MISSING"
        elif any(isinstance(item,FileNotFoundError) for item in chain): code="STARTUP_FILE_MISSING"
        elif any(isinstance(item,sqlite3.Error) for item in chain): code="STORAGE_INIT_FAILED"
        elif any(isinstance(item,json.JSONDecodeError) for item in chain): code="CONFIG_INVALID_JSON"
        elif type(error).__name__=="ValidationError": code="CONFIG_FIELDS_INVALID"
        else: code="STARTUP_FAILED"
    result={"code":code,"phase":phase,"stage":STAGES.get(phase,phase),"message":PROBLEMS[code],
            "errorType":type(error).__name__}
    if code=="CONFIG_FIELDS_INVALID" and hasattr(error,"errors"):
        # Field names are useful; omit raw input and validation messages.
        result["fields"]=list(dict.fromkeys(".".join(map(str,item["loc"])) for item in error.errors(include_input=False)))[:20]
    return result


def require_api_port(port):
    with socket.socket() as probe:
        try:
            if hasattr(socket,"SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            probe.bind(("127.0.0.1",port))
        except OSError as error:
            if error.errno in (98,48,10048) or getattr(error,"winerror",None)==10048:
                raise StartupFailure({"code":"API_PORT_IN_USE","phase":"api-bind","stage":STAGES["api-bind"],
                                      "message":PROBLEMS["API_PORT_IN_USE"],"errorType":type(error).__name__}) from error
            raise
