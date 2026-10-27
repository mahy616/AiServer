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
    instructions = (
        "AiServer " + version + " 精简服务端更新\n\n"
        "只复制payload里面的文件／目录到5080工程根目录，保持原目录合并覆盖。\n"
        "payload是完整程序更新，不需要上一份更新包；它不包含模型、引擎、私有配置、数据库、日志或缓存。\n"
        "第一次部署仍需准备Python、已有模型和引擎，以及该主机自己的私有配置／NATAPP。\n\n"
        "1. 停止旧服务／连接器。升级前备份原源码、私有配置和数据库。\n"
        "   如需界面备份，可先仅复制ServerManager.cmd与scripts/server下ServerManager.py、ManagerActions.py、manager_support.py，\n"
        "   双击ServerManager.cmd，选本机Python／原配置，加载后在维护页备份；此步骤不迁移旧库。\n"
        "2. 关闭管理窗口，合并覆盖payload。旧密码模块／登录Schema与登录样例按manifest.retiredFiles清除；不删除私有配置。\n"
        "   先保持停服运行Remove-DeepSeek.cmd，完成后再启动管理器；详细清理／备份／回退见运维文档0.9.6章节。\n"
        "   0.9.7已注册Qwen3.8的部署保持停服，按运维文档0.9.7章节执行--qwen38-output 16384容量调整。\n"
        "3. 双击工程根ServerManager.cmd。若依赖清单与目标机上次保存的manifest哈希相同，可跳过重装；\n"
        "   0.7新增tokenizers／gguf依赖；0.7.1的Coder入口会检查并在缺少时自动补齐，也可先在维护页安装。\n"
        "   选择本机NATAPP路径，按需填写HTTPS公网入口；留空时不做公网检查。备份并保存。\n"
        "4. 首次启动／账号管理前自动备份v1–v5旧库并升级到schema v6；账号、既有key、任务与性能记录保留。\n"
        "   不再提供密码登录／设置与logout，管理器改为创建账号、签发／查看／撤销API key；旧login_*配置被忽略。\n"
        "   在运行监控页查看活跃账号、队列、耗时和硬件资源；性能指标保留7天，正文仍按原配置清理。\n"
        "   服务器公网自检不代表另一外网电脑验收；5080模型能力仍需实机核对。\n"
        "   OpenCode使用OpenAI兼容入口，接入见contracts/api/v1/README.md第10节；本机启动与密钥设置见运维文档。\n"
        "   本包包含OpenCode接入脚本更新，不安装OpenCode程序或复制密钥；执行端也须更新这些脚本。\n"
        "   切换Coder：停服、补齐依赖后双击Install-Qwen25Coder7B.cmd，首次下载约5.5GB；\n"
        "   成功后从原管理器启动，OpenCode配置见contracts/examples/opencode.example.json。\n"
        "   80K／128K、显存调节及回退步骤只维护在docs/正式工程运维.md的Coder章节。\n"
        "   0.8新增14B入口：Install-Qwen25Coder14B.cmd，下载约9GB Q4_K_M，默认80K／Q4 KV，\n"
        "   切换前核对全GPU驻留，失败保留原服务配置；详细运行／测试／回退步骤见运维文档14B章节。\n"
        "   0.9.1按窗口和本次输出计算输入预算，旧MaxInputTokens不再生效；保持全GPU要求。\n"
        "   更新后重启AiServer与本机OpenCode入口以刷新能力；压缩恢复与手工配置见运维文档动态上下文章节。\n"
        "   0.9.2提供Enable-CoderModels.cmd、Install-Qwen35.cmd和Compare-Models.cmd；\n"
        "   7B／14B切换、9B准备／对比／回退只维护在运维文档多模型章节。\n"
        "   新增Qwen3.8-27B可用Install-Qwen38.cmd，安装及回退统一见运维文档Qwen3.8章节。\n"
        "   0.9.4停服后先运行scripts/inference/Repair-InferenceRuntime.py --install-dependencies，补齐9B完整提示计数。\n"
        "   它下载固定tokenizer并备份升级Budget，不改变模型权重、KV、窗口及采样；两端简单复测步骤统一见运维文档。\n"
        "   0.9.5取消旧采样及Think覆盖，正文／思考独立保留；Coder模板不变，输出限额超出时明确拒绝。\n"
        "   旧Temperature／TopP／TopK／Seed／Think／PresencePenalty字段不再生效；主动默认值改用GenerationDefaults。\n"
        "   Qwen3.8公开默认64K；已有部署停服后可用Repair-InferenceRuntime.py --qwen38-context 65536备份调整实际窗口。\n"
        "   0.9.6覆盖后保持停服，先运行Remove-DeepSeek.cmd，备份并移除DeepSeek注册、原始／工具版模型与旧入口。\n"
        "   0.9.8的C#评测默认按角色运行：7B／14B简单、9B简单／中等、Qwen3.8中等／困难；普通失败后才尝试thinking。\n"
        "   旧版6组简单双模式校准使用--profile calibration --thinking both；工具模板修复与独立读写检查见运维文档。\n"
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
