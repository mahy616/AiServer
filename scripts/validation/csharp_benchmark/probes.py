"""Bounded independent API context probes, separate from OpenCode task scores."""
import json
import re
import time

from .common import append_json, utc
from .relay import completion


def material(characters):
    prefix = ("Read this synthetic C# material. Return only JSON with start, middle, end and computed. "
              "computed = Start * Middle + End. Do not write code.\nconst int Start = 137;\n")
    center = "const int Middle = 293;\n"
    suffix = "const int End = 419;\nReturn the four requested numeric properties only."
    example = "static double Part00000(double x) => (x * 1.00 + 00.25) / 1.01;\n"
    rows = max(1, (characters-len(prefix)-len(center)-len(suffix))//len(example))
    left = [f"static double Part{i:05d}(double x) => (x * 1.{i%37:02d} + {i%19:02d}.25) / 1.01;\n" for i in range(rows)]
    start, middle, end = 137, 293, 419
    content = prefix + "".join(left[:rows//2]) + center + "".join(left[rows//2:]) + suffix
    content += " " * max(0, characters-len(content))
    return content, {"start": start, "middle": middle, "end": end, "computed": start*middle+end}


def correct(text, expected):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(text[index:])
                if value == expected:
                    return True
            except ValueError:
                pass
    return False


def answer_parts(text, expected):
    values = {}
    for key in ("start", "middle", "end"):
        match = re.search(r'"'+key+r'"\s*:\s*(-?\d+)\s*[,}]', text, re.I)
        if match:
            values[key] = int(match[1])
    parsed = None
    for i, char in enumerate(text):
        if char == "{":
            try:
                parsed, _ = json.JSONDecoder().raw_decode(text[i:])
                if isinstance(parsed, dict):
                    break
            except ValueError:
                continue
    return {"retrieval_correct": all(values.get(k) == expected[k] for k in ("start", "middle", "end")),
            "format_correct": isinstance(parsed, dict) and set(parsed) == set(expected)
                and all(type(value) in (int, float) for value in parsed.values()),
            "calculation_correct": isinstance(parsed, dict) and
                next((v for k, v in parsed.items() if k.lower() == "computed"), None) == expected["computed"]}


def exercise(recorder, model_info, save, previous=(), publish=None):
    limits = model_info["limits"]
    default_output = min(limits["maxOutputTokens"], 4096)
    context, reserved = limits["contextTokens"], limits.get("reservedTokens", 512)
    small_output = min(default_output, 1024)
    default_limit = context-default_output-reserved
    small_limit = context-small_output-reserved
    conservative = model_info.get("capacityMethod") == "conservative-estimate-v1"
    ratio = 2.0 if conservative else 3.0
    sizes = sorted(set(max(512, int(default_limit*fraction)) for fraction in (.2, .4, .6, .8)))
    schedule = [(f"input-{n}", n, default_output, "approximate-actual") for n in sizes]
    schedule += [("default-near", int(default_limit*.98), default_output, "admission-boundary"),
                 ("default-over", int(default_limit*1.02), default_output, "admission-boundary"),
                 ("small-near", int(small_limit*.98), small_output, "admission-boundary"),
                 ("small-over", int(small_limit*1.02), small_output, "admission-boundary")]
    rows = list(previous)
    finished = {r["id"] for r in rows}
    bounds = {}
    def update_bounds(row):
        output = row["output_budget"]
        lower, upper = bounds.get(output, (None, None))
        if row.get("http_status") == 200 and row.get("done") and not row.get("error"):
            lower = max(lower or 0, row["input_characters"])
        elif row.get("http_status") == 400 or "context" in json.dumps(row.get("error", "")).lower():
            upper = min(upper or float("inf"), row["input_characters"])
        bounds[output] = (lower, upper)
    for old in rows:
        update_bounds(old)
    def probe(label, target, output, mode, forced_chars=None):
        nonlocal ratio
        if hasattr(recorder, "terminal_error"):
            # A preflight 400 is an expected boundary sample. Each subsequent
            # probe is a new intentional request, not an SDK retry of that error.
            recorder.terminal_error.clear()
        characters = forced_chars or int(max(500, target-160) * (2.0 if conservative and mode == "admission-boundary" else ratio))
        prompt, expected = material(characters)
        recorder.phase = "context-" + label
        recorder.deadline = time.monotonic() + 600
        row = {"id": label, "at": utc(), "target_tokens": target, "target_kind": mode,
               "thinking": model_info.get("benchmark_thinking", False),
               "output_budget": output, "input_characters": len(prompt), "answer_correct": False}
        print(f"  上下文 {label}: 目标约{target} tokens，实际以usage为准", flush=True)
        try:
            reply = completion(recorder.url, recorder.token,
                               {"model": recorder.model, "stream": True, "stream_options": {"include_usage": True},
                                "think": model_info.get("benchmark_thinking", False),
                                "max_tokens": output, "messages": [{"role": "user", "content": prompt}]}, 600)
            row.update(reply)
            row["answer_correct"] = bool(reply.get("done") and not reply.get("error") and correct(reply["answer"], expected))
            row.update(answer_parts(reply.get("answer", ""), expected))
            actual = (reply.get("usage") or {}).get("prompt_tokens")
            row["actual_input_tokens"] = actual
            if actual and reply.get("done"):
                ratio = len(prompt)/actual
        except Exception as error:
            row["error"] = type(error).__name__
        rows.append(row)
        update_bounds(row)
        append_json(save, row)
        if publish:
            publish(rows)
        return row
    for label, target, output, mode in schedule:
        if label in finished:
            actual = next(r for r in rows if r["id"] == label)
            if actual.get("actual_input_tokens"):
                ratio = actual["input_characters"]/actual["actual_input_tokens"]
            continue
        row = probe(label, target, output, mode)
        if row.get("http_status", 500) >= 500 or row.get("error") and row.get("http_status") != 400:
            return rows
    refine_output = small_output if all(bounds.get(small_output, (None, None))) else default_output
    low, high = bounds.get(refine_output, (None, None))
    if low and high and high > low:
        for step in range(3):
            if len(rows) >= 12:
                break
            label = "refine-" + str(step+1)
            if label in finished:
                continue
            midpoint = (low+high)//2
            row = probe(label, int(midpoint/ratio), refine_output, "refinement", midpoint)
            if row.get("http_status", 500) >= 500 or row.get("error") and row.get("http_status") != 400:
                return rows
            low, high = bounds[refine_output]
    return rows
