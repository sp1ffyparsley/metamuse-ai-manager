using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Buffers.Binary;

namespace MuseMonitor.Client;

public sealed record PageState(string Id, string Title, string Generation);
public sealed record WorkspaceState(string Id, string Name, long Generation, string State,
    string? Actor, string? Role, string? Task, string? Phase, string? Blocker,
    DateTimeOffset ObservedAt, DateTimeOffset? ActionStartedAt, int Queue, int PendingSync,
    string? RecordUrl, PageState[] Pages);
public sealed record StatusEnvelope(int Protocol, string Instance, long Sequence,
    DateTimeOffset ObservedAt, string Revision, WorkspaceState[] Workspaces,
    Dictionary<string,JsonElement>? Connections = null);
public sealed record ControlGrant(string Token, string Workspace, long Generation,
    DateTimeOffset ExpiresAt, string Page, string PageGeneration);
public sealed record ControlReply(StatusEnvelope Status, ControlGrant? Grant);
public sealed record InputAction(string Kind, double? X = null, double? Y = null,
    string? Text = null, string? Key = null);
public sealed record ConnectionState(string Id, string Label, string Account, string Scope,
    string Status, string Detail, string? ConsentUrl, DateTimeOffset? VerifiedAt, bool Stale);
public sealed record ConnectionsEnvelope(int Schema, DateTimeOffset ObservedAt, ConnectionState[] Entries);

public sealed class ProtocolException(string message) : Exception(message);

// Each new connection must use a new reducer. Responses from a retired connection
// may never supply the baseline for a replacement controller instance.
public sealed class StatusReducer
{
    public StatusEnvelope? Current { get; private set; }
    public bool RequiresReconnect(StatusEnvelope next,DateTimeOffset now)
    {
        if(Current is null || next.Instance==Current.Instance)return false;
        // Validate the new authenticated response, but never accept a replacement
        // baseline into the old connection or reuse its human grant.
        new StatusReducer().Accept(next,now);
        return true;
    }
    public bool Accept(StatusEnvelope next, DateTimeOffset now)
    {
        if (next.Protocol != 1) throw new ProtocolException("Controller protocol is incompatible; expected 1.");
        if (string.IsNullOrWhiteSpace(next.Instance) || next.Sequence < 0 ||
            string.IsNullOrWhiteSpace(next.Revision) || next.Workspaces is null || next.Workspaces.Length > 20 ||
            next.ObservedAt > now.AddSeconds(30))
            throw new ProtocolException("Controller returned invalid telemetry or a future timestamp.");
        if (Current is { } current && (next.Instance != current.Instance || next.Sequence <= current.Sequence)) return false;
        if (next.Workspaces.Select(w => w.Id).Distinct().Count() != next.Workspaces.Length)
            throw new ProtocolException("Controller returned duplicate workspace IDs.");
        foreach (var workspace in next.Workspaces)
        {
            if (!ValidId(workspace.Id) || workspace.Generation < 0 || workspace.Pages is null ||
                workspace.State is not ("working" or "waiting" or "paused" or "human" or "offline" or "needs-you") ||
                workspace.Pages.Length > 500 || workspace.Queue < 0 || workspace.PendingSync < 0 ||
                workspace.ObservedAt > next.ObservedAt.AddSeconds(1) ||
                workspace.Pages.Any(p => !ValidId(p.Id) || string.IsNullOrWhiteSpace(p.Generation)))
                throw new ProtocolException("Controller returned an invalid workspace or page identity.");
            var previous = Current?.Workspaces.FirstOrDefault(w => w.Id == workspace.Id);
            if (previous != null && workspace.Generation < previous.Generation) return false;
        }
        Current = next;
        return true;
    }
    public static bool ValidId(string value) => !string.IsNullOrEmpty(value) && value.Length <= 128 &&
        value.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_');
    public static bool Fresh(DateTimeOffset observed, DateTimeOffset now) =>
        observed <= now.AddSeconds(30) && now - observed < TimeSpan.FromSeconds(15);
    public static bool CanInput(ControlGrant? grant, WorkspaceState? workspace, PageState? page, DateTimeOffset now) =>
        grant != null && workspace != null && page != null && Fresh(workspace.ObservedAt, now) &&
        workspace.State == "human" && grant.Workspace == workspace.Id && grant.Generation == workspace.Generation &&
        grant.Page == page.Id && grant.PageGeneration == page.Generation && grant.ExpiresAt > now;
}

