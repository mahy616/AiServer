"""Offline compression screening with pinned source material and C# preservation gates."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from context_compression import core_reference_pack, digest, inventory, measure, normalize_layout, parser, source_reference_pack


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def read_documents(args):
    documents = []
    if args.source_ref:
        revision = subprocess.check_output(["git", "rev-parse", args.source_ref], cwd=ROOT,
                                           text=True, encoding="utf-8").strip()
        for name in args.git_paths:
            data = subprocess.check_output(["git", "show", revision + ":" + name], cwd=ROOT)
            documents.append({"name": Path(name).name, "text": data.decode("utf-8-sig"),
                              "source": {"revision": revision, "path": name,
                                         "file_sha256": hashlib.sha256(data).hexdigest()}})
    else:
        for path in args.inputs:
            data = path.read_bytes()
            documents.append({"name": path.name, "text": data.decode("utf-8-sig"),
                              "source": {"path": str(path.resolve()),
                                         "file_sha256": hashlib.sha256(data).hexdigest()}})
    if not documents or len({item["name"] for item in documents}) != len(documents):
        raise ValueError("Supply documents with distinct filenames")
    return documents


def load_compressor(args):
    import torch
    from llmlingua import PromptCompressor
    torch.set_num_threads(args.cpu_threads)
    # The pinned local directory must include a supported upstream family name;
    # LLMLingua 0.2.2 otherwise raises NotImplementedError in word merging.
    started = time.perf_counter()
    compressor = PromptCompressor(
        model_name=str(args.compressor_model.resolve()), device_map="cpu",
        model_config={"trust_remote_code": False, "local_files_only": True},
        use_llmlingua2=True, llmlingua2_config={"max_batch_size": 4})
    return compressor, time.perf_counter() - started


def render(report):
    lines = ["# 背景资料压缩筛选", "", "时间：" + report["created_at"], "",
             "范围：离线CPU压缩与C#语法／原文保留检查；未调用9B／27B，未验证回答质量、真实SDK或5080显存。", "",
             "token为锁定Qwen tokenizer的正文计数，不包含聊天模板、文件包装、工具、历史或输出预算。", "",
             "| 方法 | 目标保留率 | 原文token | 候选token | 关键片段 | 语法通过文件 | 全部词法单元相同文件 | 压缩耗时秒 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for condition in report["conditions"]:
        rows = condition["files"]
        lines.append("| " + " | ".join(map(str, [condition["method"], condition.get("rate") or "—",
            sum(item["original_tokens"] for item in rows), sum(item["candidate_tokens"] for item in rows),
            f'{sum(item["retained_checks"] for item in rows)}/{sum(item["total_checks"] for item in rows)}',
            f'{sum(item["syntax_valid"] for item in rows)}/{len(rows)}',
            f'{sum(item["all_lexical_units_unchanged"] for item in rows)}/{len(rows)}',
            round(condition["seconds"], 3)])) + " |")
    lines += ["", "保真门限：C#可解析且包含注释、字符串在内的全部词法单元顺序与内容一致。",
              "失败候选不得替换原文；它们仍保留在candidates中供检查，accepted中只输出通过门限的候选或原文。",
              "关键片段忽略空白后检查，另存精确匹配结果；这是针对历史样例的抽查，不代表所有语义，也不等于模型理解率。", "",
              "reference-pack.md为完整调用索引，core-reference.md为入口与API观察卡；两者均省略业务控制流，不计为全量保真压缩版本。",
              f'入口／API观察卡：{report["core_reference_tokens"]} token；完整调用索引：{report["reference_pack_tokens"]} token。', ""]
    if report.get("compressor"):
        lines.append("压缩器加载耗时：" + str(round(report["compressor"]["load_seconds"], 3)) + "秒。")
    if report.get("process_memory"):
        lines.append("进程内存（含Python／PyTorch／模型，非GPU节省量）：" + json.dumps(report["process_memory"]))
    for note in report["notes"]:
        lines.append("- " + note)
    return "\n".join(lines) + "\n"


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    source = cli.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-ref", help="Git revision to read without restoring deleted files")
    source.add_argument("--inputs", type=Path, nargs="+")
    cli.add_argument("--git-paths", nargs="+", default=["C#脚本示例.txt", "C#脚本示例2.txt"])
    cli.add_argument("--tokenizer", type=Path, required=True)
    cli.add_argument("--checks", type=Path)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--deps-dir", type=Path)
    cli.add_argument("--compressor-model", type=Path)
    cli.add_argument("--tiktoken-cache", type=Path, help="Explicit predownloaded cl100k_base cache for LLMLingua")
    cli.add_argument("--rates", nargs="+", type=float, default=[0.75, 0.5])
    cli.add_argument("--cpu-threads", type=int, default=4)
    args = cli.parse_args()
    if args.cpu_threads < 1 or any(not 0 < value <= 1 for value in args.rates):
        cli.error("CPU threads must be positive and rates must be in (0, 1]")
    if args.output.exists():
        cli.error("Output must be a new directory")
    if args.compressor_model:
        cache_name = hashlib.sha1(b"https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken").hexdigest()
        cache = args.tiktoken_cache / cache_name if args.tiktoken_cache else None
        if not cache or not cache.is_file() or hashlib.sha256(cache.read_bytes()).hexdigest() != "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7":
            cli.error("Supply --tiktoken-cache with the verified cl100k_base asset; benchmark never downloads it")
    if args.deps_dir:
        sys.path.insert(0, str(args.deps_dir.resolve()))
    # Every compressor artifact is explicit and local. No implicit model download,
    # remote code execution or personal model/cache/config discovery is permitted.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    if args.tiktoken_cache:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(args.tiktoken_cache.resolve())
    from tokenizers import Tokenizer
    csharp_parser = parser()
    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    documents = read_documents(args)
    checks = json.loads(args.checks.read_text(encoding="utf-8")) if args.checks else {}
    # Refuse an inapplicable oracle file rather than reporting a misleading low score.
    for item in documents:
        original = measure(item["text"], item["text"], tokenizer, csharp_parser, checks.get(item["name"], []))
        if not original["syntax_valid"] or original["retained_checks"] != original["total_checks"]:
            raise ValueError("Invalid source syntax or checks do not match: " + item["name"])
    args.output.mkdir(parents=True)
    for folder in ("originals", "candidates", "accepted"):
        (args.output / folder).mkdir()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "scope": "offline-csharp-compression-screening-v1",
              "tokenizer_sha256": hashlib.sha256(args.tokenizer.read_bytes()).hexdigest(),
              "documents": [{"name": item["name"], "source": item["source"],
                             "normalized_text_sha256": digest(item["text"])} for item in documents],
              "versions": {}, "conditions": [], "notes": []}
    for package in ("llmlingua", "torch", "transformers", "tokenizers", "tree-sitter", "tree-sitter-c-sharp"):
        try:
            report["versions"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    inventories = []
    for item in documents:
        (args.output / "originals" / item["name"]).write_text(item["text"], encoding="utf-8", newline="\n")
        inventories.append({"name": item["name"], **inventory(item["text"], csharp_parser)})
    write_json(args.output / "inventory.json", inventories)
    reference = source_reference_pack(documents, csharp_parser)
    (args.output / "reference-pack.md").write_text(reference, encoding="utf-8", newline="\n")
    report["reference_pack_tokens"] = len(tokenizer.encode(reference, add_special_tokens=False).ids)
    core = core_reference_pack(documents, csharp_parser)
    (args.output / "core-reference.md").write_text(core, encoding="utf-8", newline="\n")
    report["core_reference_tokens"] = len(tokenizer.encode(core, add_special_tokens=False).ids)

    def run(method, transform, rate=None):
        name = method + ("-" + str(rate) if rate is not None else "")
        rows, started = [], time.perf_counter()
        for item in documents:
            text = transform(item["text"])
            measured = measure(item["text"], text, tokenizer, csharp_parser, checks.get(item["name"], []))
            measured["file"] = item["name"]
            rows.append(measured)
            for folder, value in (("candidates", text), ("accepted", text if measured["lossless_gate"] else item["text"])):
                directory = args.output / folder / name
                directory.mkdir(exist_ok=True)
                (directory / item["name"]).write_text(value, encoding="utf-8", newline="\n")
        report["conditions"].append({"method": method, "rate": rate, "seconds": time.perf_counter() - started, "files": rows})
        write_json(args.output / "report.json", report)
        print(json.dumps({"method": name, "tokens": sum(item["candidate_tokens"] for item in rows),
                          "lossless_files": sum(item["lossless_gate"] for item in rows)}, ensure_ascii=True), flush=True)

    run("layout-preserving", lambda text: normalize_layout(text, csharp_parser))
    if args.compressor_model:
        if "bert-base-multilingual-cased" not in str(args.compressor_model) and "xlm-roberta-large" not in str(args.compressor_model):
            raise ValueError("Local compressor directory must retain the upstream model family name")
        compressor, load_seconds = load_compressor(args)
        report["compressor"] = {"path": str(args.compressor_model.resolve()), "device": "cpu", "load_seconds": load_seconds}
        for rate in args.rates:
            for preserve in (False, True):
                def transform(text):
                    result = compressor.compress_prompt(
                        [text], rate=rate, use_context_level_filter=False,
                        force_tokens=list('\n;(){}.,=<>!+-*/[]"') if preserve else ["\n"],
                        force_reserve_digit=preserve)
                    return result["compressed_prompt"]
                run("llmlingua2-digits-punctuation" if preserve else "llmlingua2-raw", transform, rate)
    try:
        import psutil
        memory = psutil.Process().memory_info()
        report["process_memory"] = {"rss_bytes": memory.rss, "peak_working_set_bytes": getattr(memory, "peak_wset", None)}
    except ImportError:
        pass
    write_json(args.output / "report.json", report)
    (args.output / "report.md").write_text(render(report), encoding="utf-8", newline="\n")
    print(str(args.output.resolve() / "report.md"), flush=True)


if __name__ == "__main__":
    main()
