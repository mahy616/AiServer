"""Compile and simulate an inspected generated script; no real SDK/devices."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--code", required=True, type=Path, help="Exact extracted model code; inspect it first")
args = parser.parse_args()
code = args.code.resolve()
case = ROOT / "eval/cases/gap-threshold-v1"
tests = json.loads((case / "case.json").read_text(encoding="utf-8"))["tests"]
directory = code.parent / (code.stem + "-simulation")
directory.mkdir(exist_ok=True)


def literal(value):
    if value == "NaN":
        return "double.NaN"
    if value == "Infinity":
        return "double.PositiveInfinity"
    return str(value) + "d"


calls = []
for test in tests:
    calls.append('Simulation.Check(%s,%s,%s,%s,%s,%d,%s,%s);' % (
        json.dumps(test["name"]), literal(test["gap"]), literal(test["low"]), literal(test["high"]),
        str(test.get("throwOnRead", False)).lower(), test["status"], json.dumps(test["message"]), literal(test["writtenGap"])))
entry = 'public static class Entry { public static int Main() {\n' + "\n".join(calls) + '\nreturn Simulation.Failures == 0 ? 0 : 1; } }'
(directory / "Entry.cs").write_text(entry, encoding="utf-8-sig")
exe = directory / "simulation.exe"
compiler = "C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe"
compiled = subprocess.run([compiler, "/nologo", "/utf8output", "/target:exe", f"/out:{exe}",
                           str(code), str(case / "SimulationHarness.cs"), str(directory / "Entry.cs")],
                          capture_output=True, timeout=30)
try:
    diagnostic = compiled.stdout.decode("utf-8-sig")
except UnicodeDecodeError:
    diagnostic = compiled.stdout.decode("mbcs", errors="replace")
(directory / "compiler-output.bin").write_bytes(compiled.stdout)
result = {"codeSha256": hashlib.sha256(code.read_bytes()).hexdigest(), "compiler": compiler,
          "compileExitCode": compiled.returncode, "compileOutput": diagnostic,
          "validationScope": "synthetic SDK shim only; actual host compilation/business not_run"}
if compiled.returncode == 0:
    executed = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    result.update({"simulationExitCode": executed.returncode, "simulationOutput": executed.stdout})
(directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(result.get("simulationExitCode", compiled.returncode))