public sealed class ControllerClient : IDisposable
{
    private readonly HttpClient http;
    public Uri Endpoint { get; }
    public ControllerClient(string endpoint, string token, HttpMessageHandler? handler = null, TimeSpan? timeout = null)
    {
        if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) || !string.IsNullOrEmpty(uri.UserInfo) ||
            uri.Query.Length > 0 || uri.Fragment.Length > 0 ||
            !(uri.Scheme == "https" || uri.Scheme == "http" && uri.IsLoopback))
            throw new ArgumentException("Use a private HTTPS controller URL, or HTTP on loopback through an authenticated tunnel.");
        if (token.Length < 32 || token.Any(char.IsWhiteSpace)) throw new ArgumentException("A controller pairing token of at least 32 characters is required.");
        Endpoint = new Uri(uri.AbsoluteUri.TrimEnd('/') + "/");
        http = new HttpClient(handler ?? new HttpClientHandler { AllowAutoRedirect = false, UseCookies = false })
        { BaseAddress = Endpoint, Timeout = timeout ?? TimeSpan.FromSeconds(8), MaxResponseContentBufferSize = 2 * 1024 * 1024 };
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        http.DefaultRequestHeaders.Add("X-Muse-Protocol", "1");
    }
    public Task<StatusEnvelope> Status(CancellationToken ct) => Read<StatusEnvelope>("v1/status", ct);
    public Task<JsonElement> Health(CancellationToken ct) => Read<JsonElement>("v1/health", ct);
    public Task<ConnectionsEnvelope> Connections(CancellationToken ct) =>
        Read<ConnectionsEnvelope>("v1/connections", ct);
    public Task<JsonElement> Manager(CancellationToken ct) => Read<JsonElement>("v1/manager", ct);
    public Task<JsonElement> ManagerAction(object body, CancellationToken ct) => Post<JsonElement>("v1/manager", body, ct);
    public Task<ConnectionsEnvelope> VerifyConnection(string id, CancellationToken ct)
    {
        if (id is not ("google" or "gmail")) throw new ArgumentException("Choose a Google runtime connection.");
        return Post<ConnectionsEnvelope>("v1/connections/verify", new { id }, ct);
    }
    public async Task<ControlReply> Control(string action, WorkspaceState workspace, PageState? page, ControlGrant? grant, CancellationToken ct)
    {
        if (action is not ("pause" or "takeover" or "resume")) throw new ArgumentException("Unsupported control action.");
        if (action == "takeover" && page is null) throw new ProtocolException("Select the exact page before takeover.");
        if (action == "resume" && grant is null) throw new ProtocolException("A current human grant is required to resume.");
        return await Post<ControlReply>($"v1/workspaces/{Id(workspace.Id)}/{action}",
            new { requestId = Guid.NewGuid(), generation = workspace.Generation, page = page?.Id,
                pageGeneration = page?.Generation, grant = grant?.Token }, ct);
    }
    public async Task Input(WorkspaceState workspace, PageState page, ControlGrant? grant, InputAction action, CancellationToken ct)
    {
        if (!StatusReducer.CanInput(grant, workspace, page, DateTimeOffset.UtcNow))
            throw new ProtocolException("Input is locked. Take over this exact page with a current human grant first.");
        if (action.Kind is not ("click" or "text" or "key") || action.Text?.Length > 4096 ||
            action.Kind == "click" && (action.X is null || action.Y is null || !double.IsFinite(action.X.Value) ||
                !double.IsFinite(action.Y.Value) || action.X < 0 || action.X > 1 || action.Y < 0 || action.Y > 1))
            throw new ArgumentException("Invalid bounded viewer input.");
        await Post<JsonElement>($"v1/workspaces/{Id(workspace.Id)}/input", new {
            requestId = Guid.NewGuid(), generation = workspace.Generation, page = page.Id,
            pageGeneration = page.Generation, grant = grant!.Token, action }, ct);
    }
    public async Task<byte[]> Frame(WorkspaceState workspace, PageState page, CancellationToken ct)
    {
        using var response = await http.GetAsync($"v1/workspaces/{Id(workspace.Id)}/pages/{Id(page.Id)}/frame?generation={workspace.Generation}&document={Uri.EscapeDataString(page.Generation)}", ct);
        Check(response);
        if (response.Content.Headers.ContentType?.MediaType != "image/png") throw new ProtocolException("Viewer returned a non-PNG frame.");
        var bytes = await response.Content.ReadAsByteArrayAsync(ct);
        if (bytes.Length < 24 || !bytes.AsSpan(0,8).SequenceEqual(new byte[]{137,80,78,71,13,10,26,10})) throw new ProtocolException("Viewer returned an invalid PNG frame.");
        var width=BinaryPrimitives.ReadUInt32BigEndian(bytes.AsSpan(16,4));
        var height=BinaryPrimitives.ReadUInt32BigEndian(bytes.AsSpan(20,4));
        if(width==0 || height==0 || width>4096 || height>4096 || (ulong)width*height>8_388_608)
            throw new ProtocolException("Viewer frame dimensions exceed the safe bound.");
        return bytes;
    }
    private async Task<T> Read<T>(string path, CancellationToken ct)
    {
        using var response = await http.GetAsync(path, ct); Check(response);
        return await response.Content.ReadFromJsonAsync<T>(cancellationToken: ct) ?? throw new ProtocolException("Controller returned an empty response.");
    }
    private async Task<T> Post<T>(string path, object body, CancellationToken ct)
    {
        // Never automatically replay a mutation after timeout/disconnect.
        // JsonContent streams chunked requests. The bounded VM protocol needs a
        // known byte length before it accepts any mutation body.
        var bytes=JsonSerializer.SerializeToUtf8Bytes(body,new JsonSerializerOptions(JsonSerializerDefaults.Web));
        if(bytes.Length>65536) throw new ProtocolException("Control request exceeds the VM protocol bound.");
        using var content=new ByteArrayContent(bytes);
        content.Headers.ContentType=new MediaTypeHeaderValue("application/json");
        using var response = await http.PostAsync(path, content, ct); Check(response);
        return await response.Content.ReadFromJsonAsync<T>(cancellationToken: ct) ?? throw new ProtocolException("Controller returned an empty response; reconcile status before another action.");
    }
    private static void Check(HttpResponseMessage response)
    {
        if (response.IsSuccessStatusCode) return;
        throw new ProtocolException(response.StatusCode switch {
            HttpStatusCode.Unauthorized => "Pairing was rejected. Check the controller token in Settings.",
            HttpStatusCode.Forbidden => "The controller denied this action for this authenticated client.",
            HttpStatusCode.Conflict => "Ownership or page changed. Reconnect and inspect the current checkpoint.",
            _ => $"Controller request failed (HTTP {(int)response.StatusCode}); inspect controller diagnostics."
        });
    }
    private static string Id(string id) => StatusReducer.ValidId(id) ? id : throw new ProtocolException("Invalid identity in controller response.");
    public void Dispose() => http.Dispose();
}
