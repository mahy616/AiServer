"""Start this machine's OpenCode using its own encrypted AiServer API key."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading

from opencode_support import (ROOT, BINARY, WORK, DEFAULT_SERVER, SetupError, environment,
                              install_key, refresh_connection)


def key_dialog(verify_after_save=False):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("OpenCode · AiServer独立API key")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=22)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="请粘贴5080上AiServer签发的独立API key。\n密钥仅保存到本机Windows当前用户的加密文件，不依赖原客户端。",
              wraplength=580).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
    ttk.Label(frame, text="服务地址").grid(row=1, column=0, sticky="w", padx=(0, 12))
    server = tk.StringVar(value=DEFAULT_SERVER)
    ttk.Entry(frame, textvariable=server, width=63).grid(row=1, column=1, pady=6)
    ttk.Label(frame, text="API key").grid(row=2, column=0, sticky="w")
    secret = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=secret, width=63, show="●")
    entry.grid(row=2, column=1, pady=6)
    status = tk.StringVar(value="等待输入。验证通过后才保存；请勿把密钥发到聊天里。")
    ttk.Label(frame, textvariable=status, wraplength=580).grid(row=3, column=0, columnspan=2, sticky="w", pady=12)
    messages = []
    busy = False

    def submit():
        nonlocal busy
        if busy:
            return
        busy = True
        button.configure(state="disabled")
        status.set("正在验证5080连接、账号和模型，并加密保存……")
        address, key = server.get(), secret.get()

        def worker():
            try:
                summary = install_key(address, key)
                if verify_after_save:
                    result = subprocess.run([sys.executable, str(Path(__file__).with_name("Verify-OpenCode.py"))],
                        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=420)
                    if result.returncode:
                        raise SetupError("独立key已保存，连接通过；文件工具检查失败，请查看verification/last-result.json。")
                messages.append((True, "已完成：独立API key连接正常。" +
                    ("本地读取与工具结果回传检查通过。" if verify_after_save else "") +
                    "\n账号：" + summary["userId"] + "；模型：" + summary["model"]))
            except SetupError as error:
                messages.append((False, str(error)))
            except Exception:
                messages.append((False, "设置／验证失败；请检查本机依赖、网络及verification/last-result.json。"))

        threading.Thread(target=worker, daemon=True).start()

    def poll():
        nonlocal busy
        if messages:
            success, message = messages.pop(0)
            status.set(message)
            secret.set("")
            busy = False
            button.configure(state="normal", text="重新设置并验证" if success else "验证并保存")
        window.after(200, poll)

    button = ttk.Button(frame, text="验证并保存", command=submit)
    button.grid(row=4, column=1, sticky="e")
    entry.focus_set()
    window.bind("<Return>", lambda event: submit())
    window.after(200, poll)
    window.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--set-key", action="store_true", help="Open a masked key input window")
    parser.add_argument("--verify-after-save", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check independent key and refresh model limits")
    parser.add_argument("--verify", action="store_true", help="Run actual local-file tool verification")
    args, command = parser.parse_known_args()
    if command and command[0] == "--":
        command = command[1:]
    if args.set_key:
        return key_dialog(args.verify_after_save)
    if args.verify:
        return subprocess.call([sys.executable, str(Path(__file__).with_name("Verify-OpenCode.py"))], cwd=ROOT)
    directory = args.project.resolve()
    if not directory.is_dir():
        raise SetupError("项目目录不存在。")
    if not BINARY.is_file():
        raise SetupError("本机OpenCode程序缺失。")
    summary = refresh_connection()
    if args.check:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    return subprocess.call([str(BINARY), *command], cwd=directory, env=environment(args.read_only))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SetupError as error:
        print("OpenCode：" + str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print("OpenCode启动失败：请检查程序、本机独立密钥及服务配置；可运行--set-key重新设置。", file=sys.stderr)
        raise SystemExit(1)
