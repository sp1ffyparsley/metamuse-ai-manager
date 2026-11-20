using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using MuseMonitor.Client;

var passed=0;
void Check(bool condition,string name) { if(!condition) throw new Exception("FAIL "+name); Console.WriteLine("PASS "+name); passed++; }
Check(!PollingPolicy.FetchFrame(true,true,true),"minimized HUD requests no frames");
Check(!PollingPolicy.FetchFrame(false,false,true),"hidden HUD requests no frames");
Check(PollingPolicy.Status(false,false)>PollingPolicy.Status(true,false),"background status polling is slower");
Check(PollingPolicy.Frame(true)<PollingPolicy.Frame(false),"human viewer retains faster refresh");
void Refuses(Action action,string name) { try { action(); } catch(ArgumentException) { Check(true,name); return; } catch(ProtocolException) { Check(true,name); return; } throw new Exception("FAIL "+name); }
async Task RefusesAsync(Func<Task> action,string name) { try { await action(); } catch(ProtocolException) { Check(true,name); return; } catch(OperationCanceledException) { Check(true,name); return; } throw new Exception("FAIL "+name); }
var now=DateTimeOffset.UtcNow;
var page=new PageState("page-1","Fixture","doc-1");
var workspace=new WorkspaceState("jobs","Jobs",2,"human","fixture-actor","test",null,null,null,now,null,0,0,null,[page]);
var status=new StatusEnvelope(1,"instance-1",1,now,"fixture",[workspace]);
var grant=new ControlGrant(new string('g',32),"jobs",2,now.AddMinutes(1),page.Id,page.Generation);
Refuses(()=>new ControllerClient("http://192.0.2.22:9000",new string('t',32)),"cleartext non-loopback refused");
Refuses(()=>new ControllerClient("https://user:password@example.com",new string('t',32)),"URL credentials refused");
Refuses(()=>new ControllerClient("https://example.com/?token=secret",new string('t',32)),"URL token query refused");
Refuses(()=>new ControllerClient("https://example.com","short"),"weak pairing token refused");
var reducer=new StatusReducer();
Check(reducer.Accept(status,now),"first telemetry accepted");
Check(!reducer.Accept(status,now),"replayed sequence refused");
Check(!reducer.Accept(status with {Sequence=2,Instance="other"},now),"unexpected instance refused");
Check(reducer.RequiresReconnect(status with {Sequence=2,Instance="other"},now),"authenticated controller restart requires fresh connection");
Check(!reducer.RequiresReconnect(status,now),"replayed sequence cannot force a new baseline");
Refuses(()=>reducer.RequiresReconnect(status with {Protocol=2,Instance="other"},now),"invalid replacement protocol is not automatically accepted");
Check(!reducer.Accept(status with {Sequence=2,Workspaces=[workspace with {Generation=1}]},now),"workspace generation rollback refused");
Refuses(()=>new StatusReducer().Accept(status with {Protocol=2},now),"protocol mismatch refused");
Refuses(()=>new StatusReducer().Accept(status with {Workspaces=[workspace,workspace]},now),"duplicate workspace refused");
Check(StatusReducer.CanInput(grant,workspace,page,now),"current exact human grant permits input");
Check(!StatusReducer.CanInput(null,workspace,page,now),"Watch has no input grant");
Check(!StatusReducer.CanInput(grant with {Workspace="research"},workspace,page,now),"cross-workspace grant refused");
Check(!StatusReducer.CanInput(grant with {Generation=1},workspace,page,now),"pre-takeover grant refused");
Check(!StatusReducer.CanInput(grant with {ExpiresAt=now.AddSeconds(-1)},workspace,page,now),"expired grant refused");
Check(!StatusReducer.CanInput(grant,workspace,page with {Generation="doc-2"},now),"stale document grant refused");
Check(!StatusReducer.CanInput(grant,workspace with {ObservedAt=now.AddMinutes(-1)},page,now),"stale telemetry locks input");
Check(!StatusReducer.CanInput(grant,workspace with {State="paused"},page,now),"pause is not human ownership");

