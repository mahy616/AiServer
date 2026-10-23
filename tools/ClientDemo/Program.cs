using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;

Console.OutputEncoding = Encoding.UTF8;
if (args.Length < 1)
{
    Console.Error.WriteLine("submit <request.json> [--cancel-on-running] | resume <clientRequestId> | cancel <jobId> | ready");
    return 2;
}
var token = Environment.GetEnvironmentVariable("AISERVER_TOKEN");
if (string.IsNullOrWhiteSpace(token))
{
    Console.Error.WriteLine("Set AISERVER_TOKEN in this process environment.");
    return 2;
}
var url = new Uri((Environment.GetEnvironmentVariable("AISERVER_BASE_URL") ?? "http://127.0.0.1:18080").TrimEnd('/') + "/");
if (url.Scheme != "https" && !(url.Scheme == "http" && url.IsLoopback))
{
    Console.Error.WriteLine("Use HTTPS except for local loopback.");
    return 2;
}
using var http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false, UseProxy = false })
{ BaseAddress = url, Timeout = Timeout.InfiniteTimeSpan };
http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
var data = Path.GetFullPath(Environment.GetEnvironmentVariable("AISERVER_DEMO_DATA") ?? ".local/client");
Directory.CreateDirectory(Path.Combine(data, "requests"));
Directory.CreateDirectory(Path.Combine(data, "results"));

async Task<JsonObject> Send(HttpMethod method, string path, string? body = null, bool submit = false)
{
    using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(submit ? 120 : 15));
    using var request = new HttpRequestMessage(method, path);
    if (body is not null) request.Content = new StringContent(body, Encoding.UTF8, "application/json");
    using var response = await http.SendAsync(request, deadline.Token);
    var text = await response.Content.ReadAsStringAsync(deadline.Token);
    var json = JsonNode.Parse(text)?.AsObject() ?? throw new IOException("Invalid API response");
    if (!response.IsSuccessStatusCode)
        throw new ApiFailure((int)response.StatusCode,
            json["error"]?["code"]?.GetValue<string>() ?? "HTTP_ERROR",
            response.Headers.RetryAfter?.Delta);
    return json;
}
async Task<JsonObject> Query(string path)
{
    for (int attempt = 0; ; attempt++)
    {
        try { return await Send(HttpMethod.Get, path); }
        catch (Exception error) when (attempt < 4 && (
            error is HttpRequestException or TaskCanceledException ||
            error is ApiFailure f && (f.Status == 429 || f.Status >= 500)))
        {
            var retry = error is ApiFailure api && api.RetryAfter is not null
                ? api.RetryAfter.Value.TotalSeconds : new[] { 3, 6, 12, 30 }[attempt];
            await Task.Delay(TimeSpan.FromSeconds(retry));
        }
    }
}
async Task<JsonObject> Recover(string requestId, string? serialized)
{
    try { return await Query("api/v1/jobs/by-request/" + Uri.EscapeDataString(requestId)); }
    catch (ApiFailure error) when (error.Status == 404 && serialized is not null)
    { return await Send(HttpMethod.Post, "api/v1/jobs", serialized, true); }
}
try
{
    if (args[0] == "ready")
    {
        Console.WriteLine((await Query("api/v1/ready")).ToJsonString());
        return 0;
    }
    if (args.Length < 2) throw new ArgumentException("Missing request file or UUID");
    if (args[0] == "cancel")
    {
        string jobId = Guid.Parse(args[1]).ToString();
        var result = await Send(HttpMethod.Post, "api/v1/jobs/" + jobId + "/cancel");
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
        try { accepted = await Send(HttpMethod.Post, "api/v1/jobs", serialized, true); }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException ||
                                      error is ApiFailure f && f.Status >= 500)
        { accepted = await Recover(requestId, serialized); }
    }
    else if (args[0] == "resume")
    {
        requestId = Guid.Parse(args[1]).ToString();
        var saved = Path.Combine(data, "requests", requestId + ".json");
        accepted = await Recover(requestId, File.Exists(saved) ? File.ReadAllText(saved, Encoding.UTF8) : null);
    }
    else throw new ArgumentException("Unknown command");
    string id = accepted["jobId"]!.GetValue<string>();
    Console.WriteLine("jobId=" + id);
    try { Console.WriteLine("inferenceProvider=" + (await Send(HttpMethod.Get, "api/v1/ready"))["inferenceProvider"]); }
    catch (ApiFailure error) { Console.WriteLine("readyUnavailable=" + error.Code); }
    string? previous = null;
    bool stop = args.Contains("--cancel-on-running");
    while (true)
    {
        var state = await Query("api/v1/jobs/" + id);
        string status = state["status"]!.GetValue<string>();
        if (status != previous)
        {
            Console.WriteLine("status=" + status);
            previous = status;
        }
        if (status is "succeeded" or "failed" or "cancelled")
        {
            var result = await Query("api/v1/jobs/" + id + "/result");
            var output = Path.Combine(data, "results", id + ".json");
            File.WriteAllText(output, result.ToJsonString(), new UTF8Encoding(false));
            Console.WriteLine(result.ToJsonString());
            Console.WriteLine("savedResult=" + output);
            return status == "succeeded" || status == "cancelled" ? 0 : 1;
        }
        if (stop && status == "running")
        {
            Console.WriteLine((await Send(HttpMethod.Post, "api/v1/jobs/" + id + "/cancel")).ToJsonString());
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
    return 1;
}

sealed class ApiFailure(int status, string code, TimeSpan? retryAfter) : Exception(code)
{
    public int Status { get; } = status;
    public string Code { get; } = code;
    public TimeSpan? RetryAfter { get; } = retryAfter;
}
