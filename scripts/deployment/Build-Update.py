"""Source-only complete server updates from an explicit allowlist. Never walk .local."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("update_inventory", Path(__file__).with_name("List-UpdateFiles.py"))
inventory_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory_module)


def build(root=ROOT, output=None):
    root = Path(root).resolve()
    output = Path(output or root/".local/releases").resolve()
    rows = inventory_module.inventory(root,include_reference=False)
    version = tomllib.loads((root/"server/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    snapshot=[]
    for row in rows:
        data=(root/row["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest()!=row["sha256"]:
            raise ValueError("Source changed while preparing update; build again.")
        snapshot.append((row["path"],data))
    content_hash=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder=output/("AiServer-update-"+version+"-"+stamp+"-"+content_hash[:8])
    archive=folder.parent/(folder.name+".zip")
    if folder.exists() or archive.exists():
        raise FileExistsError("Update already exists; old builds are never overwritten.")
    revision="unavailable"; dirty=None
    try:
        revision=subprocess.check_output(["git","rev-parse","--short","HEAD"],cwd=root,stderr=subprocess.DEVNULL,text=True).strip()
        dirty=bool(subprocess.check_output(["git","status","--porcelain"],cwd=root,stderr=subprocess.DEVNULL))
    except (OSError,subprocess.CalledProcessError):
        pass
    manifest={"formatVersion":1,"kind":"complete-server-source-update","version":version,
              "createdAt":datetime.now(timezone.utc).isoformat(),"revision":revision,"workingTreeDirty":dirty,
              "schemaVersion":6,"sourceBytes":sum(len(data) for _,data in snapshot),"fileCount":len(rows),
              "requirementsSha256":hashlib.sha256((root/"server/requirements.lock.txt").read_bytes()).hexdigest(),
              "snapshotSha256":content_hash,"files":rows,
              "excluded":["models","engines","private-configuration","database","credentials","logs","caches","opencode-runtime"],
              "retiredFiles":["server/src/aiserver/operations/passwords.py",
                              "contracts/api/v1/auth-login-request.schema.json",
                              "contracts/examples/login-request.json","contracts/examples/login-response.json"]}
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 6):
        manifest["retiredFiles"] += json.loads((root / "scripts/deployment/deepseek-retired-files.json").read_text(encoding="utf-8"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 11):
        manifest["retiredFiles"] += json.loads((root / "scripts/deployment/coder-retired-files.json").read_text(encoding="utf-8"))
    instructions = (
        "AiServer " + version + " 精简服务端更新\n\n"
        "只复制payload里面的文件／目录到5080工程根目录，保持原目录合并覆盖。\n"
        "payload是完整程序更新，不需要上一份更新包；它不包含模型、引擎、私有配置、数据库、日志或缓存。\n"
        "第一次部署仍需准备Python、已有模型和引擎，以及该主机自己的私有配置／NATAPP。\n\n"
        "1. 停止旧服务／连接器。升级前备份原源码、私有配置和数据库。\n"
        "   如需界面备份，可先仅复制ServerManager.cmd与scripts/server下ServerManager.py、ManagerActions.py、manager_support.py，\n"
        "   双击ServerManager.cmd，选本机Python／原配置，加载后在维护页备份；此步骤不迁移旧库。\n"
        "2. 关闭管理窗口，合并覆盖payload。旧密码模块／登录Schema与登录样例按manifest.retiredFiles清除；不删除私有配置。\n"
        "   已有9B／27B保持现有配置与模型顺序；新增三模型的一键下载、注册和回退见运维0.9.12章节。\n"
        "   Use-9B27B.cmd是主动收敛为双模型的旧维护入口，普通升级不要求执行。\n"
        "   Tune-ModelContexts.cmd与Test-ModelContexts128K.cmd仅用于另外实测和重新调优，会修改上述推荐窗口，按需使用。\n"
        "   容量搜索、报告、复核与回退统一见运维文档5080上下文容量实测章节；未实测不宣称已扩容。\n"
        "3. 双击工程根ServerManager.cmd。若依赖清单与目标机上次保存的manifest哈希相同，可跳过重装；\n"
        "   依赖以server/requirements.lock.txt为准，需要时在维护页安装。\n"
        "   选择本机NATAPP路径，按需填写HTTPS公网入口；留空时不做公网检查。备份并保存。\n"
        "4. 首次启动／账号管理前自动备份v1–v5旧库并升级到schema v6；账号、既有key、任务与性能记录保留。\n"
        "   不再提供密码登录／设置与logout，管理器改为创建账号、签发／查看／撤销API key；旧login_*配置被忽略。\n"
        "   在运行监控页查看活跃账号、队列、耗时和硬件资源；性能指标保留7天，正文仍按原配置清理。\n"
        "   服务器公网自检不代表另一外网电脑验收；5080模型能力仍需实机核对。\n"
        "   客户端只使用AiServer个人API key；输入完整ais_密钥，不加Bearer前缀，由SDK发送Bearer请求头。\n"
        "   认证约定见contracts/api/v1/README.md第3节，OpenCode配置见第10节；本机设置与检查见运维文档。\n"
        "   本包包含OpenCode接入脚本更新，不安装OpenCode程序或复制密钥；执行端也须更新这些脚本。\n"
        "   Qwen3.8公开默认80K；已有部署复制payload后，保持停服双击Set-Qwen38-80K.cmd，只备份调整实际注册27B窗口，保留9B、输出和其他调优。\n"
        "   旧DeepSeek清理为独立维护操作，具体备份／回退见运维文档。\n"
        "   C#评测默认按角色运行：9B简单／中等、Qwen3.8中等／困难；普通失败后才尝试thinking。\n"
        "   双模型简单双模式校准使用--profile calibration --thinking both；独立读写检查见运维文档。\n"
        "   5080采集、客户端启动／续跑／结果提供与回退统一见docs/正式工程运维.md的单文件C#脚本评测章节。\n"
        "5. 把manifest.json留作这次版本记录；有schema迁移时，回退需旧源码＋原配置＋升级前库备份。\n\n"
        "manifest包含每个文件的SHA256。不要把开发机.local或server.local.json复制到5080。\n")
    folder.mkdir(parents=True,exist_ok=False)
    for relative,data in snapshot:
        target=folder/"payload"/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
    (folder/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (folder/"UPDATE.txt").write_text(instructions,encoding="utf-8-sig")
    with zipfile.ZipFile(archive,"x",zipfile.ZIP_DEFLATED,compresslevel=9) as packed:
        for path in sorted(folder.rglob("*")):
            if path.is_file(): packed.write(path,path.relative_to(folder).as_posix())
    with zipfile.ZipFile(archive) as packed:
        expected={path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}
        if set(packed.namelist())!=expected or packed.testzip() is not None:
            raise ValueError("Archive verification failed")
        for row in rows:
            if hashlib.sha256(packed.read("payload/"+row["path"])).hexdigest()!=row["sha256"]:
                raise ValueError("Payload checksum verification failed")
    return {"folder":str(folder),"payload":str(folder/"payload"),"zip":str(archive),
            "files":len(rows),"sourceBytes":manifest["sourceBytes"],"zipBytes":archive.stat().st_size}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--pause",action="store_true",help="Keep a double-click console open")
    args=parser.parse_args()
    try:
        result=build(output=args.output)
        print(f'Complete source update: {result["files"]} files, {result["sourceBytes"]/1024:.1f} KiB')
        print(f'Compressed update: {result["zipBytes"]/1024:.1f} KiB')
        print("Folder: "+result["folder"])
        print("ZIP: "+result["zip"])
        print("No models, private configuration or runtime data included.")
    finally:
        if args.pause: input("Press Enter to close...")


if __name__=="__main__":
    main()