// A real loopback HTTP boundary exercises auth, denial, lost-response behavior,
// redirect refusal, and bounded cancellation without a production credential.
using var portProbe=new TcpListener(IPAddress.Loopback,0); portProbe.Start(); var port=((IPEndPoint)portProbe.LocalEndpoint).Port; portProbe.Stop();
using var listener=new HttpListener(); listener.Prefixes.Add($"http://127.0.0.1:{port}/"); listener.Start();
var token=new string('t',32); var requests=0; var mutations=0; var mode="status";var badPostFraming=false;
using var serverStop=new CancellationTokenSource();
var server=Task.Run(async()=> {
    while(!serverStop.IsCancellationRequested) {
        HttpListenerContext ctx; try {ctx=await listener.GetContextAsync().WaitAsync(serverStop.Token);} catch {break;}
        Interlocked.Increment(ref requests);
        try {
            if(ctx.Request.Headers["Authorization"]!="Bearer "+token || ctx.Request.Headers["X-Muse-Protocol"]!="1") {ctx.Response.StatusCode=401;ctx.Response.Close();continue;}
            if(ctx.Request.HttpMethod=="POST") {
                Interlocked.Increment(ref mutations);
                if(ctx.Request.ContentLength64<=0 || ctx.Request.ContentLength64>65536 || ctx.Request.Headers["Transfer-Encoding"]!=null) {
                    badPostFraming=true;ctx.Response.StatusCode=409;ctx.Response.Close();continue;
                }
                using var reader=new StreamReader(ctx.Request.InputStream);await reader.ReadToEndAsync();
            }
            if(mode=="deny") {ctx.Response.StatusCode=403;ctx.Response.Close();continue;}
            if(mode=="redirect") {ctx.Response.StatusCode=302;ctx.Response.RedirectLocation="http://127.0.0.1:1/secret";ctx.Response.Close();continue;}
            if(mode=="wait") {await Task.Delay(500,serverStop.Token);ctx.Response.Close();continue;}
            if(mode=="lost") {ctx.Response.Abort();continue;}
            if(mode=="huge-png") {
                var png=new byte[24];new byte[]{137,80,78,71,13,10,26,10}.CopyTo(png,0);
                System.Buffers.Binary.BinaryPrimitives.WriteUInt32BigEndian(png.AsSpan(16,4),65535);
                System.Buffers.Binary.BinaryPrimitives.WriteUInt32BigEndian(png.AsSpan(20,4),65535);
                ctx.Response.ContentType="image/png";await ctx.Response.OutputStream.WriteAsync(png);ctx.Response.Close();continue;
            }
            var bytes=Encoding.UTF8.GetBytes(mode=="control"?JsonSerializer.Serialize(new ControlReply(status,grant)):JsonSerializer.Serialize(status)); ctx.Response.ContentType="application/json";
            await ctx.Response.OutputStream.WriteAsync(bytes); ctx.Response.Close();
        } catch {try{ctx.Response.Abort();}catch{}}
    }
});
using var client=new ControllerClient($"http://127.0.0.1:{port}",token);
Check((await client.Status(CancellationToken.None)).Revision=="fixture","real HTTP status and authentication");
var before=requests;
await RefusesAsync(()=>client.Input(workspace,page,null,new("click",0.5,0.5),CancellationToken.None),"Watch mutation refused before transport");
Check(requests==before,"Watch generated zero HTTP input requests");
mode="control";
Check((await client.Control("takeover",workspace,page,null,CancellationToken.None)).Grant?.Page==page.Id,"control succeeds through bounded real HTTP request reader");
Check(!badPostFraming,"mutation carries bounded Content-Length instead of chunked transfer");
mode="deny";
await RefusesAsync(()=>client.Input(workspace,page,grant,new("click",0.5,0.5),CancellationToken.None),"server-side denied human input surfaced");
mode="huge-png";
await RefusesAsync(()=>client.Frame(workspace,page,CancellationToken.None),"oversized PNG dimensions refused before WPF decode");
mode="redirect";
await RefusesAsync(()=>client.Status(CancellationToken.None),"redirect refused without credential forwarding");
mode="wait";
using(var timeout=new CancellationTokenSource(80)) await RefusesAsync(()=>client.Status(timeout.Token),"caller cancellation bounds transport");
await Task.Delay(550);
mode="lost"; var startMutations=mutations;
try {await client.Control("pause",workspace,page,null,CancellationToken.None);} catch(Exception e) when(e is HttpRequestException or JsonException or ProtocolException) { }
Check(mutations==startMutations+1,"lost mutation response was not replayed");
mode="status";
Check((await client.Status(CancellationToken.None)).Instance==status.Instance,"read-only status recovers after lost response");
serverStop.Cancel(); listener.Stop(); await server;
Console.WriteLine($"{passed} checks passed; no VM, Google, portal or application effects exercised.");
