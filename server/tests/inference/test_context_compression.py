"""Optional offline compression checks; no compressor model or network required."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
if os.environ.get("AISERVER_COMPRESSION_TEST_DEPS"):
    sys.path.insert(0, os.environ["AISERVER_COMPRESSION_TEST_DEPS"])
pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_c_sharp")
spec = importlib.util.spec_from_file_location("compression_checks", ROOT / "scripts/validation/context_compression.py")
compression = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compression)


@pytest.fixture
def tokenizer():
    from tokenizers import Tokenizer, models, pre_tokenizers
    value = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    value.pre_tokenizer = pre_tokenizers.Whitespace()
    return value


def test_layout_preserves_verbatim_strings_comments_and_directives(tokenizer):
    source = '''#define FLAG
namespace Demo
{
    // 禁止重复执行；不是旧上升沿标志。
    class Example
    {
        string label = @"first line
    literal indentation";
        string raw = """
            retain these spaces
            """;
        void Run() { int x = 1 + +2; }
    }
}
'''
    csharp = compression.parser()
    candidate = compression.normalize_layout(source, csharp)
    result = compression.measure(source, candidate, tokenizer, csharp)
    assert result["lossless_gate"]
    assert "\n    literal indentation" in candidate
    assert "\n            retain these spaces" in candidate
    assert "禁止重复执行；不是旧上升沿标志。" in candidate


@pytest.mark.parametrize("replacement", ["38401", "3840", "384000"])
def test_valid_syntax_does_not_hide_changed_numeric_contract(tokenizer, replacement):
    source = 'class C { void F() { Script.SetVarValue(2, "baud", 38400); } }'
    candidate = source.replace("38400", replacement)
    result = compression.measure(source, candidate, tokenizer, compression.parser())
    assert result["syntax_valid"]
    assert not result["lossless_gate"]
    assert result["retained_script_invocation_occurrences"] == 0


def test_invocation_metric_preserves_whitespace_inside_string_literals(tokenizer):
    source = 'class C { void F() { Script.SetVarValue(2, "a b", 1); } }'
    candidate = source.replace('"a b"', '"ab"')
    result = compression.measure(source, candidate, tokenizer, compression.parser())
    assert result["retained_script_invocation_occurrences"] == 0


def test_negation_loss_fails_even_when_executable_code_is_unchanged(tokenizer):
    source = '// 本脚本不修改计数。\nclass C {}'
    candidate = source.replace("不修改", "修改")
    result = compression.measure(source, candidate, tokenizer, compression.parser(),
                                 [{"id": "negation", "pattern": "本脚本不修改计数"}])
    assert result["code_lexical_units_unchanged"]
    assert not result["lossless_gate"]
    assert result["retained_checks"] == 0


def test_invalid_source_is_not_silently_repaired():
    with pytest.raises(ValueError, match="syntax errors"):
        compression.normalize_layout("class C { void F( }", compression.parser())


def test_reference_pack_identifies_observations_instead_of_inventing_sdk_types():
    source = 'class C { void F() { var x = Script.GetValue(67, "Distance"); } }'
    result = compression.source_reference_pack([{"name": "sample.cs", "text": source}], compression.parser())
    assert 'Script.GetValue(67, "Distance")' in result
    assert "不代表完整SDK签名、返回类型或工具契约" in result
    assert "业务算法和控制流须回查原文件" in result


def test_core_pack_keeps_observed_casts_without_claiming_sdk_return_type():
    source = '''using System;
namespace Engine { class ScriptClass {
public static void Execute(Guid key) {
var Script = new Script_Function(key);
double a = (double)Script.GetValue(67, "Distance");
double b = (double)Script.GetValue(68, "Distance");
}}}'''
    result = compression.core_reference_pack([{"name": "sample.cs", "text": source}], compression.parser())
    assert "Engine" in result and "ScriptClass" in result
    assert "public static void Execute(Guid key)" in result
    assert "new Script_Function(key)" in result
    assert 'Script.GetValue(67, "Distance")' in result
    assert "样例中的显式强转：double" in result
    assert "不等同SDK返回类型或完整签名" in result


def asset_preparer():
    spec = importlib.util.spec_from_file_location("compression_assets", ROOT / "scripts/validation/Prepare-ContextCompression.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_asset_fetch_reuses_only_verified_contents(tmp_path):
    import hashlib
    import httpx
    prepare = asset_preparer()
    destination = tmp_path / "asset.bin"
    destination.write_bytes(b"verified")
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected download"))) as client:
        assert prepare.fetch(client, "https://example.invalid/file", destination,
                             hashlib.sha256(b"verified").hexdigest()) == "verified-existing"


def test_corrupt_download_does_not_publish_asset(tmp_path):
    import hashlib
    import httpx
    prepare = asset_preparer()
    destination = tmp_path / "asset.bin"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"wrong"))) as client:
        with pytest.raises(ValueError, match="checksum"):
            prepare.fetch(client, "https://example.invalid/file", destination, hashlib.sha256(b"expected").hexdigest())
    assert not destination.exists()


def test_verified_download_publishes_exact_bytes(tmp_path):
    import hashlib
    import httpx
    prepare = asset_preparer()
    destination = tmp_path / "asset.bin"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"expected"))) as client:
        assert prepare.fetch(client, "https://example.invalid/file", destination,
                             hashlib.sha256(b"expected").hexdigest(), 8) == "downloaded-verified"
    assert destination.read_bytes() == b"expected"
