"""Build/evaluate a submitted single file after the OpenCode session has ended."""
import json
import os
from pathlib import Path
import shutil
import subprocess

from .cases import close_numbers
from .common import run_process, write_json

PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings><Nullable>disable</Nullable>
    <EnableNETAnalyzers>false</EnableNETAnalyzers><EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <UseSharedCompilation>false</UseSharedCompilation>
  </PropertyGroup>
  <ItemGroup><Compile Include="Program.cs"/><Compile Include="Script.cs"/></ItemGroup>
</Project>
"""
HOST = """using System;
using System.Text.Json;
using System.IO;
public static class BenchmarkHost
{
    public static int Main(string[] args)
    {
        var tests=JsonSerializer.Deserialize<double[][]>(File.ReadAllText(args[0]));
        using var output=new StreamWriter(args[1]);
        foreach(var original in tests)
        {
            var input=(double[])original.Clone();
            try
            {
                var result=Script.Run(input);
                bool unchanged=input.Length==original.Length;
                for(int i=0;unchanged && i<input.Length;i++) unchanged=input[i].Equals(original[i]);
                output.WriteLine(JsonSerializer.Serialize(new {ok=true,result,unchanged}));
            }
            catch(Exception error) {output.WriteLine(JsonSerializer.Serialize(new {ok=false,error=error.GetType().Name}));}
            output.Flush();
        }
        return 0;
    }
}
"""


def dotnet_environment(work):
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in
           ("API_KEY", "AISERVER_", "OPENCODE_", "ACCESS_TOKEN"))}
    env.update(DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
               DOTNET_CLI_HOME=str(work / "dotnet-home"), MSBUILDDISABLENODEREUSE="1")
    return env


def check_sdk(dotnet):
    result = subprocess.run([dotnet, "--list-sdks"], capture_output=True, text=True, timeout=20)
    if result.returncode or not any(line.startswith("8.") for line in result.stdout.splitlines()):
        raise ValueError("需要本机安装.NET 8 SDK。")
    return result.stdout.strip()


def evaluate(case, script, work, dotnet="dotnet"):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if not Path(script).is_file():
        return {"compiled": False, "passed": 0, "total": len(case.tests), "error": "未提交Script.cs"}
    source = Path(script).read_text(encoding="utf-8-sig")
    result = {"compiled": False, "passed": 0, "total": len(case.tests), "lines": len(source.splitlines()),
              "within_line_cap": len(source.splitlines()) <= case.cap}
    (work / "Script.cs").write_text(source, encoding="utf-8")
    (work / "Program.cs").write_text(HOST, encoding="utf-8")
    (work / "Harness.csproj").write_text(PROJECT, encoding="utf-8")
    (work / "NuGet.Config").write_text('<configuration><packageSources><clear/></packageSources></configuration>', encoding="utf-8")
    write_json(work / "inputs.json", case.tests)
    env = dotnet_environment(work)
    build = run_process([dotnet, "build", "Harness.csproj", "-c", "Release", "--nologo", "-v", "quiet",
                         "--disable-build-servers"], work, env, 90, work / "build")
    result["build"] = build
    result["compiled"] = build["exit_code"] == 0 and not build["timed_out"]
    if not result["compiled"]:
        result["error"] = "编译超时" if build["timed_out"] else "编译失败"
        return result
    run = run_process([dotnet, str(work / "bin/Release/net8.0/Harness.dll"), str(work / "inputs.json"),
                       str(work / "results.jsonl")], work, env, 15, work / "execution", limit=1024*1024)
    result["execution"] = run
    rows = []
    path = work / "results.jsonl"
    if path.is_file() and path.stat().st_size < 8*1024*1024:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"ok": False})
    checks = [i < len(rows) and rows[i].get("ok") is True and rows[i].get("unchanged") is True
              and close_numbers(rows[i].get("result"), expected) for i, expected in enumerate(case.expected)]
    result.update(passed=sum(checks), failed_indices=[i for i, passed in enumerate(checks) if not passed])
    if run["timed_out"]:
        result["error"] = "脚本运行超时"
    elif run["exit_code"]:
        result["error"] = "脚本进程异常退出"
    result["all_passed"] = (all(checks) and len(rows) == len(case.tests) and run["exit_code"] == 0
                            and not run["timed_out"] and result["within_line_cap"])
    return result
