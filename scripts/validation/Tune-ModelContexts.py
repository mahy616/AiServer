"""停服实测模型固定128K或最大全GPU窗口，长输入复核后可备份写回配置。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
sys.path.insert(0, str(ROOT / "scripts/inference"))
from aiserver.context.window_budget import validate_window
from aiserver.inference.configuration import runtime_options, validate_options
from aiserver.inference.gpu import require_gpu_status
from aiserver.inference.ollama import engine_error
from aiserver.operations.monitoring import HostSampler
from aiserver.operations.processes import matching_processes
from context_capacity import CapacityFailure, commit_configs, largest_context, write_report
from gpu_capacity_sampler import GpuSamples, engine_process_names, validate_samples

spec = importlib.util.spec_from_file_location("context_tuner_shared", ROOT / "scripts/inference/Install-Qwen3VL8B.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

PROFILES = {"9b": ("qwen35", 262144), "qwen38": ("qwen38", 262144)}
OOM_MARKERS = ("out of memory", "cuda error: memory allocation", "cuda malloc failed",
               "cudamalloc failed", "unable to allocate cuda", "not enough memory",
               "requires more gpu memory", "failed to allocate cuda", "cuda error out of memory")


class EngineRequestError(RuntimeError):
    def __init__(self, code, details):
        super().__init__(code)
        self.details = details


def generate(fetch, cfg, prompt, output, *, raw=False, think=None, diagnostics=None, stage="generate"):
    body = {"model": cfg["Model"], "prompt": prompt, "stream": False, "raw": raw,
            "truncate": False, "shift": False, "keep_alive": cfg["KeepAlive"],
            "options": runtime_options(cfg, output)}
    if think is not None:
        body["think"] = think
    entry = {"stage": stage, "endpoint": "/api/generate", "context": cfg["NumCtx"],
             "output_limit": output, "raw": raw, "think": think,
             "prompt_utf8_bytes": len(prompt.encode("utf-8")),
             "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    if diagnostics is not None:
        diagnostics.append(entry)
    started = time.monotonic()
    try:
        result = fetch("/api/generate", body)
    except HTTPError as error:
        raw_error = error.read(8193)
        detail = raw_error[:8192].decode("utf-8", "replace")
        try:
            message = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            message = detail
        entry.update(http_status=error.code, engine_error=str(message)[:2048],
                     error_truncated=len(raw_error)>8192 or len(str(message))>2048)
        if any(marker in detail.lower() for marker in OOM_MARKERS):
            raise CapacityFailure("GPU_ALLOCATION_FAILED") from None
        if engine_error(message) == "CONTEXT_TOO_LARGE":
            raise CapacityFailure("CONTEXT_TOO_LARGE") from None
        code = "PROBE_REPETITION_ABORTED" if "token repeat limit reached" in detail.lower() else "ENGINE_HTTP_" + str(error.code)
        raise EngineRequestError(code, entry) from None
    finally:
        entry["seconds"] = round(time.monotonic() - started, 3)
    entry.update(http_status=200, **{key: result[key] for key in
        ("done", "done_reason", "prompt_eval_count", "eval_count") if key in result})
    if result.get("error"):
        entry["engine_error"] = str(result["error"])[:2048]
        if any(marker in str(result["error"]).lower() for marker in OOM_MARKERS):
            raise CapacityFailure("GPU_ALLOCATION_FAILED")
        if engine_error(result["error"]) == "CONTEXT_TOO_LARGE":
            raise CapacityFailure("CONTEXT_TOO_LARGE")
        raise EngineRequestError("ENGINE_GENERATION_FAILED", entry)
    if result.get("done") is not True:
        raise RuntimeError("ENGINE_INCOMPLETE_RESPONSE")
    return result


def full_gpu(cfg, fetch):
    try:
        return require_gpu_status(cfg, fetch("/api/ps"))
    except ValueError:
        raise CapacityFailure("NOT_FULL_GPU_OR_WINDOW") from None


def capacity_prompt(count, marker):
    # Unique code-like records avoid a single repeated token dominating a raw
    # continuation. Use the installed chat template to frame the instruction.
    rows = [f"// entry {i:08x}: value=0x{((i+1)*2654435761)&0xffffffff:08x}; check=0x{((i+7)*2246822519)&0xffffffff:08x};\n"
            for i in range(count)]
    return (marker + "\nRead the following synthetic C# inventory for a memory capacity test.\n" + "".join(rows) +
            "\nEnd of inventory. Write a numbered list of 64 distinct short programming tips. Do not copy the inventory.")


def long_input(fetch, cfg, think, diagnostics=None):
    """Fit a varied templated input using actual usage; no tokenizer download."""
    markers = iter("ABCDEFGH")
    def text(count):
        # Distinct first tokens prevent prefix-cache reuse from replacing a
        # genuine long prefill with a nearly empty cached request.
        return capacity_prompt(count, next(markers))
    counts = []
    for repeats in (512, 1024):
        result = generate(fetch, cfg, text(repeats), 1, think=think, diagnostics=diagnostics, stage="calibration")
        count = result.get("prompt_eval_count")
        if type(count) is not int or count < 1:
            raise RuntimeError("ACTUAL_INPUT_USAGE_MISSING")
        counts.append(count)
    slope = (counts[1] - counts[0]) / 512
    if slope <= 0:
        raise RuntimeError("CAPACITY_PROMPT_CALIBRATION_FAILED")
    target = cfg["NumCtx"] - 16384 - cfg["ReservedTokens"]
    repeats = max(1, 512 + int((target - counts[0]) / slope))
    for _ in range(6):
        result = generate(fetch, cfg, text(repeats), 256, think=think, diagnostics=diagnostics, stage="long-input")
        actual, output = result.get("prompt_eval_count"), result.get("eval_count")
        if type(actual) is not int or type(output) is not int or output < 1:
            raise RuntimeError("ACTUAL_USAGE_MISSING")
        if target - 64 <= actual <= target:
            return {"target_input_tokens": target, "actual_input_tokens": actual,
                    "actual_output_tokens": output, "probe_output_limit": 256,
                    "think": think, "done_reason": result.get("done_reason"),
                    **{k: result[k] for k in ("total_duration", "prompt_eval_duration", "eval_duration") if k in result}}
        # Token density can differ between calibration and a large inventory.
        # Use the observed large-input density to refine the next attempt.
        density = (actual - counts[0]) / (repeats - 512) if repeats != 512 else slope
        repeats = max(1, repeats + int((target - actual) / max(density, 1)) - (1 if actual > target else 0))
    raise RuntimeError("CAPACITY_PROMPT_COUNT_MISMATCH")


def trial(root, cfg, identity, work, *, stress, max_shared_bytes, engine_factory=legacy.temporary_engine):
    row = {"passed": False, "context": cfg["NumCtx"], "stress": stress,
           "samples": [], "prefill": [], "requests": [], "log_directory": str(work / "logs")}
    start = time.monotonic()
    try:
        with engine_factory(root, cfg, work) as fetch:
            legacy.verify_profiles([(cfg, identity)], fetch)
            show = fetch("/api/show", {"model": cfg["Model"]})
            if identity.get("yarn") and any(show.get("model_info", {}).get(k) != v for k, v in identity["yarn"].items()):
                raise RuntimeError("YARN_IDENTITY_MISMATCH")
            thinking = "thinking" in show.get("capabilities", [])
            with GpuSamples(Path(cfg["ExecutablePath"]).name, row["samples"]):
                generate(fetch, cfg, "", 1, think=False if thinking else None, diagnostics=row["requests"], stage="load")
                row["allocation"] = full_gpu(cfg, fetch)
                generate(fetch, cfg, "Write the integers 1 through 32.", 32, think=False if thinking else None,
                         diagnostics=row["requests"], stage="short-generation")
                full_gpu(cfg, fetch)
                if stress:
                    for think in ([False, True] if thinking else [None]):
                        row["prefill"].append(long_input(fetch, cfg, think, row["requests"]))
                        full_gpu(cfg, fetch)
            row["memory"] = validate_samples(row["samples"], max_shared_bytes)
        row["passed"] = True
    except CapacityFailure as error:
        row["failure"] = str(error)
    except Exception as error:
        row["error"] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        if isinstance(error, EngineRequestError):
            row["request_error"] = error.details
        raise
    finally:
        if "memory" not in row and row["samples"]:
            try:
                row["memory"] = validate_samples(row["samples"], max_shared_bytes)
            except (RuntimeError, CapacityFailure) as error:
                row["memory_diagnostic_error"] = str(error)
        row["seconds"] = round(time.monotonic() - start, 3)
        write_report(work / "trial.json", row)
    return row


def plan(root, server_config, models):
    path = legacy.select_config(root, server_config)
    server, raw = legacy.support.read_config(path)
    if server.get("provider") != "Ollama" or not server.get("manage_engine", True):
        raise ValueError("需要实际provider=Ollama、manage_engine=true的服务配置。")
    before, known = {path: raw}, {}
    for key in (PROFILES if models is None else models):
        name, cap = PROFILES[key]
        template = root / f"config/inference.{name}.example.json"
        cfg, data = legacy.support.read_config(template)
        before[template] = data
        if key in ("9b", "qwen38"):
            source = root / f"config/models/{name}.source.json"
            source_cfg, data = legacy.support.read_config(source)
            before[source] = data
            if source_cfg["model"] != cfg["Model"] or source_cfg["native_context"] != cap:
                raise ValueError("固定模型上下文来源不符。")
        known[cfg["Model"]] = (key, cap)
    selected = {}
    for entry in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        target = legacy.resolve(root, entry)
        cfg, data = legacy.support.read_config(target)
        before[target] = data
        if cfg.get("Model") not in known:
            if models is None:
                raise ValueError("存在未支持的已注册模型，请先核对服务配置：" + str(cfg.get("Model")))
            continue
        key, cap = known[cfg["Model"]]
        if key in selected:
            raise ValueError("重复注册模型：" + key)
        if target == path:
            raise ValueError("服务与推理配置路径冲突。")
        lock_path = legacy.resolve(root, cfg["ModelLockPath"])
        identity, data = legacy.support.read_config(lock_path)
        before[lock_path] = data
        selected[key] = (target, cfg, identity, cap)
    if not selected:
        raise ValueError("没有可测试的已注册模型。")
    if models is not None and set(selected) != set(models):
        raise ValueError("缺少已注册模型：" + ", ".join(k for k in models if k not in selected))
    return server, before, selected


def check_128k(root=ROOT, server_config=None, *, models=None, repeats=2,
               max_shared_mib=64, apply=True, probe=trial):
    """Test only 131072; commit passing models, report failures without shrinking."""
    root = Path(root).resolve()
    if type(repeats) is not int or repeats < 2 or max_shared_mib < 0:
        raise ValueError("128K长输入复核至少2轮；共享显存门限不得为负。")
    if models is not None and (not models or len(set(models)) != len(models)
                              or any(key not in PROFILES for key in models)):
        raise ValueError("模型选择无效。")
    server, before, selected = plan(root, server_config, models)
    for target, _, _, _ in selected.values():
        if target.name.endswith(".example.json"):
            raise ValueError("请先将注册的公开推理模板复制为私有配置并更新服务注册：" + str(target))
    work = root / ".local/context-capacity" / ("128k-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    report_path = work / "summary.json"
    report = {"kind": "fixed-128k-gpu-context-check", "context_tokens": 131072,
              "created_at": datetime.now(timezone.utc).isoformat(), "output_limit": 16384,
              "repeats": repeats, "max_shared_mib": max_shared_mib, "applied": False,
              "models": [], "source_sha256": {str(p): hashlib.sha256(v).hexdigest() for p, v in before.items()}}
    changes = []
    try:
        with legacy.offline(root, server):
            legacy.unchanged(before)
            executables = {name for _, cfg, _, _ in selected.values()
                           for name in engine_process_names(Path(cfg["ExecutablePath"]).name)}
            if matching_processes(executables):
                raise RuntimeError("请先退出占用模型库的Ollama／测试引擎；不会终止外部进程。")
            report["hardware"] = HostSampler().sample()
            for key, (target, original, identity, cap) in selected.items():
                model = {"key": key, "model": original["Model"], "config": str(target),
                         "previous_context": original["NumCtx"], "context": 131072,
                         "passed": False, "applied": False, "trials": []}
                report["models"].append(model)
                print("开始128K实测：" + key + "（" + original["Model"] + "）", flush=True)
                candidate = {**deepcopy(original), "NumCtx": 131072, "NumPredict": 16384, "RequireFullGpu": True}
                if "MaxOutputTokens" in candidate:
                    candidate["MaxOutputTokens"] = 16384
                try:
                    if cap < 131072:
                        raise CapacityFailure("MODEL_CONTEXT_LIMIT_EXCEEDED")
                    validate_options(candidate)
                    validate_window(candidate)
                    for repeat in range(repeats):
                        legacy.unchanged(before)
                        location = work / key / ("repeat-" + str(repeat + 1))
                        try:
                            row = probe(root, candidate, identity, location, stress=True,
                                        max_shared_bytes=max_shared_mib * 1024**2)
                        except Exception:
                            trial_path = location / "trial.json"
                            if trial_path.is_file():
                                model["trials"].append(json.loads(trial_path.read_bytes()))
                            raise
                        model["trials"].append(row)
                        write_report(report_path, report)
                        if not row["passed"]:
                            raise CapacityFailure(row.get("failure", "CONTEXT_128K_FAILED"))
                        print(key + " 128K冷加载／长输入第" + str(repeat + 1) + "轮通过。", flush=True)
                    model.update(passed=True, default_input_tokens=131072 - 16384 - candidate["ReservedTokens"],
                                 full_16k_output_verified=False)
                    changes.append((target, candidate))
                except Exception as error:
                    code = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
                    model.update(error=code, capacity_failure=isinstance(error, CapacityFailure))
                    hint = {"GPU_ALLOCATION_FAILED": "显存不足", "CONTEXT_TOO_LARGE": "超过引擎上下文限制",
                            "NOT_FULL_GPU_OR_WINDOW": "未全GPU驻留或实际窗口不足128K",
                            "GPU_SHARED_MEMORY_USED": "共享GPU内存超过门限",
                            "GPU_MEMORY_COUNTERS_UNAVAILABLE": "无法确认显存采样",
                            "MODEL_CONTEXT_LIMIT_EXCEEDED": "超过已锁定模型上下文上限"}.get(code, "测试异常")
                    print("错误：" + key + " 128K测试失败：" + hint + "（" + code + "），保留原上下文。", flush=True)
                write_report(report_path, report)
            legacy.unchanged(before)
            if apply and changes:
                report["backups"] = commit_configs(changes, before, legacy.support, legacy.unchanged)
                report["applied"] = True
                for model in report["models"]:
                    model["applied"] = model["passed"]
            report["completed"] = True
            report["all_passed"] = all(model["passed"] for model in report["models"])
    except BaseException as error:
        report["error"] = "CANCELLED" if isinstance(error, KeyboardInterrupt) else str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        raise
    finally:
        write_report(report_path, report)
        print("报告：" + str(report_path), flush=True)
    return report


def tune(root=ROOT, server_config=None, *, models=tuple(PROFILES), step=1024, repeats=2,
         max_shared_mib=64, vram_fraction=0.95, apply=False, probe=trial):
    root = Path(root).resolve()
    if type(step) is not int or step < 1024 or step % 1024 or repeats < 2 or max_shared_mib < 0:
        raise ValueError("步长须为至少1024的整倍数；长输入复核至少2轮；共享显存门限不得为负。")
    if type(vram_fraction) not in (int, float) or not 0 < vram_fraction <= 1:
        raise ValueError("显存占用比例须大于0且不超过1。")
    if not models or len(set(models)) != len(models) or any(key not in PROFILES for key in models):
        raise ValueError("模型选择无效。")
    server, before, selected = plan(root, server_config, models)
    work = root / ".local/context-capacity" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    report = {"kind": "measured-gpu-context-capacity-not-code-quality", "created_at": datetime.now(timezone.utc).isoformat(),
              "step_tokens": step, "output_limit": 16384, "repeats": repeats, "max_shared_mib": max_shared_mib,
              "vram_limit_fraction": vram_fraction,
              "applied": False, "models": [], "source_sha256": {str(p): hashlib.sha256(v).hexdigest() for p, v in before.items()}}
    report_path = work / "summary.json"
    changes = []
    try:
        with legacy.offline(root, server):
            legacy.unchanged(before)
            executables = {name for _, cfg, _, _ in selected.values()
                           for name in engine_process_names(Path(cfg["ExecutablePath"]).name)}
            if matching_processes(executables):
                raise RuntimeError("请先退出占用模型库的Ollama／测试引擎；不会终止外部进程。")
            hardware = HostSampler().sample()
            report["hardware"] = hardware
            if not any("5080" in gpu["name"] for gpu in hardware["gpus"]):
                raise RuntimeError("RTX_5080_REQUIRED")
            for key in models:
                target, original, identity, cap = selected[key]
                cfg = deepcopy(original)
                cfg.update(NumPredict=16384, RequireFullGpu=True)
                if "MaxOutputTokens" in cfg:
                    cfg["MaxOutputTokens"] = 16384
                validate_options(cfg)
                minimum = max(32768, cfg["ReservedTokens"] + 16384 + 1024)
                model = {"model": cfg["Model"], "key": key, "native_context_limit": cap,
                         "previous_context": cfg["NumCtx"], "kv": cfg.get("KvCacheType", "f16"),
                         "batch": cfg.get("NumBatch"), "trials": []}
                report["models"].append(model)
                print("开始搜索：" + key + "，上界=" + str(cap) + "，步长=" + str(step), flush=True)
                load_cache = {}
                def check(context, stress=False, fraction=1.0):
                    candidate = {**cfg, "NumCtx": context}
                    validate_window(candidate)
                    for repeat in range(repeats if stress else 1):
                        legacy.unchanged(before)
                        cached = not stress and context in load_cache
                        if cached:
                            row = deepcopy(load_cache[context])
                        else:
                            location = work / key / (str(context) + ("-stress-" if stress else "-load-") + uuid4().hex[:6])
                            try:
                                row = probe(root, candidate, identity, location, stress=stress, max_shared_bytes=max_shared_mib*1024**2)
                            except Exception as error:
                                trial_path = location / "trial.json"
                                row = json.loads(trial_path.read_bytes()) if trial_path.is_file() else {
                                    "context": context, "stress": stress, "passed": False,
                                    "error": str(error) if isinstance(error, RuntimeError) else type(error).__name__}
                                row["trial_report"] = str(trial_path)
                                model["trials"].append(row)
                                write_report(report_path, report)
                                raise
                            if not stress:
                                load_cache[context] = deepcopy(row)
                        row["cached"] = cached
                        row["repeat"] = repeat + 1
                        row["vram_limit_fraction"] = fraction
                        row["physical_passed"] = row["passed"]
                        if row["passed"] and row["memory"]["peak_used_fraction"] > fraction:
                            row.update(passed=False, failure="VRAM_BUDGET_EXCEEDED")
                        model["trials"].append(row)
                        write_report(report_path, report)
                        print(key + " " + str(context) + (" 长输入" if stress else " 加载") + "：" +
                              ("通过" if row["passed"] else row.get("failure", "失败")) +
                              ("，显存采样峰值=" + str(round(row["memory"]["peak_used_fraction"]*100, 2)) + "%"
                               if row.get("memory") else ""), flush=True)
                        if not row["passed"]:
                            return False
                    return True
                loaded = largest_context(minimum, cap, step, check)
                model["max_loaded_context"] = loaded
                budgeted = largest_context(minimum, loaded, step, lambda n: check(n, fraction=vram_fraction))
                model["vram_limited_context"] = budgeted
                stable = largest_context(minimum, budgeted, step, lambda n: check(n, True, vram_fraction))
                model.update(max_loaded_context=loaded, selected_context=stable,
                    vram_limited_context=budgeted,
                    default_input_tokens=stable-16384-cfg["ReservedTokens"],
                    upper_bound_reached=stable == cap,
                    input_kind="synthetic-templated-inventory-prefill", full_16k_output_verified=False)
                final = {**original, "NumCtx": stable, "NumPredict": 16384, "RequireFullGpu": True}
                if "MaxOutputTokens" in final:
                    final["MaxOutputTokens"] = 16384
                validate_window(final)
                changes.append((target, final))
                write_report(report_path, report)
            legacy.unchanged(before)
            if apply:
                report["backups"] = commit_configs(changes, before, legacy.support, legacy.unchanged)
                report["applied"] = True
            report["completed"] = True
    except BaseException as error:
        report["error"] = "CANCELLED" if isinstance(error, KeyboardInterrupt) else str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        raise
    finally:
        write_report(report_path, report)
        print("报告：" + str(report_path), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--models", nargs="+", choices=tuple(PROFILES), help="默认测试9B和27B；请先迁移双模型注册")
    parser.add_argument("--context", type=int, choices=(131072,), help="固定测试128K，失败明确报错，不搜索其他窗口")
    parser.add_argument("--step", type=int, default=1024, help="上下文搜索精度，默认1024 token")
    parser.add_argument("--repeats", type=int, default=2, help="每个长输入候选至少2次冷加载复核")
    parser.add_argument("--max-shared-mib", type=int, default=64, help="Ollama进程共享GPU内存峰值门限，默认64MiB；严格零共享可设0")
    parser.add_argument("--vram-fraction", type=float, default=0.95, help="写回窗口的实测GPU总显存占用峰值门限，默认0.95")
    parser.add_argument("--apply", action="store_true", help="备份写回配置；固定128K模式只写通过的模型，搜索模式要求全部完成")
    args = parser.parse_args()
    try:
        if args.context is not None:
            report = check_128k(server_config=args.server_config, models=args.models, repeats=args.repeats,
                                max_shared_mib=args.max_shared_mib, apply=args.apply)
            print("通过模型已备份设置为128K，请启动服务并重启OpenCode入口刷新容量。" if report["applied"]
                  else "实际配置未修改。")
            if not report["all_passed"]:
                print("错误：部分模型未通过128K测试，原上下文保留；详见上述逐模型错误及报告。")
                return 1
            return 0
        tune(server_config=args.server_config, models=args.models or tuple(PROFILES), step=args.step, repeats=args.repeats,
             max_shared_mib=args.max_shared_mib, vram_fraction=args.vram_fraction, apply=args.apply)
        print("完成。" + ("已备份更新配置，请重启服务和OpenCode入口。" if args.apply else "仅测量，实际配置未修改。"))
        return 0
    except KeyboardInterrupt:
        print("已取消，未提交容量配置；原服务由用户从原入口启动。")
    except Exception as error:
        print("容量测试停止：" + (str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
