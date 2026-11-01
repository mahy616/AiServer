"""One real OpenCode task: first submission, then at most one test-feedback fix."""
import json
import re
import time

from .common import read_events, run_process, sha, write_json
from .harness import evaluate
from .cases import analysis_result


def extract_script(answer):
    blocks = re.findall(r"```(?:csharp|cs|c#)?\s*\n(.*?)```", answer, re.S | re.I)
    if len(blocks) == 1:
        return blocks[0].strip()+"\n"
    if not blocks and re.match(r"\s*(using\s|public\s+(?:static\s+)?class\s)", answer):
        return answer.strip()+"\n"
    return None


def feedback(case, result, work):
    if not result["delivery_valid"]:
        return "尚未交付有效Script.cs；不能保留入口骨架，也不能只描述工具调用。"
    evaluation = result["evaluation"]
    if not evaluation.get("compiled"):
        path = work / "build.jsonl"
        return "编译失败：\n" + (path.read_text(encoding="utf-8")[:3000] if path.exists() else "未取得编译输出")
    rows = read_events(work / "results.jsonl")
    examples = [{"input": list(case.tests[i])[:128], "input_length": len(case.tests[i]),
                 "expected": case.expected[i], "actual": rows[i] if i < len(rows) else None}
                for i in evaluation.get("failed_indices", [])[:3]]
    return f"功能通过{evaluation['passed']}/{evaluation['total']}，行数限制{case.cap}。失败样例："+json.dumps(examples, ensure_ascii=False)


