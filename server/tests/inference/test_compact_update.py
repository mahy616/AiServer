import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location("compact_build",ROOT/"scripts/deployment/Build-Update.py")
builder=importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)


def source_tree(tmp_path):
    root=tmp_path/"source"
    files={
        "server/pyproject.toml":'[project]\nversion="0.4.1"\n',
        "server/requirements.lock.txt":"example==1.0\n",
        "server/src/aiserver/__init__.py":"# synthetic code\n",
        "server/src/aiserver/storage/003.sql":"PRAGMA user_version=3;\n",
        "contracts/api/v1/job-request.schema.json":"{}\n",
        "scripts/server/Run.py":"# synthetic\n", "scripts/server/Manage.py":"# synthetic\n",
        "scripts/server/ServerManager.py":"# synthetic\n", "scripts/server/ManagerActions.py":"# synthetic\n",
        "scripts/server/manager_support.py":"# synthetic\n", "ServerManager.cmd":"@echo off\n",
        ".local/ollama/model.bin":"PRIVATE_MODEL_SENTINEL",
        ".local/releases/old.zip":"OLD_RELEASE_SENTINEL", "config/server.local.json":"PRIVATE_CONFIG_SENTINEL",
        "config/private.ini":"PRIVATE_TOKEN_SENTINEL", "server/src/aiserver/__pycache__/x.pyc":"CACHE_SENTINEL",
        "client/output.exe":"CLIENT_BUILD_SENTINEL", "natapp.exe":"ENGINE_SENTINEL"}
    for name,text in files.items():
        path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text,encoding="utf-8")
    return root


def test_compact_complete_update_excludes_runtime_and_previous_archives(tmp_path):
    root=source_tree(tmp_path)
    first=builder.build(root)
    second=builder.build(root)
    assert first["zip"]!=second["zip"]
    with zipfile.ZipFile(first["zip"]) as archive:
        names=archive.namelist()
        assert set(names)=={"manifest.json","UPDATE.txt"}|{
            "payload/"+row["path"] for row in json.loads(archive.read("manifest.json"))["files"]}
        assert not any(".local" in Path(name).parts or "__pycache__" in Path(name).parts or name.endswith(".exe") for name in names)
        all_bytes=b"".join(archive.read(name) for name in names)
        for sentinel in (b"PRIVATE_MODEL_SENTINEL",b"PRIVATE_CONFIG_SENTINEL",b"PRIVATE_TOKEN_SENTINEL",b"OLD_RELEASE_SENTINEL",b"CLIENT_BUILD_SENTINEL"):
            assert sentinel not in all_bytes
        manifest=json.loads(archive.read("manifest.json"))
        assert manifest["kind"]=="complete-server-source-update"
        assert manifest["fileCount"]==11
        for row in manifest["files"]:
            assert hashlib.sha256(archive.read("payload/"+row["path"])).hexdigest()==row["sha256"]
        assert "ServerManager.cmd" in [row["path"] for row in manifest["files"]]
    assert first["zipBytes"]<10000


def test_inventory_rejects_private_runtime_via_link(tmp_path):
    root=source_tree(tmp_path)
    linked=root/"server/src/aiserver/unsafe.py"
    target=root/".local/private.py"; target.write_text("PRIVATE_SENTINEL",encoding="utf-8")
    try: linked.symlink_to(target)
    except OSError:
        # Unprivileged Windows may not permit symlinks; validate an equivalent resolved path.
        original=builder.inventory_module.Path.resolve
        from unittest.mock import patch
        def resolving(path,*args,**kwargs):
            return original(target) if path.name=="__init__.py" else original(path,*args,**kwargs)
        with patch.object(Path,"resolve",resolving):
            import pytest
            with pytest.raises(ValueError): builder.build(root)
    else:
        import pytest
        with pytest.raises(ValueError): builder.build(root)
