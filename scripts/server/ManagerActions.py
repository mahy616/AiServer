"""Private stdio bridge; newly issued keys only leave through the requested result."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import threading
import http.client
import hashlib
import subprocess
import os
import socket

from manager_support import ROOT, backup_database, child_environment, inspect_config, offline, read_config, save_config

sys.path.insert(0,str(ROOT / "server/src"))
_emit_lock = threading.Lock()


def perform(action, value):
    config = str(Path(value["config"]).resolve())
    if action != "inspect" and value.get("expectedHash"):
        if hashlib.sha256(read_config(config)[1]).hexdigest() != value["expectedHash"]:
            raise ValueError("配置已被其他操作修改，请重新加载后再操作。")
    if action == "inspect":
        return inspect_config(ROOT,config)
    if action == "save-config":
        return save_config(ROOT,config,value["fields"],value["sha256"])
    if action == "backup":
        return backup_database(ROOT,config)
    if action == "install-dependencies":
        values,_ = read_config(config)
        with offline(ROOT,values):
            result = subprocess.run([sys.executable,"-m","pip","install","-r",str(ROOT/"server/requirements.lock.txt")],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,cwd=ROOT,env=child_environment(ROOT),timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        if result.returncode:
            raise ValueError("依赖安装失败，请检查该Python环境和网络；原模型不会改变。")
        return {"installed":True}
    if action == "probe":
        description = inspect_config(ROOT,config)
        connection = http.client.HTTPConnection("127.0.0.1",description["summary"]["port"],timeout=3)
        try:
            connection.request("GET","/api/v1/health")
            response = connection.getresponse()
            response.read(4096)
            return {"message":"本机API有响应（HTTP " + str(response.status) + "）；模型就绪与公网连通需分别核实。"}
        except OSError:
            return {"message":"本机API未响应。如服务由其他入口启动，请从原窗口或计划任务检查／停止。"}
        finally:
            connection.close()
    if action == "startup-check":
        return startup_check(config)
    if action == "monitor":
        from aiserver.settings import Settings
        from aiserver.operations.monitoring import read_snapshot, query_history
        settings = Settings.load(config)
        result = read_snapshot(settings)
        try:
            result["history"] = query_history(settings.data_directory/"jobs.db",value.get("filters"))
        except ValueError as error:
            result["history"] = None
            result["history_error"] = str(error) if any("\u4e00"<=char<="\u9fff" for char in str(error)) else "筛选条件无效。"
        except (OSError,sqlite3.Error):
            result["history"] = None
            result["history_error"] = "尚无可读的监控历史，或数据库暂时不可用。"
        return result
    from aiserver.settings import Settings
    from aiserver.storage.db import Store
    from aiserver.operations.lock import InstanceLock
    settings = Settings.load(config)
    store = Store(settings)
    lock = InstanceLock(settings.data_directory).acquire() if store.needs_migration() else None
    try:
        store.initialize()
        if action == "users":
            return {"users":store.users()}
        username = value["username"]
        if action == "create-user":
            store.user_add(username,value.get("displayName") or username)
        elif action == "token-create":
            return {"apiKey":store.token_create(username,value.get("expiresAtMs")), "username":username}
        elif action == "token-list":
            return {"keys":store.tokens(username), "username":username}
        elif action == "token-revoke":
            if not store.token_revoke(value["keyId"],username):
                raise ValueError("TOKEN_NOT_FOUND")
            return {"keys":store.tokens(username), "username":username}
        elif action == "enable":
            store.user_enable(username)
        elif action == "disable":
            store.user_disable(username)
        else:
            raise ValueError("未知操作。")
        return {"users":store.users()}
    finally:
        if lock:
            lock.release()


def describe_error(error):
    from aiserver.operations.diagnostics import StartupFailure, diagnostic
    if isinstance(error,StartupFailure):
        problem=error.diagnostic
        return f'[{problem["code"]}] 阶段：{problem["stage"]}。{problem["message"]}'
    if type(error).__name__=="ValidationError":
        problem=diagnostic(error)
        return problem["message"]+" 字段："+", ".join(problem.get("fields",[]))
    messages = {
        "USER_ALREADY_EXISTS":"账号已存在，可为该账号签发新的API key。", "USER_NOT_FOUND":"账号不存在，请刷新列表。",
        "USER_DISABLED":"账号已禁用，请启用后再签发API key。",
        "TOKEN_NOT_FOUND":"该账号下没有此API key，请刷新列表。",
        "TOKEN_EXPIRY_INVALID":"API key到期时间必须晚于当前时间。",
        "ACCOUNT_FIELDS_INVALID":"账号应为1–128个字符，显示名不超过256个字符，均不能为空白。",
        "Data directory already has an active instance":"服务正在运行或有其他维护操作。请先从原入口停止服务，再执行备份、迁移或保存配置。",
        "Unsupported schema":"数据库版本与当前源码不匹配，请检查更新文件。",
        "NATAPP_EXECUTABLE_MISSING":"NATAPP程序不存在，请在基础配置页选择本机已有的程序。",
        "NATAPP_CONFIG_MISSING":"NATAPP私有INI不存在，请在基础配置页选择该主机的INI。",
        "NATAPP_CONFIG_INVALID":"NATAPP私有INI格式不正确，请检查UTF-8编码及default节。",
        "NATAPP_TOKEN_NOT_CONFIGURED":"请在该主机NATAPP私有INI中配置authtoken。",
        "NATAPP_LOG_MUST_BE_NONE":"请将该主机NATAPP私有INI的log设为none。",
        "NATAPP_UNMANAGED_INSTANCE":"发现独立运行的NATAPP，请从原窗口或计划任务停止后再启动。",
        "SERVICE_STARTUP_TIMEOUT":"API或模型未在启动期限内就绪，请检查本机模型和端口。",
        "API_STARTUP_FAILED":"API启动失败，请检查端口、数据锁和模型配置。",
    }
    if isinstance(error,ModuleNotFoundError):
        return "当前Python缺少依赖，请在维护页安装服务端依赖。"
    if isinstance(error,FileNotFoundError):
        return "找不到所选文件，请检查本机路径。"
    if isinstance(error,PermissionError):
        return "当前账户无权读取配置或写入数据目录。"
    if isinstance(error,sqlite3.Error):
        return "数据库操作失败，请检查权限、锁或数据库完整性；未重建原库。"
    if str(error) in messages:
        return messages[str(error)]
    if type(error) is ValueError and len(str(error)) < 150 and any("\u4e00" <= c <= "\u9fff" for c in str(error)):
        return str(error)
    return "操作失败，请检查所选配置、依赖及本机状态。"


def emit(kind, **fields):
    with _emit_lock:
        print(json.dumps({"event":kind,**fields},ensure_ascii=False),flush=True)


def startup_check(config):
    from aiserver.settings import Settings
    from aiserver.operations.diagnostics import diagnostic, require_api_port
    from aiserver.operations.lock import InstanceLock
    settings=Settings.load(config)
    checks=[]
    def add(ok,code,message): checks.append({"ok":ok,"code":code,"message":message})
    add(True,"CONFIG_VALID","服务JSON及字段校验通过；未修改配置。")
    try:
        require_api_port(settings.port)
        add(True,"API_PORT_FREE",f"本机API端口 {settings.port} 当前可绑定。")
    except Exception as error:
        problem=diagnostic(error,"api-bind")
        add(False,problem["code"],problem["message"])
    lock=InstanceLock(settings.data_directory)
    try:
        lock.acquire()
        add(True,"DATA_LOCK_FREE","数据独占锁可获取；未初始化或迁移数据库。")
    except Exception as error:
        problem=diagnostic(error,"data-lock")
        add(False,problem["code"],problem["message"])
    finally:
        lock.release()
    for name in ("job-request.schema.json","openai-chat-request.schema.json"):
        exists=(settings.contract_directory/name).is_file()
        add(exists,"CONTRACT_FILE",name+(" 已存在。" if exists else " 缺失，请检查contract_directory及覆盖文件。"))
    database=settings.data_directory/"jobs.db"
    if database.exists():
        try:
            from contextlib import closing
            with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=2)) as current:
                version=current.execute("PRAGMA user_version").fetchone()[0]
                integrity=current.execute("PRAGMA quick_check").fetchone()[0]
            add(integrity=="ok" and version in (0,1,2,3,4,5,6),"DATABASE_CHECK",f"数据库schema v{version}，完整性检查："+("通过。" if integrity=="ok" else "失败。"))
        except sqlite3.Error:
            add(False,"STORAGE_INIT_FAILED","无法只读检查现有数据库，请检查权限和锁。")
    if settings.provider=="Ollama":
        try:
            from aiserver.inference.registry import load_models
            from aiserver.settings import ROOT as source_root
            from urllib.parse import urlsplit
            entries=load_models(settings)
            cfg=entries[0][1]
            add(True,"INFERENCE_PROFILE_VALID",f"{len(entries)}份推理配置与模型锁可读取，容量及单引擎配置校验通过；没有启动模型。")
            for key,label,kind in (("ExecutablePath","Ollama程序","file"),("ModelsPath","模型存储目录","directory")):
                path=(source_root/cfg[key]).resolve()
                exists=path.is_file() if kind=="file" else path.is_dir()
                add(exists,"INFERENCE_PATH",f"{key}（{label}）：{path}"+("，程序文件检查通过。" if exists and kind=="file" else "，目录检查通过。" if exists else "，不存在。"))
            uri=urlsplit(cfg["BaseUrl"])
            with socket.socket() as probe:
                probe.settimeout(1)
                occupied=probe.connect_ex((uri.hostname,uri.port))==0
            if occupied:
                from aiserver.operations.processes import engine_port_problem
                message = engine_port_problem(uri.port,source_root/".local/managed-engine/process.json")["message"]
                if not settings.manage_engine: message += "当前配置使用外部引擎，启动时仍需校验身份。"
            else: message = f"Ollama端口 {uri.port} 当前没有响应实例；模型子进程不单独视为端口冲突。"
            add(not(occupied and settings.manage_engine),"ENGINE_PORT_IN_USE" if occupied else "ENGINE_PORT_FREE",message)
        except Exception as error:
            problem=diagnostic(error,"configuration")
            add(False,problem["code"],problem["message"])
    elif settings.provider == "Strata":
        try:
            from aiserver.inference.strata import load_strata_profile, path_of
            from urllib.parse import urlsplit
            cfg, identity, profile = load_strata_profile(settings)
            add(True, "INFERENCE_PROFILE_VALID", "Strata模型锁、运行配置和文件检查通过；允许CPU／内存协同，未启动真实模型。")
            for key in ("ExecutablePath", "EnginePath", "RunConfigPath"):
                add(path_of(cfg[key]).is_file(), "INFERENCE_PATH", key + "：" + str(path_of(cfg[key])))
            uri = urlsplit(cfg["BaseUrl"])
            with socket.socket() as probe:
                probe.settimeout(1)
                occupied = probe.connect_ex((uri.hostname, uri.port)) == 0
            add(not occupied, "ENGINE_PORT_IN_USE" if occupied else "ENGINE_PORT_FREE",
                "Strata端口已占用，请从原入口停止实例。" if occupied else f'Strata端口空闲；{cfg["Model"]}冷加载允许{cfg["StartupTimeoutSeconds"]}秒。')
        except Exception as error:
            problem = diagnostic(error, "configuration")
            add(False, problem["code"], problem["message"])
    return {"ok":all(item["ok"] for item in checks),"checks":checks}


async def host(value):
    import uvicorn
    from aiserver.app import create_app
    from aiserver.settings import Settings
    from aiserver.operations.service import serve
    from aiserver.operations.tunnel import ManagedTunnel
    from aiserver.operations.diagnostics import require_api_port
    if value.get("expectedHash") and hashlib.sha256(read_config(value["config"])[1]).hexdigest()!=value["expectedHash"]:
        raise ValueError("配置已被其他操作修改，请重新加载后再启动。")
    settings = Settings.load(value["config"])
    require_api_port(settings.port)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=settings.port,workers=1,
        access_log=False,proxy_headers=False,timeout_graceful_shutdown=45,ws="none",log_level="critical"))
    loop = asyncio.get_running_loop()
    tunnel = None
    def tunnel_factory(*args):
        nonlocal tunnel
        tunnel = ManagedTunnel(*args)
        return tunnel
    def listen():
        # EOF means the owning UI disappeared; request graceful stop.
        for line in sys.stdin:
            if line.strip() == "stop":
                break
        try:
            loop.call_soon_threadsafe(setattr,server,"should_exit",True)
        except RuntimeError:
            pass
    threading.Thread(target=listen,daemon=True).start()
    async def status():
        while True:
            scheduler = app.state.scheduler
            emit("state",started=server.started,scheduler=scheduler.state,
                 tunnel=tunnel.state if tunnel else "disabled", publicConnectivity=app.state.monitor.public)
            await asyncio.sleep(1)
    polling = asyncio.create_task(status())
    try:
        await serve(settings,app=app,server=server,tunnel_factory=tunnel_factory,
                    report=lambda message:emit("log",message=message))
    finally:
        polling.cancel()
        await asyncio.gather(polling,return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action",choices=["inspect","save-config","backup","probe","startup-check","install-dependencies","users","create-user",
                                         "token-create","token-list","token-revoke","enable","disable","serve","monitor"])
    args = parser.parse_args()
    try:
        value = json.loads(sys.stdin.readline())
        if args.action == "serve":
            asyncio.run(host(value))
        else:
            print(json.dumps({"ok":True,"result":perform(args.action,value)},ensure_ascii=False),flush=True)
        return 0
    except (KeyboardInterrupt,EOFError):
        return 130
    except Exception as error:
        if args.action == "serve":
            from aiserver.operations.diagnostics import StartupFailure, diagnostic
            problem=diagnostic(error)
            message=describe_error(error)
            if not isinstance(error,StartupFailure):
                message=f'[{problem["code"]}] 阶段：{problem["stage"]}。'+message
            emit("error",message=message,code=problem["code"],stage=problem["stage"])
        else:
            print(json.dumps({"ok":False,"error":describe_error(error)},ensure_ascii=False),flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
