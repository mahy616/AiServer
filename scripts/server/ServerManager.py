"""Local Windows desktop administration. No public management HTTP endpoints."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from manager_support import ROOT, DEFAULT_PYTHON, atomic_write, child_environment

sys.path.insert(0,str(ROOT / "server/src"))

TASK_CAPTIONS={"queued":"排队中","running":"生成中","cancelling":"取消中","succeeded":"成功","failed":"失败","cancelled":"已取消"}
SCHEDULER_CAPTIONS={"starting":"启动中","available":"模型已就绪","busy":"正在处理任务","recovering":"模型恢复中",
    "draining":"任务收尾中","paused":"服务已暂停","stopping":"停止中"}


class ServerManager:
    def __init__(self, window, *, preferences_file=None, auto_load=True):
        self.window = window
        self.window.title("AiServer 服务端管理")
        self.window.geometry("1180x880")
        self.window.minsize(980,780)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.monitor_executor = ThreadPoolExecutor(max_workers=1)
        self.monitor_pending = self.external_running = False
        self.monitor_generation = 0
        self.monitor_filters = {"page":1}
        self.monitor_rows = {}
        self.event_cursor = (None,0)
        self.last_observed_ms = None
        self.mailbox = queue.Queue()
        self.child = self.job = None
        self.busy = self.stopping = self.closing = False
        self.loaded = None
        self.loaded_path = None
        self.action_buttons = []
        self.launcher_file = Path(preferences_file) if preferences_file else ROOT / ".local/server-manager/launcher.json"
        preferences = {}
        try:
            preferences = json.loads(self.launcher_file.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            pass
        self.python = tk.StringVar(value=preferences.get("python",sys.executable or DEFAULT_PYTHON))
        self.config = tk.StringVar(value=preferences.get("config",str(ROOT / "config/server.local.json")))
        self.feedback = tk.StringVar(value="请选择当前服务器的Python和私有配置，点击“加载配置”。")
        self.status = tk.StringVar(value="本窗口尚未启动服务")
        self.tunnel_status = tk.StringVar(value="NATAPP：未启动；公网入口：尚未检测")
        self.summary = tk.StringVar(value="尚未加载配置")
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.window.option_add("*Font",("Microsoft YaHei UI",10))
        shell = ttk.Frame(window,padding=18)
        shell.pack(fill="both",expand=True)
        ttk.Label(shell,text="AiServer 服务端管理",font=("Microsoft YaHei UI",19,"bold")).pack(anchor="w")
        ttk.Label(shell,text="在本机管理服务、账号和配置。模型与私有凭据继续使用该服务器现有文件。",foreground="#536171").pack(anchor="w",pady=(5,12))
        paths = ttk.LabelFrame(shell,text="本机运行环境",padding=10)
        paths.pack(fill="x")
        paths.columnconfigure(1,weight=1)
        ttk.Label(paths,text="工程目录").grid(row=0,column=0,sticky="w",padx=(0,12),pady=4)
        ttk.Label(paths,text=str(ROOT)).grid(row=0,column=1,columnspan=2,sticky="w")
        self.path_widgets = []
        for row,label,variable in ((1,"Python程序",self.python),(2,"服务配置",self.config)):
            ttk.Label(paths,text=label).grid(row=row,column=0,sticky="w",pady=4)
            entry=ttk.Entry(paths,textvariable=variable)
            entry.grid(row=row,column=1,sticky="ew",pady=4)
            button=ttk.Button(paths,text="选择…",command=lambda v=variable:self.browse(v))
            button.grid(row=row,column=2,padx=(8,0))
            self.path_widgets.extend([entry,button])
        self.load_button=ttk.Button(paths,text="加载配置 / 记住路径",command=self.load_config)
        self.load_button.grid(row=3,column=1,sticky="w",pady=(8,0))
        self.path_widgets.append(self.load_button)
        backend_choices = ttk.Frame(paths)
        backend_choices.grid(row=4, column=1, sticky="w", pady=(6,0))
        for backend, label in (("Ollama", "Ollama配置"), ("Strata", "Strata Qwen"), ("DeepSeek", "Strata DeepSeek")):
            choice = ttk.Button(backend_choices, text=label, command=lambda name=backend: self.choose_backend(name))
            choice.pack(side="left", padx=(0,8))
            self.path_widgets.append(choice)
        self.tabs=ttk.Notebook(shell)
        self.tabs.pack(fill="both",expand=True,pady=(14,10))
        self.build_service()
        self.build_monitor()
        self.build_users()
        self.build_config()
        self.build_maintenance()
        ttk.Label(shell,textvariable=self.feedback,foreground="#37485e",wraplength=1000).pack(fill="x",anchor="w")
        self.window.protocol("WM_DELETE_WINDOW",self.close)
        self.window.after(100,self.drain)
        self.window.after(2000,self.poll_monitor)
        if auto_load and Path(self.config.get()).is_file():
            self.window.after(180,self.load_config)

    def choose_backend(self, backend):
        filename = {"Strata": "server.strata.local.json", "DeepSeek": "server.strata.deepseek.local.json", "Ollama": "server.local.json"}[backend]
        path = ROOT / "config" / filename
        if not path.is_file():
            self.feedback.set("请先停服运行Install-Strata-DeepSeek.cmd。" if backend == "DeepSeek" else
                              "请先停服运行Install-Strata.cmd准备Qwen。" if backend == "Strata" else "请先选择原Ollama私有服务配置。")
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            self.feedback.set("配置无法读取，请检查JSON文件。")
            return
        if value.get("provider") != ("Strata" if backend == "DeepSeek" else backend):
            self.feedback.set("所选默认文件不是" + backend + "配置，请手工选择正确的服务JSON。")
            return
        self.config.set(str(path))
        self.load_config()

    def button(self,parent,text,command):
        button=ttk.Button(parent,text=text,command=command)
        self.action_buttons.append(button)
        return button

    def build_service(self):
        page=ttk.Frame(self.tabs,padding=16); self.tabs.add(page,text="  服务控制  ")
        ttk.Label(page,textvariable=self.status,font=("Microsoft YaHei UI",14,"bold")).pack(anchor="w",pady=(0,6))
        ttk.Label(page,textvariable=self.tunnel_status).pack(anchor="w",pady=(0,10))
        actions=ttk.Frame(page); actions.pack(fill="x")
        self.start_button=self.button(actions,"启动服务",self.start_service)
        self.start_button.pack(side="left",padx=(0,8))
        self.stop_button=ttk.Button(actions,text="停止本窗口启动的服务",command=self.stop_service,state="disabled")
        self.stop_button.pack(side="left",padx=(0,8))
        self.button(actions,"检查本机API",self.probe).pack(side="left")
        self.button(actions,"启动前检查",self.startup_check).pack(side="left",padx=(8,0))
        ttk.Label(page,text="启动顺序：API / 模型就绪 → 后台NATAPP。关闭管理窗口时，会先停止本窗口启动的服务。",wraplength=940).pack(anchor="w",pady=(10,8))
        self.logs=ScrolledText(page,height=12,wrap="word",font=("Consolas",10),state="disabled")
        self.logs.pack(fill="both",expand=True)

    @staticmethod
    def seconds(value):
        return "—" if value is None else f"{value/1000:.2f}s"

    @staticmethod
    def timestamp(value):
        return datetime.fromtimestamp(value/1000).strftime("%m-%d %H:%M:%S") if value is not None else "—"

    @staticmethod
    def number(value, suffix=""):
        return "暂不可用" if value is None else f"{value:.1f}{suffix}"

    def build_monitor(self):
        page=ttk.Frame(self.tabs,padding=12); self.tabs.add(page,text="  运行监控  ")
        self.monitor_status=tk.StringVar(value="加载配置后每2秒刷新；尚无实时数据。")
        self.monitor_counts=tk.StringVar(value="活跃账号 —  |  生成账号 —  |  排队账号 —  |  未完成任务 —")
        self.monitor_resources=tk.StringVar(value="CPU／内存／GPU：暂不可用")
        self.monitor_accounts=tk.StringVar(value="活跃：近5分钟有认证访问，或仍有未完成任务的账号。")
        for variable in (self.monitor_status,self.monitor_counts,self.monitor_resources,self.monitor_accounts):
            if variable is self.monitor_resources:
                self.resource_text=ScrolledText(page,height=3,wrap="word",font=("Microsoft YaHei UI",10),state="disabled")
                self.resource_text.pack(fill="x",pady=(0,4))
                self.monitor_resources.trace_add("write",lambda *args:self.render_resources())
                self.render_resources()
            else:
                ttk.Label(page,textvariable=variable,wraplength=1080).pack(anchor="w",pady=(0,4))
        filters=ttk.Frame(page); filters.pack(fill="x",pady=5)
        self.filter_vars={key:tk.StringVar() for key in ("user","status","from","to")}
        self.filter_slow=tk.BooleanVar(value=False)
        for column,(key,label,width) in enumerate((("user","账号",15),("status","状态",12),("from","开始日期",12),("to","结束日期",12))):
            ttk.Label(filters,text=label).grid(row=0,column=column*2,padx=(0,5))
            if key=="status":
                entry=ttk.Combobox(filters,textvariable=self.filter_vars[key],width=width,state="readonly",
                    values=("",*TASK_CAPTIONS.values()))
            else: entry=ttk.Entry(filters,textvariable=self.filter_vars[key],width=width)
            entry.grid(row=0,column=column*2+1,padx=(0,10))
        actions=ttk.Frame(page); actions.pack(fill="x",pady=(0,5))
        ttk.Checkbutton(actions,text="总耗时超过20秒",variable=self.filter_slow).pack(side="left",padx=(0,10))
        ttk.Button(actions,text="查询 / 刷新",command=self.search_monitor).pack(side="left",padx=(0,10))
        ttk.Label(actions,text="日期 YYYY-MM-DD；按提交日期筛选，终态指标保留7天。").pack(side="left")
        self.monitor_summary=tk.StringVar(value="尚无统计")
        ttk.Label(page,textvariable=self.monitor_summary).pack(anchor="w",pady=(0,5))
        table=ttk.Frame(page); table.pack(fill="both",expand=True)
        table.rowconfigure(0,weight=1); table.columnconfigure(0,weight=1)
        columns=("user","job","status","created","total","first","queue","tokens","speed","error")
        self.tasks=ttk.Treeview(table,columns=columns,show="headings",selectmode="browse",height=5)
        for key,label,width in (("user","账号",135),("job","任务编号",275),("status","状态",95),("created","提交时间",120),
                ("total","总耗时",85),("first","首段等待",85),("queue","排队",85),("tokens","输入 / 输出token",135),
                ("speed","token/s",75),("error","错误码",170)):
            self.tasks.heading(key,text=label); self.tasks.column(key,width=width,stretch=False)
        self.tasks.grid(row=0,column=0,sticky="nsew")
        scroll=ttk.Scrollbar(table,orient="horizontal",command=self.tasks.xview)
        scroll.grid(row=1,column=0,sticky="ew"); self.tasks.configure(xscrollcommand=scroll.set)
        vertical=ttk.Scrollbar(table,orient="vertical",command=self.tasks.yview)
        vertical.grid(row=0,column=1,sticky="ns"); self.tasks.configure(yscrollcommand=vertical.set)
        self.tasks.bind("<<TreeviewSelect>>",lambda event:self.task_details())
        pagination=ttk.Frame(page); pagination.pack(fill="x",pady=5)
        ttk.Button(pagination,text="上一页",command=lambda:self.monitor_page(-1)).pack(side="left")
        self.page_caption=tk.StringVar(value="第1 / 1页")
        ttk.Label(pagination,textvariable=self.page_caption).pack(side="left",padx=10)
        ttk.Button(pagination,text="下一页",command=lambda:self.monitor_page(1)).pack(side="left")
        self.task_detail=tk.StringVar(value="选择任务查看接收准备、执行及模型内部耗时；缺失数据用 — 表示。")
        ttk.Label(page,textvariable=self.task_detail,wraplength=1080).pack(anchor="w")

    def task_details(self):
        selected=self.tasks.selection()
        if not selected or selected[0] not in self.monitor_rows: return
        row=self.monitor_rows[selected[0]]
        timings="；".join(label+" "+self.seconds(row.get(key)) for key,label in (
            ("prepare_ms","接收准备"),("queue_ms","排队"),("execution_ms","执行"),("first_output_ms","首段等待"),
            ("load_ms","模型加载"),("prompt_eval_ms","输入处理"),("eval_ms","生成"),("engine_total_ms","引擎总计")))
        note="旧任务总耗时从入队起算。" if row["total_origin"]=="accepted" else "服务端总耗时从收到请求起算。"
        self.task_detail.set(timings+"\n"+note+"首段等待与模型内部耗时可能重叠；不含客户端渲染和完整网络传输。")

    def render_resources(self):
        self.resource_text.configure(state="normal")
        self.resource_text.delete("1.0","end")
        self.resource_text.insert("1.0",self.monitor_resources.get())
        self.resource_text.configure(state="disabled")

    def search_monitor(self):
        self.monitor_filters={key:value.get().strip() for key,value in self.filter_vars.items()}
        self.monitor_filters["status"]={caption:code for code,caption in TASK_CAPTIONS.items()}.get(self.monitor_filters["status"],"")
        self.monitor_filters.update(slow=self.filter_slow.get(),page=1)
        self.request_monitor()

    def monitor_page(self,offset):
        self.monitor_filters["page"]=max(1,self.monitor_filters.get("page",1)+offset)
        self.request_monitor()

    def poll_monitor(self):
        self.request_monitor()
        if not self.closing: self.window.after(2000,self.poll_monitor)

    def request_monitor(self):
        if self.monitor_pending or self.closing or not self.loaded: return
        try: python,config=self.input_paths()
        except ValueError: return
        if config!=self.loaded_path: return
        self.monitor_pending=True
        generation=self.monitor_generation
        filters=dict(self.monitor_filters)
        def work():
            try:
                process=subprocess.run([python,"-u",str(ROOT/"scripts/server/ManagerActions.py"),"monitor"],
                    input=json.dumps({"config":config,"filters":filters})+"\n",capture_output=True,text=True,encoding="utf-8",
                    env=child_environment(ROOT),cwd=ROOT,timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
                value=json.loads(process.stdout)
                result=value["result"] if value.get("ok") else {"error":value.get("error","监控暂不可用。")}
            except Exception: result={"error":"监控读取暂不可用，请检查Python环境及配置。"}
            self.mailbox.put(("monitor",generation,filters,result))
        self.monitor_executor.submit(work)

    def public_caption(self,value):
        captions={"unconfigured":"未配置公网检查地址","pending":"等待检查","reachable":"服务器自检可达","unreachable":"服务器自检未通过"}
        caption=captions.get(value.get("state"),"尚未检测")
        if value.get("checked_at_ms"):
            caption+="（"+self.timestamp(value["checked_at_ms"])+"；"+self.seconds(value.get("latency_ms"))+"）"
        if value.get("reason"):
            reason=value["reason"]
            caption+=" "+{"TIMEOUT":"检查超时","NETWORK_OR_TLS_ERROR":"网络或证书错误",
                "INVALID_HEALTH_RESPONSE":"健康响应内容不符","CHECK_UNAVAILABLE":"检查暂不可用"}.get(reason,reason)
        return caption

    def apply_monitor(self,result):
        if "error" in result:
            self.mark_monitor_stale(result["error"]); return
        snapshot=result.get("snapshot") if result.get("fresh") else None
        if snapshot:
            self.last_observed_ms=snapshot["observed_at_ms"]
            self.external_running=not self.child and snapshot["scheduler"]!="stopping"
            owner="本窗口托管" if self.child else "外部入口启动；仅查看，请从原入口停止"
            self.monitor_status.set(f'本机API：'+("已就绪" if snapshot["local_ready"] else "未就绪")+
                f'；模型：{SCHEDULER_CAPTIONS.get(snapshot["scheduler"],snapshot["scheduler"])}；{owner}；更新 {self.timestamp(snapshot["observed_at_ms"])}')
            self.monitor_counts.set(f'活跃账号 {snapshot["active_users"]}  |  生成账号 {snapshot["generating_users"]}  |  排队账号 {snapshot["waiting_users"]}  |  排队任务 {snapshot["waiting_tasks"]}  |  未完成任务 {snapshot["active_tasks"]}')
            accounts=snapshot["active_accounts"]
            names="、".join(name[:32]+("…" if len(name)>32 else "") for name in accounts[:10])
            self.monitor_accounts.set("近5分钟访问或有未完成任务的账号："+(names or "无")+(f" 等{len(accounts)}个账号" if len(accounts)>10 else ""))
            resources=snapshot["resources"]
            used,total=resources["memory_used_bytes"],resources["memory_total_bytes"]
            memory=f"{used/1073741824:.1f} / {total/1073741824:.1f} GiB" if used is not None and total else "暂不可用"
            gpus=[f'GPU {gpu["index"]} {gpu["name"]}：{self.number(gpu["utilization_percent"],"%")}，显存 {self.number(gpu["memory_used_mib"])} / {self.number(gpu["memory_total_mib"])} MiB' for gpu in resources["gpus"]]
            self.monitor_resources.set("CPU "+self.number(resources["cpu_percent"],"%")+"；内存 "+memory+"\n"+("；".join(gpus) or "GPU／显存：暂不可用"))
            self.tunnel_status.set("NATAPP："+snapshot["tunnel"]+"；公网入口："+self.public_caption(snapshot["public"]))
            identity=(snapshot["pid"],snapshot["identity"]["created"])
            cursor=self.event_cursor[1] if self.event_cursor[0]==identity else 0
            if not self.child:
                for item in snapshot["events"]:
                    if item["sequence"]>cursor: self.log(item["message"])
            self.event_cursor=(identity,max([cursor]+[item["sequence"] for item in snapshot["events"]]))
        else: self.mark_monitor_stale("实时数据已过期或服务尚未运行（超过10秒／进程身份不匹配）。")
        history=result.get("history")
        if history is not None:
            selected=self.tasks.selection()
            self.monitor_rows={item["job_id"]:item for item in history["items"]}
            self.tasks.delete(*self.tasks.get_children())
            for row in history["items"]:
                tokens=" / ".join(str(row.get(key)) if row.get(key) is not None else "—" for key in ("input_tokens","output_tokens"))
                self.tasks.insert("","end",iid=row["job_id"],values=(row["user_id"],row["job_id"],TASK_CAPTIONS.get(row["status"],row["status"]),
                    self.timestamp(row["created_at_ms"]),self.seconds(row["total_ms"]),self.seconds(row["first_output_ms"]),self.seconds(row["queue_ms"]),
                    tokens,"—" if row["tokens_per_second"] is None else f'{row["tokens_per_second"]:.1f}',row["error_code"] or "—"))
            if selected and selected[0] in self.monitor_rows:
                self.tasks.selection_set(selected[0]); self.task_details()
            else: self.task_detail.set("选择任务查看阶段耗时；旧任务未知指标显示 —。")
            summary=history["summary"]
            self.monitor_summary.set(f'共 {summary["total"]} 项；成功 {summary["succeeded"]} / 失败 {summary["failed"]} / 取消 {summary["cancelled"]}；成功平均 {self.seconds(summary["average_ms"])} / P95 {self.seconds(summary["p95_ms"])}')
            self.monitor_filters["page"]=history["page"]
            self.page_caption.set(f'第 {history["page"]} / {history["pages"]} 页（每页50项）')
        else:
            self.monitor_summary.set(result.get("history_error","监控历史暂不可用。"))
            self.tasks.delete(*self.tasks.get_children()); self.monitor_rows={}
        self.update_buttons()

    def mark_monitor_stale(self,message):
        self.last_observed_ms=None
        self.external_running=False
        self.monitor_status.set(message)
        self.monitor_counts.set("活跃账号 —  |  生成账号 —  |  排队账号 —  |  未完成任务 —")
        self.monitor_resources.set("CPU／内存／GPU：实时数据暂不可用")
        self.monitor_accounts.set("活跃：近5分钟有认证访问，或仍有未完成任务的账号。")
        if not self.child: self.tunnel_status.set("NATAPP／公网入口：实时数据已过期或尚未运行")
        self.update_buttons()

    def build_users(self):
        page=ttk.Frame(self.tabs,padding=16); self.tabs.add(page,text="  账号与API key  ")
        actions=ttk.Frame(page); actions.pack(fill="x",pady=(0,12))
        for label,command in (("刷新用户",self.refresh_users),("创建账号",self.account_dialog),
                              ("签发API key",self.issue_key),("查看／撤销key",self.manage_keys),
                              ("启用账号",lambda:self.set_enabled(True)),("禁用账号",lambda:self.set_enabled(False))):
            self.button(actions,label,command).pack(side="left",padx=(0,8))
        self.users=ttk.Treeview(page,columns=("id","name","enabled"),show="headings",selectmode="browse",height=10)
        for key,title,width in (("id","账号",260),("name","显示名称",340),("enabled","状态",120)):
            self.users.heading(key,text=title); self.users.column(key,width=width,anchor="w")
        self.users.pack(fill="both",expand=True)
        ttk.Label(page,text="创建账号后签发API key。完整key仅在签发时显示；列表只显示编号、时间和撤销状态。禁用账号会阻止全部key访问并取消活动任务。",wraplength=940).pack(anchor="w",pady=(10,0))

    def build_config(self):
        page=ttk.Frame(self.tabs,padding=16); self.tabs.add(page,text="  基础配置  ")
        page.columnconfigure(1,weight=1)
        ttk.Label(page,textvariable=self.summary,wraplength=940,foreground="#536171").grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,12))
        self.fields={"manage_natapp":tk.BooleanVar(value=True),"natapp_executable":tk.StringVar(),
                     "natapp_config":tk.StringVar(),
                     "payload_days":tk.StringVar(value="1"),"max_request_bytes":tk.StringVar(value="20971520"),
                     "public_base_url":tk.StringVar()}
        ttk.Checkbutton(page,text="服务启动后统一管理NATAPP",variable=self.fields["manage_natapp"]).grid(row=1,column=0,columnspan=3,sticky="w",pady=5)
        for row,key,label in ((2,"natapp_executable","NATAPP程序"),(3,"natapp_config","NATAPP私有INI"),
                              (4,"payload_days","正文保留天数"),
                              (5,"max_request_bytes","请求大小上限（字节）"),(6,"public_base_url","HTTPS公网入口（可留空）")):
            ttk.Label(page,text=label).grid(row=row,column=0,sticky="w",pady=6,padx=(0,12))
            ttk.Entry(page,textvariable=self.fields[key]).grid(row=row,column=1,sticky="ew",pady=6)
            if row in (2,3):
                self.button(page,"选择…",lambda k=key:self.browse(self.fields[k])).grid(row=row,column=2,padx=(8,0))
        actions=ttk.Frame(page); actions.grid(row=8,column=0,columnspan=3,sticky="w",pady=12)
        self.button(actions,"填入升级推荐值",self.recommended).pack(side="left",padx=(0,10))
        self.button(actions,"备份原配置并保存",self.save_config).pack(side="left")
        ttk.Label(page,text="仅保存上面6项；端口、数据目录、模型配置、allowed_hosts和其他原字段保持。\n公网入口例：https://szzj.nat200.top；不含路径或令牌。留空时不发起公网检查。\n保存前须停止服务；原JSON会另存为同目录.bak，服务重启后生效。",wraplength=920).grid(row=9,column=0,columnspan=3,sticky="w")

    def build_maintenance(self):
        page=ttk.Frame(self.tabs,padding=16); self.tabs.add(page,text="  维护与备份  ")
        ttk.Label(page,text="升级前备份",font=("Microsoft YaHei UI",12,"bold")).pack(anchor="w")
        ttk.Label(page,text="服务停止后，直接对当前数据库做一致性备份并包含WAL。支持旧版本，不会先升级或创建空库。",wraplength=940).pack(anchor="w",pady=(6,10))
        self.button(page,"备份当前数据库",self.backup).pack(anchor="w")
        ttk.Separator(page).pack(fill="x",pady=18)
        ttk.Label(page,text="Python依赖",font=("Microsoft YaHei UI",12,"bold")).pack(anchor="w")
        ttk.Label(page,text="使用上方选择的Python安装server/requirements.lock.txt；只安装服务依赖，不下载模型。",wraplength=940).pack(anchor="w",pady=(6,10))
        self.button(page,"安装 / 补齐服务依赖",self.install_dependencies).pack(anchor="w")
        self.button(page,"打开本机备份目录",self.open_backups).pack(anchor="w",pady=(15,0))

    def browse(self,variable):
        current=Path(variable.get().strip('"'))
        selected=filedialog.askopenfilename(parent=self.window,title="选择本机文件",initialdir=str(current.parent) if current.parent.is_dir() else str(ROOT))
        if selected: variable.set(selected)

    def input_paths(self):
        python=Path(self.python.get().strip().strip('"'))
        config=Path(self.config.get().strip().strip('"'))
        if not python.is_absolute() or not python.is_file(): raise ValueError("请选择存在的Python程序绝对路径。")
        if not config.is_absolute() or not config.is_file(): raise ValueError("请选择5080已有的服务私有配置JSON。")
        return str(python),str(config.resolve())

    def invoke(self,action,values,callback,message):
        try:
            python,config=self.input_paths()
        except ValueError as error:
            messagebox.showerror("检查路径",str(error),parent=self.window); return
        expected = self.loaded["sha256"] if self.loaded and self.loaded_path == config else None
        def work():
            process=subprocess.run([python,"-u",str(ROOT/"scripts/server/ManagerActions.py"),action],
                input=json.dumps({"config":config,"expectedHash":expected,**values},ensure_ascii=False)+"\n",capture_output=True,
                text=True,encoding="utf-8",env=child_environment(ROOT),cwd=ROOT,timeout=630 if action=="install-dependencies" else 90,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            try: result=json.loads(process.stdout)
            except ValueError: raise ValueError("操作程序未能运行，请检查Python版本和依赖；未显示原始内容以保护私有配置。") from None
            if not result.get("ok"): raise ValueError(result.get("error","操作失败。"))
            return result["result"]
        self.background(work,callback,message)

    def background(self,work,callback,message):
        if self.busy: return
        self.busy=True; self.feedback.set(message); self.update_buttons()
        future=self.executor.submit(work)
        def completed(result):
            try: self.mailbox.put(("done",callback,result.result()))
            except Exception as error: self.mailbox.put(("failed",str(error)))
        future.add_done_callback(completed)

    def update_buttons(self):
        state="disabled" if self.busy or self.closing else "normal"
        for button in self.action_buttons: button.configure(state=state)
        self.start_button.configure(state="disabled" if self.busy or self.closing or self.child or self.external_running else "normal")
        for widget in self.path_widgets:
            widget.configure(state="disabled" if self.busy or self.child else "normal")
        self.stop_button.configure(state="normal" if self.child and not self.stopping else "disabled")

    def apply_loaded(self,result):
        self.monitor_generation+=1; self.external_running=False
        self.monitor_filters={"page":1}; self.event_cursor=(None,0)
        for variable in self.filter_vars.values(): variable.set("")
        self.filter_slow.set(False)
        self.monitor_rows={}; self.tasks.delete(*self.tasks.get_children())
        self.monitor_summary.set("等待监控历史…")
        self.mark_monitor_stale("配置已加载，等待监控数据…")
        self.loaded=result; self.loaded_path=str(Path(self.config.get()).resolve())
        for key,value in result["fields"].items(): self.fields[key].set(value)
        summary=result["summary"]
        self.summary.set(f'后端 {summary.get("provider", "Ollama")}；端口 {summary["port"]}；数据目录 {summary["dataDirectory"]}\n模型配置 {summary["inferenceConfig"]}；允许域名 {", ".join(summary["allowedHosts"])}')
        atomic_write(self.launcher_file,(json.dumps({"python":self.python.get(),"config":self.config.get()},ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
        self.feedback.set("配置已加载，路径已记住；原文件尚未修改。")

    def load_config(self):
        self.invoke("inspect",{},self.apply_loaded,"正在读取所选配置…")

    def require_loaded(self):
        if not self.loaded or self.loaded_path != str(Path(self.config.get()).resolve()):
            messagebox.showinfo("请先加载","请点击上方“加载配置”，确认操作的是目标服务配置。",parent=self.window)
            return False
        return True

    def field_values(self):
        values={key:variable.get() for key,variable in self.fields.items()}
        for key in ("payload_days","max_request_bytes"):
            try: values[key]=int(values[key])
            except ValueError: raise ValueError("天数和大小须填写整数。") from None
        return values

    def recommended(self):
        for key,value in {"manage_natapp":True,"payload_days":1,"max_request_bytes":20971520}.items():
            self.fields[key].set(value)
        self.feedback.set("已填入升级推荐值，尚未保存。请选择5080现有的NATAPP程序和INI。")

    def save_config(self):
        if not self.require_loaded(): return
        try: values=self.field_values()
        except ValueError as error: messagebox.showerror("配置错误",str(error),parent=self.window); return
        def saved(result):
            self.apply_loaded(result)
            self.feedback.set("配置已保存；原文件备份："+result["backup"])
        self.invoke("save-config",{"fields":values,"sha256":self.loaded["sha256"]},saved,"正在备份原配置并合并基础配置…")

    def populate_users(self,result):
        self.users.delete(*self.users.get_children())
        for row in result["users"]:
            self.users.insert("","end",values=(row["user_id"],row["display_name"],"启用" if row["enabled"] else "禁用"))
        self.feedback.set(f'已读取 {len(result["users"])} 个用户。')

    def refresh_users(self):
        if self.require_loaded(): self.invoke("users",{},self.populate_users,"正在读取用户；旧数据库如需迁移会先自动备份…")

    def selected_user(self):
        selected=self.users.selection()
        if not selected:
            messagebox.showinfo("选择用户","请先刷新并选中一个用户。",parent=self.window); return None
        return self.users.item(selected[0],"values")[0]

    def account_dialog(self):
        if not self.require_loaded(): return
        dialog=tk.Toplevel(self.window); dialog.title("创建账号")
        dialog.transient(self.window); dialog.resizable(False,False)
        frame=ttk.Frame(dialog,padding=20); frame.pack(fill="both",expand=True)
        variables={key:tk.StringVar() for key in ("username","displayName")}
        entries=[]
        for index,(key,label) in enumerate((("username","账号"),("displayName","显示名称"))):
            ttk.Label(frame,text=label).grid(row=index,column=0,sticky="w",padx=(0,12),pady=6)
            entry=ttk.Entry(frame,textvariable=variables[key],width=32)
            entry.grid(row=index,column=1,pady=6); entries.append(entry)
        def submit():
            values={key:variable.get() for key,variable in variables.items()}
            if not values["username"].strip() or len(values["username"])>128:
                messagebox.showerror("检查账号","请输入1–128个字符的账号。",parent=dialog); return
            self.invoke("create-user",values,self.populate_users,"正在创建账号…")
            dialog.destroy()
        ttk.Button(frame,text="创建",command=submit).grid(row=2,column=1,sticky="e",pady=(8,0))
        dialog.bind("<Return>",lambda event:submit()); dialog.grab_set(); entries[0].focus_set()

    def issue_key(self):
        if not self.require_loaded(): return
        username=self.selected_user()
        if not username: return
        days=simpledialog.askinteger("签发API key",f"账号：{username}\n有效天数，0表示不设固定到期时间：",parent=self.window,
                                     initialvalue=0,minvalue=0,maxvalue=3650)
        if days is None: return
        expires=None if days==0 else int(time.time()*1000)+days*86400000
        self.invoke("token-create",{"username":username,"expiresAtMs":expires},self.show_created_key,"正在签发API key…")

    def show_created_key(self,result):
        secret=result.pop("apiKey")
        dialog=tk.Toplevel(self.window); dialog.title("API key已签发")
        dialog.transient(self.window); dialog.resizable(False,False)
        frame=ttk.Frame(dialog,padding=20); frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="账号："+result["username"]+"\n请复制并保存到OpenCode。完整key只在本窗口显示，关闭后无法取回。",wraplength=780).pack(anchor="w")
        variable=tk.StringVar(value=secret)
        entry=ttk.Entry(frame,textvariable=variable,width=100,state="readonly"); entry.pack(fill="x",pady=12)
        def copy():
            dialog.clipboard_clear(); dialog.clipboard_append(variable.get())
            self.feedback.set("API key已复制到剪贴板。")
        def close():
            variable.set(""); dialog.destroy()
        actions=ttk.Frame(frame); actions.pack(anchor="e")
        ttk.Button(actions,text="复制key",command=copy).pack(side="left",padx=8)
        ttk.Button(actions,text="关闭",command=close).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW",close)
        self.feedback.set("API key已签发；完整值只显示在密钥窗口，不写入日志。")

    def manage_keys(self):
        if not self.require_loaded(): return
        username=self.selected_user()
        if not username: return
        self.invoke("token-list",{"username":username},self.show_keys,"正在读取API key状态…")

    def show_keys(self,result):
        username=result["username"]
        dialog=tk.Toplevel(self.window); dialog.title("API key管理 · "+username)
        dialog.transient(self.window)
        frame=ttk.Frame(dialog,padding=16); frame.pack(fill="both",expand=True)
        table=ttk.Treeview(frame,columns=("id","created","expires","status"),show="headings",selectmode="browse",height=10)
        for key,label,width in (("id","keyId",290),("created","创建时间",160),("expires","到期时间",160),("status","状态",90)):
            table.heading(key,text=label); table.column(key,width=width)
        table.pack(fill="both",expand=True)
        def populate(data):
            if not dialog.winfo_exists(): return
            table.delete(*table.get_children())
            for row in data["keys"]:
                stamp=lambda value:datetime.fromtimestamp(value/1000).strftime("%Y-%m-%d %H:%M") if value is not None else "不设到期"
                table.insert("","end",values=(row["key_id"],stamp(row["created_at_ms"]),stamp(row["expires_at_ms"]),
                    {"valid":"有效","expired":"已到期","revoked":"已撤销"}[row["status"]]))
        def revoke():
            chosen=table.selection()
            if not chosen: return
            key=table.item(chosen[0],"values")[0]
            if not messagebox.askyesno("撤销API key","撤销所选key后，使用它的程序将无法继续认证。",parent=dialog): return
            self.invoke("token-revoke",{"username":username,"keyId":key},populate,"正在撤销所选API key…")
        actions=ttk.Frame(frame); actions.pack(fill="x",pady=(12,0))
        ttk.Button(actions,text="刷新",command=lambda:self.invoke("token-list",{"username":username},populate,"正在刷新key状态…")).pack(side="left")
        ttk.Button(actions,text="撤销选中key",command=revoke).pack(side="left",padx=8)
        ttk.Label(frame,text="时间为本机时区；账号禁用时所有key均无法访问。此处不显示密钥明文。",wraplength=720).pack(anchor="w",pady=(10,0))
        populate(result)

    def set_enabled(self,enabled):
        if not self.require_loaded(): return
        username=self.selected_user()
        if not username: return
        if not enabled and not messagebox.askyesno("禁用账号",f"禁用 {username} 并取消其活动任务？",parent=self.window): return
        self.invoke("enable" if enabled else "disable",{"username":username},self.populate_users,"正在更新用户状态…")

    def backup(self):
        if not self.require_loaded(): return
        self.invoke("backup",{},lambda result:self.feedback.set(f'数据库备份完成（schema v{result["schemaVersion"]}）：{result["backup"]}'),"正在创建升级前数据库一致性备份…")

    def open_backups(self):
        path=ROOT/".local/backups"; path.mkdir(parents=True,exist_ok=True)
        os.startfile(path)

    def install_dependencies(self):
        try: python,_=self.input_paths()
        except ValueError as error: messagebox.showerror("检查路径",str(error),parent=self.window); return
        if self.child:
            messagebox.showinfo("先停止服务","请先停止服务，再更新其Python依赖。",parent=self.window); return
        if not messagebox.askyesno("安装依赖","使用当前Python安装服务端锁定依赖？不会下载模型。",parent=self.window): return
        self.invoke("install-dependencies",{},lambda result:self.feedback.set("依赖安装完成。可以刷新用户或启动服务。"),"正在安装依赖，请稍候…")

    def probe(self):
        if not self.require_loaded(): return
        self.invoke("probe",{},lambda result:self.feedback.set(result["message"]),"正在检查本机API…")

    def startup_check(self):
        if not self.require_loaded(): return
        def checked(result):
            self.log("=== 启动前检查（不启动模型、不修改配置） ===")
            for item in result["checks"]:
                self.log(("通过" if item["ok"] else "需处理")+" ["+item["code"]+"] "+item["message"])
            self.feedback.set("静态检查通过；模型身份和实际运行仍需启动验证。" if result["ok"] else "启动前检查发现问题，请看日志中的“需处理”项。")
        self.invoke("startup-check",{},checked,"正在检查JSON、路径、端口及数据锁，不启动模型…")

    def start_service(self):
        if self.child or self.external_running or self.busy or not self.require_loaded(): return
        try:
            if self.field_values()!=self.loaded["fields"]:
                raise ValueError("配置页有未保存修改，请先保存，或重新加载原配置后启动。")
            python,config=self.input_paths()
            from aiserver.operations.engine import WindowsJob
            from aiserver.operations.processes import resume_suspended
            process=subprocess.Popen([python,"-u",str(ROOT/"scripts/server/ManagerActions.py"),"serve"],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding="utf-8",
                env=child_environment(ROOT),cwd=ROOT,creationflags=(subprocess.CREATE_NO_WINDOW|0x4) if os.name=="nt" else 0)
            job=None
            try:
                if os.name=="nt":
                    job=WindowsJob(process); resume_suspended(process)
                process.stdin.write(json.dumps({"config":config,"expectedHash":self.loaded["sha256"]})+"\n"); process.stdin.flush()
            except BaseException:
                if job: job.close()
                elif process.poll() is None: process.terminate()
                process.wait(timeout=10)
                raise
            self.child,self.job=process,job
            self.status.set("服务启动中…")
            def read():
                try:
                    for line in process.stdout:
                        try: self.mailbox.put(("service",json.loads(line)))
                        except ValueError: pass
                finally:
                    process.stdout.close()
                    self.mailbox.put(("exited",process.wait()))
            threading.Thread(target=read,daemon=True).start()
            self.update_buttons()
        except Exception as error:
            messagebox.showerror("无法启动",str(error) if type(error) is ValueError else "无法启动服务，请检查所选环境、依赖与本机进程权限。",parent=self.window)

    def stop_service(self):
        if not self.child or self.stopping: return
        self.stopping=True; self.status.set("正在停止任务与自有模型 / NATAPP，请稍候…")
        try: self.child.stdin.write("stop\n"); self.child.stdin.flush()
        except (OSError,ValueError): pass
        self.update_buttons()

    def log(self,message):
        self.logs.configure(state="normal")
        self.logs.insert("end",message[:4000]+"\n")
        if int(self.logs.index("end-1c").split(".")[0])>1000: self.logs.delete("1.0","200.0")
        self.logs.see("end"); self.logs.configure(state="disabled")

    def drain(self):
        if self.last_observed_ms is not None and time.time()*1000-self.last_observed_ms>10000:
            self.mark_monitor_stale("实时数据已过期（超过10秒）；请检查原服务进程。")
        try:
            while True:
                item=self.mailbox.get_nowait()
                if item[0]=="done":
                    self.busy=False
                    try: item[1](item[2])
                    except Exception: self.feedback.set("界面更新失败，请重新加载配置。")
                    self.update_buttons()
                elif item[0]=="monitor":
                    self.monitor_pending=False
                    if item[1]==self.monitor_generation and item[2]==self.monitor_filters:
                        self.apply_monitor(item[3])
                elif item[0]=="failed":
                    self.busy=False; self.feedback.set(item[1]); self.update_buttons()
                    messagebox.showerror("操作未完成",item[1],parent=self.window)
                elif item[0]=="service":
                    value=item[1]
                    if value.get("event")=="state":
                        if not self.stopping: self.status.set(SCHEDULER_CAPTIONS.get(value["scheduler"],value["scheduler"]))
                        self.tunnel_status.set("NATAPP："+{"running":"进程运行","stopped":"未运行","retrying":"正在重试","disabled":"未启用"}.get(value["tunnel"],value["tunnel"])+"；公网入口："+self.public_caption(value.get("publicConnectivity",{})))
                    else:
                        self.log(value.get("message",""))
                        if value.get("event")=="error": self.feedback.set(value["message"])
                elif item[0]=="exited":
                    if self.job: self.job.close(); self.job=None
                    if self.child and self.child.stdin: self.child.stdin.close()
                    self.child=None; self.stopping=False
                    self.status.set("服务已停止" if item[1]==0 else f"启动或运行失败（退出码 {item[1]}），请查看日志提示")
                    self.tunnel_status.set("本窗口托管的NATAPP已结束；公网状态未检查")
                    self.update_buttons()
                    if self.closing: self.finish_close(); return
        except queue.Empty:
            pass
        self.window.after(100,self.drain)

    def close(self):
        if self.busy:
            messagebox.showinfo("操作进行中","请等待当前保存、账号操作或依赖安装完成。",parent=self.window); return
        if self.child:
            if not messagebox.askyesno("停止并退出","关闭窗口会先停止本窗口启动的服务。是否停止并退出？",parent=self.window): return
            self.closing=True; self.stop_service(); return
        self.finish_close()

    def finish_close(self):
        self.closing=True
        self.executor.shutdown(wait=False,cancel_futures=True)
        self.monitor_executor.shutdown(wait=False,cancel_futures=True)
        self.window.destroy()


def main():
    window=tk.Tk()
    ServerManager(window)
    window.mainloop()


if __name__ == "__main__":
    main()
