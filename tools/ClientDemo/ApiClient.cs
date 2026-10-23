using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;

namespace AiServer.Client;

// HTTP-only component: it has no dependency on console, files, UI or host SDK.
public sealed class ApiClient : IDisposable
{
    private readonly HttpClient http;

    public ApiClient(string baseUrl, string token)
    {
        var url = new Uri(baseUrl.TrimEnd('/') + "/");
        if (url.Scheme != "https" && !(url.Scheme == "http" && url.IsLoopback))
            throw new ArgumentException("Use HTTPS except for local loopback.");
        http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false, UseProxy = false })
        { BaseAddress = url, Timeout = Timeout.InfiniteTimeSpan };
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
    }

    public async Task<JsonObject> Send(HttpMethod method, string path, string? body = null, bool submit = false)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(submit ? 120 : 15));
        using var request = new HttpRequestMessage(method, path);
        if (body is not null) request.Content = new StringContent(body, Encoding.UTF8, "application/json");
        using var response = await http.SendAsync(request, deadline.Token);
        var text = await response.Content.ReadAsStringAsync(deadline.Token);
        JsonObject? json = null;
        try { json = JsonNode.Parse(text)?.AsObject(); }
        catch (System.Text.Json.JsonException) { }
        catch (InvalidOperationException) { }
        if (!response.IsSuccessStatusCode)
            throw new ApiFailure((int)response.StatusCode,
                json?["error"]?["code"]?.GetValue<string>() ?? "HTTP_ERROR",
                response.Headers.RetryAfter?.Delta);
        return json ?? throw new IOException("Invalid API response");
    }

    public async Task<JsonObject> Query(string path)
    {
        for (int attempt = 0; ; attempt++)
        {
            try { return await Send(HttpMethod.Get, path); }
            catch (Exception error) when (attempt < 4 && (
                error is HttpRequestException { InnerException: not System.Security.Authentication.AuthenticationException } or TaskCanceledException ||
                error is ApiFailure f && (f.Status == 429 || f.Status >= 500)))
            {
                var seconds = error is ApiFailure api && api.RetryAfter is not null
                    ? api.RetryAfter.Value.TotalSeconds : new[] { 3, 6, 12, 30 }[attempt];
                await Task.Delay(TimeSpan.FromSeconds(Math.Clamp(seconds, 0, 60)));
            }
        }
    }

    public async Task<JsonObject> Recover(string requestId, string? serialized)
    {
        try { return await Query("api/v1/jobs/by-request/" + Uri.EscapeDataString(requestId)); }
        catch (ApiFailure error) when (error.Status == 404 && serialized is not null)
        { return await Send(HttpMethod.Post, "api/v1/jobs", serialized, true); }
    }

    public void Dispose() => http.Dispose();
}

public sealed class ApiFailure(int status, string code, TimeSpan? retryAfter) : Exception(code)
{
    public int Status { get; } = status;
    public string Code { get; } = code;
    public TimeSpan? RetryAfter { get; } = retryAfter;
}
