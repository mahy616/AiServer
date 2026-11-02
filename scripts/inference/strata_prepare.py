"""Pinned Strata preparation. Writes separate private configs; never rewrites the Ollama deployment."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tarfile
from contextlib import nullcontext
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
sys.path.insert(0, str(ROOT / "scripts/server"))
from manager_support import atomic_write, offline, read_config, resolve
from aiserver.inference.strata import digest, strict_json, validate_config, StrataProvider, verify_local_files, is_deepseek
from aiserver.operations.lock import InstanceLock
from aiserver.inference.strata import StrataBudget
from aiserver.inference.contracts import TaskInput
from aiserver.settings import Settings


def download(url, target, sha256, size):
    import requests
    for attempt in range(3):
        try:
            return download_once(url, target, sha256, size)
        except requests.RequestException:
            if attempt == 2:
                raise
            print("Download interrupted; retrying from the verified partial offset", flush=True)
            time.sleep(attempt + 1)


def download_once(url, target, sha256, size):
    """Resume only a matching contiguous Content-Range; publish only a full verified file."""
    import requests
    target = Path(target)
    if target.exists():
        print("Checking SHA256: " + target.name, flush=True)
        if target.stat().st_size == size and digest(target) == sha256:
            return target
        raise ValueError("Existing artifact does not match pinned SHA256: " + target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".download")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > size:
        raise ValueError("Partial download exceeds pinned size")
    if offset == size:
        if digest(partial) != sha256:
            raise ValueError("Partial download hash mismatch; remove that .download file explicitly")
        os.replace(partial, target)
        return target
    with requests.get(url, headers={"Range": f"bytes={offset}-"} if offset else {}, stream=True, timeout=(15, 90)) as response:
        response.raise_for_status()
        resume = bool(offset and response.status_code == 206)
        if resume and response.headers.get("Content-Range") != f"bytes {offset}-{size-1}/{size}":
            raise ValueError("Invalid resume range")
        if response.status_code not in (200, 206):
            raise ValueError("Invalid artifact response")
        count = offset if resume else 0
        started, progress_at, initial = time.monotonic(), time.monotonic(), count
        with partial.open("ab" if resume else "wb") as stream:
            for block in response.iter_content(4 * 1024**2):
                count += len(block)
                if count > size:
                    raise ValueError("Artifact exceeds pinned size")
                stream.write(block)
                now = time.monotonic()
                if now - progress_at >= 5:
                    speed = (count - initial) / max(.001, now - started)
                    print(f"  {target.name}: {100*count/size:.1f}% ({count/1e9:.2f}/{size/1e9:.2f} GB), {speed/1024**2:.1f} MiB/s", flush=True)
                    progress_at = now
    print("Checking SHA256: " + target.name, flush=True)
    if count != size or digest(partial) != sha256:
        raise ValueError("Artifact SHA256/size mismatch: " + target.name)
    os.replace(partial, target)
    return target


def filesystem_path(path):
    """Use extended Win32 paths for archive I/O without requiring host policy changes."""
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def unpack(archive, destination, sha256, *, include=None):
    """Do not extract through links or publish a partially extracted runtime."""
    destination = Path(destination).resolve()
    output = filesystem_path(destination)
    archive = filesystem_path(archive)
    if include is not None and (not isinstance(include, (list, tuple)) or not include or
            not all(isinstance(prefix, str) and prefix for prefix in include)):
        raise ValueError("Invalid archive selection")
    selection = list(include) if include is not None else None
    def selected(name):
        name = name.replace("\\", "/")
        return selection is None or any(name.rstrip("/") == prefix.rstrip("/") or
            prefix.endswith("/") and name.startswith(prefix) for prefix in selection)
    marker = output / ".aiserver-archive.sha256"
    if output.exists():
        manifest = output / ".aiserver-files.json"
        if marker.is_file() and marker.read_text() == sha256 and manifest.is_file():
            previous = json.loads(manifest.read_text())
            # v0.10.0/1 recorded a full extraction as a bare list. Full extractions
            # remain reusable when a newer installer asks for a runtime subset.
            rows = previous if isinstance(previous, list) else previous["files"]
            previous_selection = None if isinstance(previous, list) else previous.get("selection")
            if previous_selection is not None and previous_selection != selection:
                raise ValueError("Existing runtime extraction selection differs")
            for row in rows:
                file = output / row["path"]
                if (not file.resolve().is_relative_to(output.resolve()) or not file.is_file() or
                        file.stat().st_size != row["size"] or digest(file) != row["sha256"]):
                    raise ValueError("Extracted runtime was modified: " + file.name)
            return destination
        raise ValueError("Existing runtime directory differs; keep it and choose a clean destination")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_name(".unpack-" + uuid4().hex[:12])
    stage.mkdir()
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as packed:
                for row in packed.infolist():
                    name = row.filename.replace("\\", "/")
                    if not (stage / name).resolve().is_relative_to(stage.resolve()) or (row.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Unsafe archive entry")
                members = [row for row in packed.infolist() if selected(row.filename)]
                if not any(not row.is_dir() for row in members):
                    raise ValueError("Archive selection contains no files")
                packed.extractall(stage, members=members)
        else:
            with tarfile.open(archive) as packed:
                for row in packed.getmembers():
                    if (not (stage / row.name).resolve().is_relative_to(stage.resolve()) or row.issym() or row.islnk() or
                            not (row.isfile() or row.isdir())):
                        raise ValueError("Unsafe archive entry")
                members = [row for row in packed.getmembers() if selected(row.name)]
                if not any(row.isfile() for row in members):
                    raise ValueError("Archive selection contains no files")
                packed.extractall(stage, members=members, filter="data")
        rows = [{"path": p.relative_to(stage).as_posix(), "size": p.stat().st_size, "sha256": digest(p)}
                for p in stage.rglob("*") if p.is_file()]
        (stage / ".aiserver-files.json").write_bytes(json_bytes({"formatVersion": 2, "selection": selection, "files": rows}))
        (stage / marker.name).write_text(sha256)
        stage.rename(output)
    except BaseException:
        # Leave a named staging directory for diagnosis; never delete an existing installation.
        raise
    return destination


def run(command, *, cwd, env, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError("Preparation step failed; see " + str(log))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def commit_configs(changes, expected=None):
    """Compare every source before writing; restore only our own writes after a failure."""
    before = {Path(path): Path(path).read_bytes() if Path(path).exists() else None for path in changes}
    if expected is not None and before != expected:
        raise ValueError("Configuration changed during preparation")
    written = []
    for path, previous in before.items():
        if previous is not None and previous != changes[path]:
            backup = path.with_name(path.name + ".before-strata-" + uuid4().hex + ".bak")
            backup.write_bytes(previous)
    try:
        for path, data in changes.items():
            current = path.read_bytes() if path.exists() else None
            if current != before[path]:
                raise ValueError("Configuration changed during preparation")
            if current == data:
                continue
            atomic_write(path, data)
            written.append(path)
    except BaseException:
        for path in reversed(written):
            if path.read_bytes() == changes[path]:
                if before[path] is None:
                    path.unlink()
                else:
                    atomic_write(path, before[path])
        raise


def file_record(path, *, expected=None):
    path = Path(path).resolve()
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "sha256": expected or digest(path),
            "mtime_ns": stat.st_mtime_ns, "startup_hash": stat.st_size < 8 * 1024**2}


async def preflight(settings, cfg, identity):
    provider = StrataProvider(settings, cfg, identity)
    try:
        await provider.start()
        profile = type("PreparationProfile", (), {"profile_id": "strata-preflight"})()
        question = "What is the capital of France? Answer in one short English sentence." if is_deepseek(cfg) else "Reply with OK."
        check = await StrataBudget(cfg).prepare(TaskInput(json.dumps({"messages": [{"role": "user", "content": [
            {"type": "text", "text": question}]}], "options": {"num_predict": 64}, "think": False}), ()), profile)
        if check.code:
            raise ValueError(check.code)
        seen = []
        async def delta(text, channel="content"):
            seen.append(text)
        output = await provider.generate(check.prepared, asyncio.Event(), delta)
        if not seen or output.output_tokens == 0:
            raise ValueError("Strata produced no verification tokens")
        if is_deepseek(cfg) and ("paris" not in output.text.lower() or not output.is_complete):
            raise ValueError("DeepSeek failed its known-answer generation; configuration was not published")
    finally:
        await provider.aclose()


def prepare(args):
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise ValueError("The pinned Strata v1 runtime requires 64-bit Windows")
    deepseek = getattr(args, "family", "qwen") == "deepseek"
    suffix = ".deepseek" if deepseek else ""
    source = json.loads((ROOT / ("config/models/strata" + suffix + ".source.json")).read_text(encoding="utf-8"))
    base_path = resolve(ROOT, args.server_config)
    base, base_bytes = read_config(base_path)
    output_server = ROOT / ("config/server.strata" + suffix + ".local.json")
    if base_path == output_server and base.get("provider") != "Strata":
        raise ValueError("Strata output must not overwrite an Ollama configuration")
    cfg_path = ROOT / ("config/inference.strata" + suffix + ".local.json")
    cfg_original = cfg_path.read_bytes() if cfg_path.exists() else None
    cfg = strict_json(cfg_original.decode("utf-8-sig")) if cfg_original is not None else json.loads(
        (ROOT / ("config/inference.strata" + suffix + ".example.json")).read_text(encoding="utf-8"))
    if is_deepseek(cfg) != deepseek:
        raise ValueError("The selected installation entry does not match this model family")
    requested_storage = getattr(args, "storage_root", None)
    storage_value = str(requested_storage) if requested_storage is not None else cfg.get("StorageRoot")
    if storage_value is not None:
        if not isinstance(storage_value, str) or not Path(storage_value).is_absolute():
            raise ValueError("StorageRoot must be an absolute folder on the selected SSD")
        cfg["StorageRoot"] = str(Path(storage_value).resolve())
    storage = Path(cfg["StorageRoot"]) if storage_value is not None else ROOT / ".local/strata"
    cfg["ExecutablePath"] = str(Path(sys.executable).resolve())
    if args.context is not None:
        cfg["NumCtx"] = args.context
    if getattr(args, "max_output", None) is not None:
        cfg["NumPredict"] = args.max_output
        if "MaxOutputTokens" in cfg:
            cfg["MaxOutputTokens"] = args.max_output
    validate_config(cfg)
    if cfg["EngineVersion"] != source["engine_version"] or cfg["RuntimeVersion"] != source["release"]:
        raise ValueError("Strata configuration does not match the pinned release")
    validate_settings = {**base, "provider": "Strata", "manage_engine": True, "additional_inference_configs": [],
        "inference_config": str(cfg_path), "service_startup_timeout_seconds": cfg["StartupTimeoutSeconds"] + 30,
        "execution_timeout_seconds": cfg["GenerateTimeoutSeconds"]}
    server_original = None
    if output_server.exists():
        validate_settings, server_original = read_config(output_server)
        if validate_settings.get("provider") != "Strata":
            raise ValueError("Existing output is not a Strata configuration")
    validate_settings["service_startup_timeout_seconds"] = max(
        validate_settings.get("service_startup_timeout_seconds", 90), cfg["StartupTimeoutSeconds"] + 30)
    if resolve(ROOT, validate_settings["inference_config"]) != cfg_path:
        raise ValueError("Strata output must use " + cfg_path.name)
    targets = (resolve(ROOT, cfg["RunConfigPath"]), resolve(ROOT, cfg["ModelLockPath"]), cfg_path, output_server)
    if len(set(targets)) != 4 or base_path in targets[:3]:
        raise ValueError("Strata output paths must be distinct from the base service configuration")
    other_path = ROOT / ("config/inference.strata" + ("" if deepseek else ".deepseek") + ".local.json")
    if other_path.exists():
        other = strict_json(other_path.read_text(encoding="utf-8-sig"))
        if set(targets) & {resolve(ROOT, other[k]) for k in ("RunConfigPath", "ModelLockPath")}:
            raise ValueError("Qwen and DeepSeek must use independent run configurations and identity locks")
    expected = {p: p.read_bytes() if p.exists() else None for p in targets}
    expected[cfg_path], expected[output_server] = cfg_original, server_original
    settings = Settings(**{**validate_settings,
        "data_directory": resolve(ROOT, validate_settings.get("data_directory", ".local/server")),
        "contract_directory": resolve(ROOT, validate_settings.get("contract_directory", "contracts/api/v1"))})
    work = ROOT / ".local/strata/install" / uuid4().hex
    work.mkdir(parents=True)
    data_lock = offline(ROOT, validate_settings) if settings.data_directory != resolve(ROOT, base.get("data_directory", ".local/server")) else nullcontext()
    storage_lock = InstanceLock(storage / "preparation") if storage.resolve() != (ROOT / ".local/strata").resolve() else nullcontext()
    with InstanceLock(ROOT / ".local/strata/preparation"), offline(ROOT, base), data_lock, storage_lock:
        # The current data lock must be idle before preparation; switching does not consume old queued tasks.
        db = settings.data_directory / "jobs.db"
        if db.exists():
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]:
                    raise ValueError("Finish existing jobs before changing the inference backend")
        print("Strata storage: " + str(storage), flush=True)
        cache = storage / "downloads"
        home = storage / "runtime/v0.2.9" if storage_value is not None else ROOT / ".local/apps/strata/v0.2.9"
        engine_folder = home / ("engine-dsv41" if deepseek else "engine")
        source_folder = home / ("deepseek-source" if deepseek else "source")
        for name, asset in source["artifacts"].items():
            asset = source["artifacts"][name]
            print("Preparing " + name + " (fixed SHA256)", flush=True)
            archive = download(asset["url"], cache / asset["filename"], asset["sha256"], asset["size"])
            output = source_folder if name == "source" else engine_folder if name == "engine" else home / name
            unpack(archive, output, asset["sha256"], include=asset.get("include"))
        runtime = source_folder / "Strata-0.2.9"
        if (digest(runtime / "requirements.txt") != source["requirements_sha256"] or
                not deepseek and digest(runtime / "tools/mtp_fetch.py") != source["mtp_fetch_sha256"]):
            raise ValueError("Pinned upstream preparation scripts differ")
        engines = list(engine_folder.rglob("strata-dsv41.exe" if deepseek else "strata.exe"))
        if len(engines) != 1:
            raise ValueError("Pinned engine archive must contain exactly one model-family executable")
        if deepseek:
            if (digest(runtime / "tools/dsv41/pack_gguf.py") != source["pack_script_sha256"] or
                    digest(runtime / "data/dsv41/profile-2.00.bin") != source["expert_profile_sha256"] or
                    digest(engines[0].parent / "engine-component.json") != source["artifacts"]["engine"]["manifest_sha256"] or
                    strict_json((engines[0].parent / "BUILD.json").read_bytes()).get("version") != cfg["EngineVersion"]):
                raise ValueError("Pinned DeepSeek engine or preparation scripts differ")
        gguf = None if deepseek else home / ("gguf/llama.cpp-" + source["gguf_revision"]) / "gguf-py"
        requirement_path = ROOT / "scripts/inference/strata.requirements.txt"
        requirement_hash = digest(requirement_path)
        dependency = home / "dependencies" / requirement_hash[:16]
        if args.install_dependencies and not dependency.exists():
            stage = home / ("dependencies.prepare-" + uuid4().hex)
            pip_env = os.environ.copy()
            if storage_value is not None:
                pip_env["PIP_CACHE_DIR"] = str(storage / "pip-cache")
            run([sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--no-deps", "--require-hashes", "--target", str(stage),
                "-r", str(requirement_path)], cwd=ROOT, env=pip_env, log=work/"dependencies.log")
            dependency.parent.mkdir(parents=True, exist_ok=True)
            (stage / ".aiserver-requirements.sha256").write_text(requirement_hash)
            stage.rename(dependency)
        if not dependency.is_dir() or (dependency/".aiserver-requirements.sha256").read_text() != requirement_hash:
            raise ValueError("Strata dependencies are missing; use --install-dependencies")
        cfg.update(RuntimePath=str(runtime), DependencyPath=str(dependency), EnginePath=str(engines[0]))
        env = os.environ.copy()
        env.update(PYTHONPATH=os.pathsep.join([str(dependency), str(runtime)]), PYTHONNOUSERSITE="1", PYTHONUTF8="1", STRATA_ALLOW_UNPINNED="0")
        if not deepseek:
            env.update(STRATA_GGUF_PY=str(gguf), STRATA_MTP_REVISION=source["mtp_revision"])
        env.pop("STRATA_SOURCE", None)
        run([sys.executable, "-s", "-c", "import numpy,jinja2,regex,psutil,requests,PIL,yaml"], cwd=runtime, env=env, log=work/"dependencies.log")
        run([sys.executable, "-s", str(runtime / "serve/server.py"), "--help"], cwd=runtime, env=env, log=work/"runtime-check.log")
        if deepseek:
            run([str(engines[0]), "--version"], cwd=runtime, env=env, log=work/"runtime-check.log")
        model_dir = storage / ("models/deepseek41/IQ2_XXS" if deepseek else "models/IQ3_XXS")
        model_dir.mkdir(parents=True, exist_ok=True)
        missing_bytes = sum(max(0, row["size"] - ((model_dir / (row["filename"] + ".download")).stat().st_size
            if (model_dir / (row["filename"] + ".download")).exists() else 0))
            for row in source["model_files"] if not (model_dir / row["filename"]).exists())
        if shutil.disk_usage(model_dir).free < missing_bytes + 12 * 1024**3:
            raise ValueError("Not enough disk space for missing model files, MTP and preparation")
        for row in source["model_files"]:
            target = model_dir / row["filename"]
            if not target.exists() and args.source_models:
                origin = Path(args.source_models) / row["filename"]
                if origin.is_file():
                    if origin.stat().st_size != row["size"] or digest(origin) != row["sha256"]:
                        raise ValueError("Supplied model does not match pinned SHA256")
                    copied = target.with_name(target.name + ".source-copy")
                    shutil.copyfile(origin, copied)
                    if copied.stat().st_size != row["size"] or digest(copied) != row["sha256"]:
                        raise ValueError("Supplied model changed during copy")
                    os.replace(copied, target)
            print("Verifying/downloading " + target.name, flush=True)
            download(row["url"], target, row["sha256"], row["size"])
        native = model_dir / source["model_files"][0]["filename"]
        # Publish references to a new prepared set only after preflight. A failed repeat
        # must not rewrite files (or their mtimes) used by an older identity lock.
        prepared_set = storage / "prepared" / work.name
        prepared_set.mkdir(parents=True)
        pack = prepared_set / "pack"
        if deepseek:
            if (digest(runtime / "tools/dsv41/pack_gguf.py") != source["pack_script_sha256"] or
                    digest(runtime / "data/dsv41/profile-2.00.bin") != source["expert_profile_sha256"]):
                raise ValueError("Pinned DeepSeek preparation scripts differ")
            roles = {role: [model_dir / row["filename"] for row in source["model_files"] if row["role"] == role]
                     for role in ("backbone", "engram", "metadata", "draft")}
            if [len(roles[r]) for r in roles] != [4, 1, 1, 1]:
                raise ValueError("DeepSeek requires four IQ2_XXS shards, Engram, metadata and DSpark")
            run([sys.executable, "-s", str(runtime / "tools/dsv41/pack_gguf.py"),
                "--model", str(model_dir), "--shards", *[p.name for p in roles["backbone"]],
                "--engram", str(roles["engram"][0]), "--engram-meta", str(roles["metadata"][0]),
                "--dspark-gguf", str(roles["draft"][0]), "--dspark-noise-token", "128799",
                "--dspark-markov-rank", "256", "--out", str(pack)], cwd=runtime, env=env, log=work/"prepare.log")
            shutil.copyfile(runtime / "data/dsv41/profile-2.00.bin", pack / "expert-profile.bin")
            # Use the same canonical V4.1 DSML template that the pinned server selects.
            # This also makes the actual rendered template's hash unambiguous at startup.
            template = pack / "tokenizer/chat_template.jinja"
            if " calls>" not in template.read_text(encoding="utf-8"):
                shutil.copyfile(runtime / "serve/fixtures/deepseek41/chat_template.jinja", template)
            libs = sorted({str(p.parent) for p in engine_folder.rglob("*.dll")})
            env["PATH"] = os.pathsep.join([str(engines[0].parent), *libs, env.get("PATH", "")])
            run([str(engines[0]), "--pack", str(pack), "--pack-info"], cwd=runtime, env=env, log=work/"pack-info.log")
            run_config = {"exe": str(engines[0]), "cwd": str(runtime), "lib_dirs": libs, "arch": "deepseek41", "max_queue": 1,
                "env": {"STRATA_DS4_SLOT_KEEP": "0", "STRATA_DS4_AUTO_PIN": "0"},
                "tokenizer": str(pack / "tokenizer"), "model_name": cfg["Model"], "host": "127.0.0.1", "parallel": 1,
                "lazy_load": False, "idle_unload_s": 0, "open_browser": False, "api_monitor": False,
                "fit_max_tokens": False, "answer_room": False, "tool_call_recovery": False,
                "reasoning_close_retry": False, "reasoning_loop_recovery": False, "engine_ready_s": cfg["StartupTimeoutSeconds"],
                "args": ["--pack", str(pack), "--max-context", str(cfg["NumCtx"]), "--eos", "1", "--vision", "off",
                    "--dspark", cfg["DraftMode"], "--dspark-conf", "0.65", "--experts", str(cfg["ActiveExperts"])]}
            native = pack
            artifact_folders = (runtime, engine_folder, dependency, pack)
        else:
            # The native pack indexes experts in place; it does not write another 43 GB expert pool.
            run([sys.executable, "-s", str(runtime/"tools/iq_pack.py"), "--gguf", str(native), "--out", str(pack)], cwd=runtime, env=env, log=work/"prepare.log")
            mtp_source = storage / "mtp-source"
            mtp = prepared_set / "mtp"
            mtp_gguf = prepared_set / "mtp-q2_0.gguf"
            for tool, values in (("mtp_fetch.py", ["fetch", "--out", str(mtp_source)]),
                    ("mtp_fetch.py", ["verify", "--out", str(mtp_source)]),
                    ("mtp_pack.py", ["--src", str(mtp_source), "--experts", "q2_0", "--out", str(mtp_gguf)]),
                    ("mtp_rt.py", ["--gguf", str(mtp_gguf), "--out", str(mtp)])):
                print("Preparing " + tool, flush=True)
                run([sys.executable, "-s", str(runtime/"tools"/tool), *values], cwd=runtime, env=env, log=work/"prepare.log")
            libs = sorted({str(p.parent) for p in engine_folder.rglob("*.dll")})
            run_config = {"exe": str(engines[0]), "cwd": str(runtime), "lib_dirs": libs, "max_queue": 1,
                "tokenizer": str(pack/"tokenizer"), "model_name": cfg["Model"], "host": "127.0.0.1", "parallel": 1,
                "mtp": True, "drafting": "mtp", "lazy_load": False, "idle_unload_s": 0, "open_browser": False,
                "api_monitor": False, "fit_max_tokens": False, "answer_room": False, "tool_call_recovery": False,
                "reasoning_close_retry": False, "reasoning_loop_recovery": False, "engine_ready_s": cfg["StartupTimeoutSeconds"],
                "args": ["--pack", str(pack), "--native", str(native), "--ple-gguf", str(model_dir/source["model_files"][1]["filename"]),
                    "--expert-profile", str(runtime/"data/expert-profile.bin"), "--expert-cache", "auto", "--prefill", "4096",
                    "--spec", "4", "--spec-min-p", "0.5", "--mtp", str(mtp), "--max-context", str(cfg["NumCtx"]),
                    "--kv", "int8", "--kv-resident", "32768", "--vram-reserve-mib", "1536"]}
            artifact_folders = (runtime, engine_folder, gguf, dependency, pack, mtp)
        run_config["log"] = str(work / "engine-native.log")
        run_bytes = json_bytes(run_config)
        files = [file_record(model_dir/row["filename"], expected=row["sha256"]) for row in source["model_files"]]
        for folder in artifact_folders:
            files.extend(file_record(p) for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".log"))
        identity = {"provider": "Strata", "model": cfg["Model"], "engine_version": cfg["EngineVersion"],
            "release": source["release"], "family": cfg.get("ModelFamily", "qwen4exp"),
            "engine_model_path": str(native), "files": files,
            "run_config_sha256": hashlib.sha256(run_bytes).hexdigest(),
            "chat_template_sha256": digest(pack/"tokenizer/chat_template.jinja")}
        temporary_run = work / "run.local.json"
        temporary_run.write_bytes(run_bytes)
        print("Checking model load and a short generation (not a long-context quality test)", flush=True)
        asyncio.run(preflight(settings, {**cfg, "RunConfigPath": str(temporary_run)}, identity))
        verify_local_files({**cfg, "RunConfigPath": str(temporary_run)}, identity)
        if base_path.read_bytes() != base_bytes:
            raise ValueError("Base service configuration changed during preparation")
        changes = {resolve(ROOT, cfg["RunConfigPath"]): run_bytes, resolve(ROOT, cfg["ModelLockPath"]): json_bytes(identity),
                   cfg_path: json_bytes(cfg), output_server: json_bytes(validate_settings)}
        commit_configs(changes, expected)
        report = {"prepared": True, "model": cfg["Model"], "context": cfg["NumCtx"], "server_config": str(output_server),
                  "storage_root": str(storage),
                  "verification": "load-and-short-generation-only", "long_context_and_5080_acceptance": False}
        (work/"summary.json").write_bytes(json_bytes(report))
        print("Prepared. Select this service config in ServerManager: " + str(output_server), flush=True)
        return report
