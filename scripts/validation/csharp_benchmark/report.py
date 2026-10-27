"""Compact client Markdown: evidence-backed comparisons, never invented winners."""
from datetime import datetime, timedelta, timezone
import statistics


def cell(value):
    if value is None:
        return "未取得"
    return str(value).replace("|", "\\|").replace("\n", " ")


def passed(task, first=False):
    result = task.get("result", {})
    item = result.get("first", {}) if first else result
    return bool(item.get("task_passed", item.get("evaluation", {}).get("all_passed"))
                and not (result.get("error") if not first else False))


def seconds(tasks):
    values = [t["result"]["seconds"] for t in tasks if passed(t) and t.get("result", {}).get("seconds") is not None]
    return f"{statistics.median(values):.2f}s" if values else "—"


def usage(requests):
    values = [r["usage"] for r in requests if isinstance(r.get("usage"), dict)]
    if not values:
        return "未取得"
    total = f"{sum(v.get('prompt_tokens', 0) for v in values)}/{sum(v.get('completion_tokens', 0) for v in values)}"
    return total if len(values) == len(requests) else total + f"（不完整，{len(values)}/{len(requests)}请求有usage）"


def thinking_label(state, alias):
    condition = state.get("conditions", {}).get(alias)
    return ("thinking" if condition["thinking"] else "普通") if condition else "历史模式未标记"


def output_tokens(tasks):
    values = [r.get("usage") for t in tasks for r in t.get("result", {}).get("requests", [])]
    return sum(v.get("completion_tokens", 0) for v in values) if values and all(isinstance(v, dict) for v in values) else "未取得"


