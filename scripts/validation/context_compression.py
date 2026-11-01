"""Offline material checks. Optional compressors never run in the HTTP service."""
from collections import Counter
import hashlib
import re


ATOMIC = {"comment", "string_literal", "verbatim_string_literal", "raw_string_literal",
          "interpolated_string_expression", "character_literal"}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def parser():
    from tree_sitter import Language, Parser
    import tree_sitter_c_sharp
    return Parser(Language(tree_sitter_c_sharp.language()))


def atoms(node, data, *, comments=True):
    if node.type == "comment" and not comments:
        return []
    if node.type in ATOMIC or not node.children:
        return [(node.type, data[node.start_byte:node.end_byte])]
    return [item for child in node.children for item in atoms(child, data, comments=comments)]


def lexical_units(node):
    if node.type in ATOMIC or not node.children:
        return [node]
    return [item for child in node.children for item in lexical_units(child)]


def normalize_layout(text, csharp_parser):
    """Remove redundant whitespace only; preserve line breaks and literal contents."""
    data = text.encode("utf-8")
    root = csharp_parser.parse(data).root_node
    if root.has_error:
        raise ValueError("Source C# has syntax errors; refusing normalization")
    units = lexical_units(root)
    output, previous = [], 0
    for unit in units:
        gap = data[previous:unit.start_byte]
        if gap and output:
            output.append(b"\n" if b"\n" in gap else b" ")
        output.append(data[unit.start_byte:unit.end_byte])
        previous = unit.end_byte
    candidate = b"".join(output).decode("utf-8") + "\n"
    checked = csharp_parser.parse(candidate.encode()).root_node
    if checked.has_error or atoms(root, data) != atoms(checked, candidate.encode()):
        raise ValueError("Lexical preservation check failed")
    return candidate


def inventory(text, csharp_parser):
    data = text.encode("utf-8")
    root = csharp_parser.parse(data).root_node
    invocations, entries, comments = [], [], []
    namespaces, classes, constructors, usings = [], [], [], []
    for node in walk(root):
        value = data[node.start_byte:node.end_byte].decode("utf-8")
        if node.type == "invocation_expression" and value.startswith("Script."):
            casts, parent = [], node.parent
            while parent and parent.type not in {"expression_statement", "local_declaration_statement"}:
                if parent.type == "cast_expression":
                    cast_type = parent.child_by_field_name("type")
                    if cast_type:
                        casts.append(data[cast_type.start_byte:cast_type.end_byte].decode("utf-8"))
                parent = parent.parent
            invocations.append({"line": node.start_point.row + 1, "text": value, "observed_casts": casts})
        elif node.type == "method_declaration":
            body = node.child_by_field_name("body")
            signature = data[node.start_byte:body.start_byte if body else node.end_byte].decode("utf-8").strip()
            entries.append({"line": node.start_point.row + 1, "signature": signature})
        elif node.type == "comment":
            comments.append({"line": node.start_point.row + 1, "text": value})
        elif node.type in {"namespace_declaration", "file_scoped_namespace_declaration", "class_declaration"}:
            name = node.child_by_field_name("name")
            if name:
                target = classes if node.type == "class_declaration" else namespaces
                target.append(data[name.start_byte:name.end_byte].decode("utf-8"))
        elif node.type == "object_creation_expression" and re.match(r"new\s+Script_Function\s*\(", value):
            constructors.append(value)
        elif node.type == "using_directive":
            usings.append(value)
    return {"syntax_valid": not root.has_error, "methods": entries,
            "script_invocations": invocations, "comments": comments,
            "namespaces": namespaces, "classes": classes, "constructors": constructors, "usings": usings}


