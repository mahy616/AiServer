using System.Text;
using System.Text.Json.Nodes;

namespace AiServer.Demo;

internal static class RequestFiles
{
    public static void Prepare(string[] args)
    {
        if (args.Length < 4 || (args.Length - 2) % 2 != 0)
            throw new ArgumentException("prepare <output.json> --instruction <file> [--context file] [--code file] [--image PNG] [--operation create|modify|explain] [--parent jobId] [--feedback file] [--history file]");
        var options = new Dictionary<string, string>();
        var allowed = new[] { "--instruction", "--context", "--code", "--image", "--operation", "--parent", "--feedback", "--history" };
        for (int i = 2; i < args.Length; i += 2)
            if (!allowed.Contains(args[i]) || !options.TryAdd(args[i], args[i + 1]))
                throw new ArgumentException("Unknown or repeated option");
        string? Read(string key) => options.TryGetValue(key, out var file) ? File.ReadAllText(file, Encoding.UTF8) : null;
        var instruction = Read("--instruction") ?? throw new ArgumentException("--instruction is required");
        if (options.ContainsKey("--feedback")) instruction += "\n本轮检查反馈：\n" + Read("--feedback");
        var context = Read("--context") is string raw ? JsonNode.Parse(raw)!.AsObject() : new JsonObject
        { ["schemaVersion"] = 1, ["targetFramework"] = ".NETFramework,Version=v4.7.2" };
        var history = Read("--history") is string historyJson ? JsonNode.Parse(historyJson)!.AsArray() : new JsonArray();
        var images = new JsonArray();
        if (options.TryGetValue("--image", out var image))
            images.Add(new JsonObject { ["imageId"] = "flow-1", ["mediaType"] = "image/png",
                ["dataBase64"] = Convert.ToBase64String(File.ReadAllBytes(image)) });
        var code = Read("--code");
        var operation = options.GetValueOrDefault("--operation", code is null ? "create" : "modify");
        if (!new[] { "create", "modify", "explain" }.Contains(operation) || operation == "modify" && string.IsNullOrWhiteSpace(code))
            throw new ArgumentException("Invalid operation or missing code for modify");
        var request = new JsonObject { ["clientRequestId"] = Guid.NewGuid().ToString(), ["operation"] = operation,
            ["instruction"] = instruction, ["currentCode"] = code, ["context"] = context,
            ["history"] = history, ["images"] = images,
            ["parentJobId"] = options.TryGetValue("--parent", out var parent) ? Guid.Parse(parent).ToString() : null };
        // Refuse accidental overwrite of a previously submitted identity.
        using var output = new FileStream(Path.GetFullPath(args[1]), FileMode.CreateNew);
        using var writer = new StreamWriter(output, new UTF8Encoding(false));
        writer.Write(request.ToJsonString(new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine("savedRequest=" + Path.GetFullPath(args[1]));
    }

    public static string SaveResult(string directory, string id, JsonObject result)
    {
        var path = Path.Combine(directory, id + ".json");
        File.WriteAllText(path, result.ToJsonString(), new UTF8Encoding(false));
        var text = result["output"]?["text"]?.GetValue<string>();
        if (text is not null) File.WriteAllText(Path.Combine(directory, id + ".txt"), text, new UTF8Encoding(false));
        return path;
    }
}