def run_task(binary, dotnet, case, directory, info, relay, timeout, env_factory, phase_fn, allow_correction=True):
    start = time.monotonic()
    relay.deadline = start + timeout
    env, config, config_path = env_factory(directory, info, relay)
    native = info.get("toolCall", False)
    result = {"phases": [], "session_id": None, "mode": "native-tools" if native else "text-code",
              "thinking": info.get("benchmark_thinking", False),
              "temperature_policy": "server-controlled", "correction_used": False, "timeout_seconds": timeout}
    session = None
    script = directory / "workspace/Script.cs"
    specification = (directory / "workspace/TASK.md").read_text(encoding="utf-8")

    def invoke(prompt, name):
        nonlocal session
        relay.phase = name
        print(f"  {directory.name}: {name} ({result['mode']})", flush=True)
        relay.terminal_error.clear()
        remaining = timeout-(time.monotonic()-start)
        if remaining <= 0:
            raise ValueError("会话时间预算已用尽")
        previous = session
        item = phase_fn(binary, directory, env, info["id"], prompt, name, remaining, session,
                        stop_event=relay.terminal_error, agent="build" if native else "benchmark-text",
                        variant=(result["thinking"] if isinstance(result["thinking"], str) else
                                 "thinking" if result["thinking"] else "normal") if info.get("thinking") else None)
        result["phases"].append(item)
        session = item.get("session_id") or session
        result["session_id"] = session
        if item.get("timed_out") or time.monotonic() >= relay.deadline or any(r.get("deadline_exceeded") for r in relay.rows):
            result["failure_category"] = "budget"
            raise ValueError("整题时间预算已用尽；保留已生成内容，不计为服务故障，也不自动重试")
        if item.get("aborted") or relay.terminal_error.is_set():
            result["failure_category"] = "transport-or-service"
            raise ValueError("首次服务／传输错误；已停止该题自动重试，原始错误见请求日志")
        if item.get("timed_out") or not item.get("finished"):
            raise ValueError("阶段超时或未正常结束")
        if not session or item.get("session_count") != 1 or previous and session != previous:
            raise ValueError("阶段意外切换会话")
        return item

    def snapshot(name, item):
        text_saved = bool(not native and result.get("first", {}).get("text_saved_by_harness"))
        if not native:
            source = extract_script(item.get("answer", ""))
            if source is not None:
                script.write_text(source, encoding="utf-8")
                text_saved = True
        source = script.read_text(encoding="utf-8-sig") if script.exists() else ""
        changed = sha(source) != sha(case.source)
        placeholder = bool(re.search(r"throw\s+new\s+NotImplementedException\s*\(", source))
        tools = [t for phase in result["phases"] for t in phase.get("tools", [])]
        wrote = any(t["name"] in ("write", "edit", "apply_patch") and t["status"] == "completed" for t in tools)
        read = any(t["name"] == "read" and t["file"] == "Script.cs" and t["status"] == "completed" for t in tools)
        delivered = changed and not placeholder and (wrote if native else text_saved)
        if case.kind == "read" and native:
            delivered = delivered and read
        extras = [p.name for p in (directory/"workspace").iterdir() if p.name not in ("Script.cs", "TASK.md")]
        delivered = delivered and not extras
        work = directory / ("evaluation" if name == "first" else "correction-evaluation")
        evaluation = evaluate(case, script, work, dotnet)
        evaluation["all_passed"] = bool(evaluation.get("all_passed") and delivered)
        analysis_ok = case.kind != "read" or all(result.get("analysis", {}).get(k) for k in ("faults_correct", "sample_correct"))
        item_result = {"file_changed": changed, "placeholder": placeholder, "write_tool_completed": wrote,
                       "script_read": read, "text_saved_by_harness": text_saved, "delivery_valid": delivered,
                       "extra_files": extras, "evaluation": evaluation, "task_passed": evaluation["all_passed"] and analysis_ok,
                       "seconds": round(time.monotonic()-start, 3)}
        meaningful = [line.strip() for line in source.splitlines() if len(line.strip()) >= 40
                      and not line.lstrip().startswith("//")]
        item_result["code_structure"] = {"lines": len(source.splitlines()),
            "repeated_long_lines": len(meaningful)-len(set(meaningful)), "maintainability_review": "待人工审阅"}
        if source:
            (directory/(name+"-Script.cs")).write_text(source, encoding="utf-8")
        result[name] = item_result
        result.update(evaluation=evaluation, delivery_valid=delivered, task_passed=item_result["task_passed"])
        return item_result, work

    try:
        if case.kind == "read":
            prompt = ("阅读TASK.md和整个Script.cs，仅分析，不修改。按当前含错误源码的实际行为计算输入"+
                json.dumps(case.sample)+"的Run输出，找出所有与约定不符的函数。最终只输出JSON："
                '{"faultyFunctions":["函数名"],"sampleResult":[数字],"explanation":"简短原因"}。'
                "长文件分段读完，不运行命令、不委派。")
            if not native:
                prompt += "\n全部材料如下，无需工具。\n"+specification+"\nScript.cs：\n"+case.source
            item = invoke(prompt, "reading")
            result.update(analysis=analysis_result(item["answer"], case), analysis_text=item["answer"])
            if script.read_text(encoding="utf-8") != case.source:
                raise ValueError("只读阶段修改了脚本")
        if native:
            config["permission"].update(edit="allow", write="allow", apply_patch="allow")
            write_json(config_path, config)
            prompt = ("按TASK.md完成Script.cs。"+("修正源码中的错误。" if case.kind == "read" else "从入口骨架实现。")+
                "必须用真实文件编辑工具交付唯一Script.cs，不要只在回答中给代码或工具JSON。"
                "不运行编译、测试或命令，不委派。保存后简短说明。")
        else:
            prompt = ("按以下约定交付完整单文件C#代码，所有类型放在Script.cs，入口必须定义在public static class Script内。"
                "只输出一个csharp代码块，不调用工具。\n"+specification+"\n初始Script.cs：\n"+case.source)
        item = invoke(prompt, "implementation")
        first, work = snapshot("first", item)
        needs_analysis = case.kind == "read" and not all(result.get("analysis", {}).get(k)
            for k in ("faults_correct", "sample_correct"))
        if (not first["task_passed"]) and allow_correction:
            prompt = "首次交付的本机检查结果如下，仅允许这一次修正。\n"+feedback(case, first, work)
            analysis_only = needs_analysis and first["evaluation"].get("all_passed")
            if analysis_only:
                prompt += "\n代码功能已通过，禁止修改Script.cs，只修正阅读分析。"
                if native:
                    config["permission"].update(edit="deny", write="deny", apply_patch="deny")
                    write_json(config_path, config)
            else:
                prompt += ("\n直接编辑唯一Script.cs，不运行命令，完成后简短说明。" if native else
                           "\n只输出修正后的完整单文件C#代码块。当前Script.cs：\n"+script.read_text(encoding="utf-8"))
            if needs_analysis:
                prompt += ("\n阅读检查未通过：" + json.dumps(result["analysis"], ensure_ascii=False)
                    + "。请依据下列原始含错源码重新计算，不要用已修复文件的行为代替。最终附JSON："
                    '{"faultyFunctions":["函数名"],"sampleResult":[数字],"explanation":"原因"}。'
                    + "\n原始约定：\n" + specification + "\n算例输入：" + json.dumps(case.sample)
                    + "\n原始含错Script.cs：\n" + case.source)
            result["correction_used"] = True
            previous_source = script.read_text(encoding="utf-8")
            item = invoke(prompt, "correction")
            if analysis_only and script.read_text(encoding="utf-8") != previous_source:
                raise ValueError("只读分析修正阶段修改了脚本")
            if needs_analysis:
                result.update(analysis=analysis_result(item["answer"], case), analysis_text=item["answer"])
            snapshot("final", item)
        else:
            result["final"] = first
        if not result.get("delivery_valid"):
            result["failure_category"] = "tool-delivery" if native else "delivery"
            result["error"] = "未形成有效脚本交付（文件读取／编辑证据或脚本内容不满足要求）"
        elif not result.get("task_passed"):
            result["failure_category"] = "analysis" if result["evaluation"].get("all_passed") else "code"
            result["error"] = ("代码功能通过，但阅读缺陷定位／算例未通过" if result["evaluation"].get("all_passed") else
                               result["evaluation"].get("error") or "功能检查未全过")
    except ValueError as error:
        result["error"] = str(error)
    finally:
        relay.cancel()
        if not result.get("evaluation"):
            result["evaluation"] = evaluate(case, script, directory/"evaluation", dotnet)
        if result.get("error"):
            result["task_passed"] = False
        result["seconds"] = round(time.monotonic()-start, 3)
        result["requests"] = list(relay.rows)
        if session:
            exported = run_process([str(binary), "export", session], directory/"workspace", env, 20,
                                   directory/"session-export", secrets=(relay.token,))
            if exported["exit_code"] == 0:
                try:
                    messages = json.loads((directory/"session-export.jsonl").read_text(encoding="utf-8"))["messages"]
                    result["compactions"] = sum(p.get("type") == "compaction" for m in messages for p in m.get("parts", []))
                except (ValueError, KeyError, TypeError):
                    result["export_error"] = "会话导出无法解析"
    return result