def measure(original, candidate, tokenizer, csharp_parser, checks=()):
    before, after = original.encode(), candidate.encode()
    first, second = csharp_parser.parse(before).root_node, csharp_parser.parse(after).root_node
    def calls(root, data):
        return Counter(tuple(atoms(node, data, comments=False)) for node in walk(root)
                       if node.type == "invocation_expression"
                       and data[node.start_byte:node.end_byte].startswith(b"Script."))
    required, observed = calls(first, before), calls(second, after)
    retained_calls = sum((required & observed).values())
    compact = re.sub(r"\s+", "", candidate)
    facts = []
    for item in checks:
        # A compressor may insert spaces between Chinese characters and punctuation.
        # Report fragment coverage without treating that formatting alone as deletion;
        # executable token equality is checked independently and remains strict.
        pattern = item["pattern"].replace(r"\s+", "").replace(r"\s*", "").replace(" ", "")
        facts.append({"id": item["id"], "retained": bool(re.search(pattern, compact, re.S)),
                      "exact_pattern_retained": bool(re.search(item["pattern"], candidate, re.S))})
    original_tokens = len(tokenizer.encode(original, add_special_tokens=False).ids)
    candidate_tokens = len(tokenizer.encode(candidate, add_special_tokens=False).ids)
    lexical_same = atoms(first, before) == atoms(second, after)
    code_same = atoms(first, before, comments=False) == atoms(second, after, comments=False)
    return {"original_tokens": original_tokens, "candidate_tokens": candidate_tokens,
            "retained_token_fraction": candidate_tokens / original_tokens if original_tokens else 1,
            "saved_tokens": original_tokens - candidate_tokens,
            "syntax_valid": not second.has_error, "all_lexical_units_unchanged": lexical_same,
            "code_lexical_units_unchanged": code_same,
            "script_invocation_occurrences": sum(required.values()),
            "retained_script_invocation_occurrences": retained_calls,
            "checks": facts, "retained_checks": sum(item["retained"] for item in facts),
            "total_checks": len(facts),
            "lossless_gate": not second.has_error and lexical_same,
            "source_sha256": digest(original), "candidate_sha256": digest(candidate)}


def source_reference_pack(documents, csharp_parser):
    """An index for later retrieval, explicitly not a replacement for full examples."""
    output = ["# C#样例入口与接口索引", "",
              "以下仅为样例中观察到的入口、调用与注释；不代表完整SDK签名、返回类型或工具契约。",
              "业务算法和控制流须回查原文件。文件SHA256和行号见inventory.json。", ""]
    for document in documents:
        item = inventory(document["text"], csharp_parser)
        output.extend(["## " + document["name"], "", "入口与辅助方法："])
        output.extend("- `" + method["signature"] + "`" for method in item["methods"])
        output.extend(["", "观察到的Script调用（保留完整实参，合并完全相同的调用）：", "```csharp"])
        output.extend(dict.fromkeys(call["text"] + ";" for call in item["script_invocations"]))
        output.extend(["```", "", "原始注释："])
        output.extend(comment["text"] for comment in item["comments"])
        output.append("")
    return "\n".join(output)


def core_reference_pack(documents, csharp_parser):
    """Small, scope-limited script/API observation card; no full-source fidelity claim."""
    items = [(document["name"], inventory(document["text"], csharp_parser)) for document in documents]
    unique = lambda values: list(dict.fromkeys(values))
    output = ["# 脚本入口与常用API观察卡", "",
              "范围仅限脚本入口、依赖和已观察到的调用；不替代业务算法、串口协议或工具输入输出契约。",
              "精确参数及控制流请按下列原文件行号回查；完整索引见inventory.json。", ""]
    for label, key in (("命名空间", "namespaces"), ("类名", "classes"), ("脚本对象构造", "constructors")):
        output.append(label + "：" + "；".join(unique(value for _, item in items for value in item[key])))
    output.append("Execute入口：" + "；".join(unique(method["signature"] for _, item in items
                   for method in item["methods"] if re.search(r"\bExecute\s*\(", method["signature"]))))
    output.extend(["", "依赖（示例包含项，未判断是否全部必需）：", "```csharp"])
    output.extend(unique(value for _, item in items for value in item["usings"]))
    output.extend(["```", "", "## 观察到的Script调用", "",
                   "每种方法保留一个原文调用；强制转换类型仅描述样例用法，不等同SDK返回类型或完整签名。", ""])
    methods = {}
    for name, item in items:
        for call in item["script_invocations"]:
            method = re.match(r"Script\.(\w+)", call["text"])[1]
            entry = methods.setdefault(method, {"sample": call["text"], "source": f'{name}:{call["line"]}', "casts": []})
            entry["casts"].extend(call["observed_casts"])
    for method, entry in methods.items():
        output.extend(["### " + method, "```csharp", entry["sample"] + ";", "```",
                       "来源：" + entry["source"]])
        if entry["casts"]:
            output.append("样例中的显式强转：" + "、".join(unique(entry["casts"])))
        output.append("")
    return "\n".join(output)