def render(state, path):
    local = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    aliases = list(state.get("models", {}))
    groups = {a: [t for t in state["tasks"] if t["model"] == a and t.get("status") != "skipped"] for a in aliases}
    lines = ["# C#单文件脚本客户端报告", "", f"运行：`{state['run_id']}`；题库：`{state['version']}`；更新：{local}",
             f"OpenCode：{cell(state.get('opencode_version'))}；状态：{cell(state.get('status'))}；服务：{cell(state.get('server'))}",
             "", "## 一眼看结果", "",
             "首次结果与一次反馈修正后的最终结果分开。仅统计真实交付；骨架、伪工具文本和未修改脚本不计通过。",
             "简单题是基础校准，不能据此宣布复杂C#能力最强。文本代码模式由测试器落盘，与原生工具模式分别标明。", "",
             "| 模型／思考模式 | 适合场景／当前优势 | 首次→最终通过 | 成功任务中位耗时 | 代码质量证据 | 已验证代码上下文 |",
             "|---|---|---|---|---|---|"]
    for a, tasks in groups.items():
        good = [t for t in tasks if passed(t)]
        scenes = [t["case"] for t in good]
        observed = [r["usage"]["prompt_tokens"] for t in good for r in t["result"].get("requests", [])
                    if isinstance(r.get("usage"), dict) and r["usage"].get("prompt_tokens") is not None]
        modes = sorted({t.get("result", {}).get("mode", "未采集") for t in tasks if t.get("result")})
        code_good = sum(bool(t.get("result", {}).get("delivery_valid") and
                            t.get("result", {}).get("evaluation", {}).get("all_passed")) for t in tasks)
        evidence = f"功能全过{code_good}/{len(tasks)}；结构／维护性待审阅"
        lines.append(f"| {a}（{thinking_label(state, a)}／{cell(', '.join(modes) or '未运行')}） | {cell(', '.join(scenes) or '尚无完整通过；按异常定位')} | "
                     f"{sum(passed(t, True) for t in tasks)}→{len(good)}/{len(tasks)} | {seconds(good)} | {evidence} | "
                     f"{'至少'+str(max(observed))+' tokens' if observed else '未取得'} |")
    lines += ["", "## 同模型普通／thinking对照", "",
              "相同题面、相同模型输出上限、各自独立目录与会话；思考模式使用单独的较长整题预算。耗时包含思考、工具和最多一次修正。只在两种模式都通过的共同题上比较耗时与生成token。",
              "生成token包含思考与正文；思考字符来自实际流，不能直接换算成思考token。请求开启思考不保证每次都有思考正文。", "",
              "| 模型 | 普通首次→最终通过 | thinking首次→最终通过 | 共同通过题 | 普通／thinking总秒数 | 普通／thinking生成token | thinking实际思考字符 |",
              "|---|---|---|---:|---|---|---:|"]
    pairs = 0
    for base in state.get("selected_models", []):
        normal, thinking = groups.get(base), groups.get(base + "-thinking")
        if normal is None or thinking is None:
            continue
        pairs += 1
        common = {t["case"] for t in normal if passed(t)} & {t["case"] for t in thinking if passed(t)}
        n = [t for t in normal if t["case"] in common]
        t = [t for t in thinking if t["case"] in common]
        elapsed = lambda tasks: f"{sum(item['result']['seconds'] for item in tasks):.2f}" if tasks else "—"
        score = lambda tasks: f"{sum(passed(item, True) for item in tasks)}→{sum(passed(item) for item in tasks)}/{len(tasks)}"
        chars = sum(r.get("reasoning_chars", 0) for item in t for r in item["result"].get("requests", []))
        lines.append(f"| {base} | {score(normal)} | {score(thinking)} | {len(common)} | "
                     f"{elapsed(n)}／{elapsed(t)} | {output_tokens(n)}／{output_tokens(t)} | {chars if common else '—'} |")
    if not pairs:
        lines += ["| — | — | — | 0 | — | — | 本轮无双模式对照 |"]
    if state.get("thinking_selection") == "fallback":
        lines += ["", "## 普通失败后的thinking兜底", "",
                  "普通模式已解决则跳过thinking，不计作思考失败。仅普通失败后独立重试原题；不继承失败会话或测试答案，累计耗时含普通尝试与thinking尝试。",
                  "", "| 题目 | 普通结果 | thinking结果 | 增加秒数 | 两次累计秒数 |",
                  "|---|---|---|---:|---:|"]
        indexed = {t["id"]: t for t in state["tasks"]}
        for task in state["tasks"]:
            source = indexed.get(task.get("fallback_from"))
            if source is None:
                continue
            normal_s, extra = source.get("result", {}).get("seconds"), task.get("result", {}).get("seconds")
            total = round(normal_s + extra, 3) if normal_s is not None and extra is not None else None
            lines.append(f"| {task['id']} | {source['status']} | {cell(task.get('skip_reason') or task['status'])} | {cell(extra)} | {cell(total)} |")
    # Tool workflows and text-only delivery have different client overhead.
    for mode in ("native-tools", "text-code"):
        for thinking in (False, True):
            selected = {a: ts for a, ts in groups.items() if
                        state.get("conditions", {}).get(a, {}).get("thinking", False) is thinking
                        and any(passed(t) and t.get("result", {}).get("mode") == mode for t in ts)}
            common = set.intersection(*[{t["case"] for t in ts if passed(t)} for ts in selected.values()]) if selected else set()
            label = mode + ("／thinking" if thinking else "／普通")
            if len(selected) >= 2 and common:
                times = {a: sum(t["result"]["seconds"] for t in ts if t["case"] in common) for a, ts in selected.items()}
                lines += ["", f"**{label}共同通过题耗时：** " + "；".join(f"{a}={value:.2f}s" for a,value in times.items())
                          + "。题目：" + ", ".join(sorted(common)) + "；单次样本不宣布稳定效率冠军。"]
            else:
                lines += ["", f"**{label}效率对照：证据不足。** 需要至少两个同模式模型共同通过相同题目。"]
    levels = {t["case"].split("-")[0] for t in state["tasks"]}
    windows = {a: state.get("capabilities", {}).get(a, {}).get("limits", {}).get("contextTokens") for a in aliases}
    windows = {a: n for a, n in windows.items() if isinstance(n, int)}
    if windows:
        maximum = max(windows.values())
        lines += ["", "**配置窗口最大：** "+", ".join(a for a,n in windows.items() if n == maximum)+f"（{maximum} tokens）。真实代码的有效容量边界尚未确定，不能用短题成功推成窗口全程可靠。"]
    lines += ["", "**代码能力结论：** "+("本轮仅简单题；阅读和实现基础表现见上表，中难能力待验证。" if levels <= {"easy"} else
                  "按下表分别比较阅读／实现及最高通过难度；单次运行不代表稳定成功率。"),
              "", "**代码质量结论：** 编译、功能及有效边界用例有自动证据；可读性和维护性尚未人工审阅，不强行评出质量冠军。",
              "", "## 模型容量与使用限制", "",
              "| 模型 | 配置窗口 | 默认输入预算 | 最大输出 | 计数方法 | 原生工具 | 首题／后续成功耗时 |",
              "|---|---:|---:|---:|---|---|---|"]
    for a in aliases:
        info = state.get("capabilities", {}).get(a, {})
        limits = info.get("limits", {})
        cold = [t for t in groups[a] if t.get("deployment_phase") == "cold-or-switch"]
        warm = [t for t in groups[a] if t.get("deployment_phase") == "warm-followup"]
        lines.append(f"| {a} | {cell(limits.get('contextTokens'))} | {cell(limits.get('defaultMaxInputTokens'))} | "
                     f"{cell(limits.get('maxOutputTokens'))} | {cell(info.get('capacityMethod'))} | "
                     f"{'是' if info.get('toolCall') else '否／未发布'} | {seconds(cold)}／{seconds(warm)} |")
    lines += ["", "模型标识："+"；".join(f"{a}=`{m}`" for a, m in state.get("models", {}).items()),
              "", "首题／后续是客户端顺序标记，是否实际冷加载需用服务端日志核对。输入预算=窗口−本次输出预算−预留；最大输入与最大输出不能同时取满。",
              "保守估算不等于精确容量保证。代码上下文为成功任务中的最大实际输入点；未测边界时只报告“至少”，不称稳定上限。", "",
              "## 每题首次与最终结果", "",
              "| 模型／题目 | 思考模式 | 状态／执行模式 | 首次交付 | 首次功能 | 最终功能 | 阅读缺陷／算例 | 修正 | 总秒数／预算 | 输入/输出token | 思考字符 |",
              "|---|---|---|---|---|---|---|---|---:|---|---:|"]
    for task in state["tasks"]:
        r = task.get("result", {})
        first = r.get("first", {})
        fe, ev = first.get("evaluation", {}), r.get("evaluation", {})
        def tests(e):
            return f"{e.get('passed', 0)}/{e.get('total', 0)}" if e else "—"
        analysis = r.get("analysis")
        reading = (f"{'对' if analysis.get('faults_correct') else '错'}／{'对' if analysis.get('sample_correct') else '错'}") if analysis else "—"
        lines.append(f"| {task['model']}/{task['case']} | {thinking_label(state, task['model'])} | {task['status']}／{cell(r.get('mode'))} | "
                     f"{('有效' if first.get('delivery_valid') else '未交付') if first else '未采集'} | {tests(fe)} | {tests(ev)} | "
                     f"{reading} | {'一次' if r.get('correction_used') else '无'} | {cell(r.get('seconds'))}／{cell(task.get('timeout_seconds'))} | {usage(r.get('requests', []))} | "
                     f"{sum(req.get('reasoning_chars', 0) for req in r.get('requests', [])) if r else '—'} |")
    lines += ["", "功能数字可包含初始故障脚本的基线通过数；以交付有效且全部功能通过为该题通过。文本模式的文件由测试器保存，不代表模型执行了文件工具。",
              "", "## 关键异常与请求证据", ""]
    for task in state["tasks"]:
        r = task.get("result", {})
        if not r:
            continue
        lines += [f"- **{task['id']}**：{cell(r.get('error') or r.get('evaluation', {}).get('error') or '已记录交付与检查')}；会话 `{cell(r.get('session_id'))}`；原始文件在 `cases/{task['id']}/`。"]
        if r.get("failure_category"):
            lines += [f"  - 失败分类：{cell(r['failure_category'])}。"]
        for label in ("first", "final"):
            item = r.get(label, {})
            if item:
                lines += [f"  - {label}：文件变化={item.get('file_changed')}，真实写工具={item.get('write_tool_completed')}，"
                          f"读取Script.cs={item.get('script_read')}，骨架={item.get('placeholder')}，编译={item.get('evaluation', {}).get('compiled')}；"
                          f"结构信号={cell(item.get('code_structure'))}。"]
        for phase in r.get("phases", []):
            for tool in phase.get("tools", []):
                lines += [f"  - 工具 {cell(tool.get('name'))}／{cell(tool.get('file'))}：{cell(tool.get('status'))}；{cell(tool.get('error') or '无错误')}。"]
        for req in r.get("requests", []):
            lines += [f"  - `{req.get('request_id')}` → `{cell(req.get('job_id'))}`；阶段 {cell(req.get('phase'))}；HTTP {cell(req.get('http_status'))}；"
                      f"耗时 {cell(req.get('seconds'))}s；前次结束间隔 {cell(req.get('idle_gap_seconds'))}s；同内容尝试 {cell(req.get('identical_request_attempt'))}；"
                      f"Trace {cell(req.get('server_trace_id'))}；结束 {cell(req.get('finish_reason'))}；异常 {cell(req.get('error') or req.get('transport_error') or '无')}。"]
            lines += [f"  - think实际／预期={cell(req.get('requested_thinking'))}／{cell(req.get('expected_thinking'))}；"
                      f"思考字符={cell(req.get('reasoning_chars'))}；首个思考片段={cell(req.get('first_reasoning_seconds'))}s。"]
    lines += ["", "## 可选API容量探针", "", "探针与真实代码上下文分开；输出预算统一最多4096，另测1024预算。检索、算术、格式分别判定。", "",
              "| 模型／探针 | 实际输入token | 输出预算 | HTTP | 检索 | 算术 | 格式 | 完整答案 | 异常 |",
              "|---|---:|---:|---:|---|---|---|---|---|"]
    for a, rows in state.get("probes", {}).items():
        for row in rows:
            lines.append(f"| {a}/{row['id']} | {cell(row.get('actual_input_tokens'))} | {row['output_budget']} | "
                         f"{cell(row.get('http_status'))} | {cell(row.get('retrieval_correct'))} | {cell(row.get('calculation_correct'))} | "
                         f"{cell(row.get('format_correct'))} | {cell(row.get('answer_correct'))} | {cell(row.get('error') or row.get('finish_reason'))} |")
    if not any(state.get("probes", {}).values()):
        lines += ["| — | — | — | — | 未运行 | 未运行 | 未运行 | 未运行 | 默认只做基础校准 |"]
    lines += ["", "**使用建议：** 以已通过的场景选择模型；对超容量、无工具能力、服务失败和代码错误分别判断。KV实际值、GPU及引擎阶段以5080原始采集另行分析。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
