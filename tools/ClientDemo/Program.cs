using AiServer.Client;
using AiServer.Demo;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;

Console.OutputEncoding = Encoding.UTF8;
if (args.Length < 1)
{
    Console.Error.WriteLine("prepare <output.json> --instruction <file> [options] | submit <request.json> | resume <clientRequestId> | cancel/status/result <jobId> | list | ready");
    return 2;
}
if (args[0] == "prepare")
{
    try { RequestFiles.Prepare(args); return 0; }
    catch (Exception error) when (error is ArgumentException or IOException or System.Text.Json.JsonException)
    { Console.Error.WriteLine("Prepare failed: " + error.Message); return 2; }
}
var token = Environment.GetEnvironmentVariable("AISERVER_TOKEN");
if (string.IsNullOrWhiteSpace(token))
{
    Console.Error.WriteLine("Set AISERVER_TOKEN in this process environment.");
    return 2;
}
using var api = new ApiClient(Environment.GetEnvironmentVariable("AISERVER_BASE_URL") ?? "http://127.0.0.1:18080", token);
var data = Path.GetFullPath(Environment.GetEnvironmentVariable("AISERVER_DEMO_DATA") ?? ".local/client");
Directory.CreateDirectory(Path.Combine(data, "requests"));
Directory.CreateDirectory(Path.Combine(data, "results"));


try
{
    if (args[0] == "ready")
    {
        Console.WriteLine((await api.Send(HttpMethod.Get, "api/v1/ready")).ToJsonString());
        return 0;
    }
    if (args[0] == "list")
    {
        Console.WriteLine((await api.Query("api/v1/jobs")).ToJsonString());
        return 0;
    }
    if (args[0] is "status" or "result" && args.Length >= 2)
    {
        var job = Guid.Parse(args[1]).ToString();
        Console.WriteLine((await api.Query("api/v1/jobs/" + job + (args[0] == "result" ? "/result" : ""))).ToJsonString());
        return 0;
    }
    if (args.Length < 2) throw new ArgumentException("Missing request file or UUID");
    if (args[0] == "cancel")
    {
        string jobId = Guid.Parse(args[1]).ToString();
        var result = await api.Send(HttpMethod.Post, "api/v1/jobs/" + jobId + "/cancel");
        Console.WriteLine(result.ToJsonString());
        return 0;
    }
    string requestId;
    JsonObject accepted;
    if (args[0] == "submit")
    {
        var serialized = File.ReadAllText(args[1], Encoding.UTF8);
        var parsed = JsonNode.Parse(serialized)?.AsObject() ?? throw new ArgumentException("Invalid request JSON");
        requestId = Guid.Parse(parsed["clientRequestId"]!.GetValue<string>()).ToString();
        string saved = Path.Combine(data, "requests", requestId + ".json");
        if (File.Exists(saved) && File.ReadAllText(saved, Encoding.UTF8) != serialized)
            throw new ArgumentException("Saved request differs; use a new clientRequestId.");
        File.WriteAllText(saved, serialized, new UTF8Encoding(false)); // before network submission
        Console.WriteLine("clientRequestId=" + requestId);
        try { accepted = await api.Send(HttpMethod.Post, "api/v1/jobs", serialized, true); }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException ||
                                      error is ApiFailure f && f.Status >= 500)
        { accepted = await api.Recover(requestId, serialized); }
    }
    else if (args[0] == "resume")
    {
        requestId = Guid.Parse(args[1]).ToString();
        var saved = Path.Combine(data, "requests", requestId + ".json");
        accepted = await api.Recover(requestId, File.Exists(saved) ? File.ReadAllText(saved, Encoding.UTF8) : null);
    }
    else throw new ArgumentException("Unknown command");
    string id = accepted["jobId"]!.GetValue<string>();
    Console.WriteLine("jobId=" + id);
    try { Console.WriteLine("inferenceProvider=" + (await api.Send(HttpMethod.Get, "api/v1/ready"))["inferenceProvider"]); }
    catch (ApiFailure error) { Console.WriteLine("readyUnavailable=" + error.Code); }
    string? previous = null;
    bool stop = args.Contains("--cancel-on-running");
    Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop = true; };
    while (true)
    {
        var state = await api.Query("api/v1/jobs/" + id);
        string status = state["status"]!.GetValue<string>();
        if (status != previous)
        {
            Console.WriteLine("status=" + status);
            previous = status;
        }
        if (status is "succeeded" or "failed" or "cancelled")
        {
            var result = await api.Query("api/v1/jobs/" + id + "/result");
            var output = RequestFiles.SaveResult(Path.Combine(data, "results"), id, result);
            Console.WriteLine(result.ToJsonString());
            Console.WriteLine("savedResult=" + output);
            return status == "succeeded" || status == "cancelled" ? 0 : 1;
        }
        if (stop && status is "running" or "queued")
        {
            Console.WriteLine((await api.Send(HttpMethod.Post, "api/v1/jobs/" + id + "/cancel")).ToJsonString());
            stop = false;
        }
        int seconds = state["pollAfterSeconds"]?.GetValue<int>() ?? 3;
        await Task.Delay(TimeSpan.FromSeconds(seconds) + TimeSpan.FromMilliseconds(Random.Shared.Next(0, 250)));
    }
}
catch (ApiFailure error)
{
    Console.Error.WriteLine($"API error: HTTP {error.Status} {error.Code}");
    return 1;
}
catch (Exception error) when (error is ArgumentException or IOException or HttpRequestException or TaskCanceledException or System.Text.Json.JsonException)
{
    Console.Error.WriteLine("Request failed; use resume with the saved clientRequestId. " + error.GetType().Name);
    for (Exception? cause = error; cause is not null; cause = cause.InnerException)
        Console.Error.WriteLine(cause.GetType().Name + ": " + cause.Message.Replace(token, "[REDACTED]"));
    return 1;
}
