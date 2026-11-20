using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using MuseMonitor.Client;
using Button = System.Windows.Controls.Button;
using TextBox = System.Windows.Controls.TextBox;
using Application = System.Windows.Application;
using MessageBox = System.Windows.MessageBox;
using Orientation = System.Windows.Controls.Orientation;
using ComboBox = System.Windows.Controls.ComboBox;
using Image = System.Windows.Controls.Image;
using Brushes = System.Windows.Media.Brushes;
using CheckBox = System.Windows.Controls.CheckBox;

namespace MuseMonitor;

public static class Program
{
    [STAThread] public static int Main(string[] args)
    {
        var testHome=Environment.GetEnvironmentVariable("MUSE_MONITOR_TEST_HOME");
        if(testHome is not null && !System.Text.RegularExpressions.Regex.IsMatch(testHome,"^MuseMonitor-Test-[A-Za-z0-9_-]{1,100}$"))throw new ArgumentException("Invalid isolated test directory");
        if(args is ["--check-controller", var output]) return CheckController(output, false).GetAwaiter().GetResult();
        if(args is ["--check-connections", var connections]) return CheckController(connections, true).GetAwaiter().GetResult();
        var instance=testHome ?? "v1";
        using var mutex = new Mutex(true, "Local\\MuseMonitor.Windows."+instance, out var owner);
        using var reopen = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\MuseMonitor.Reopen."+instance);
        if (!owner) { reopen.Set(); return 0; }
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        app.DispatcherUnhandledException += (_, e) => { Log.Write("unhandled", e.Exception.GetType().Name); e.Handled = true; MessageBox.Show("Muse Monitor encountered an error. Check its local diagnostic log; VM work is unaffected."); };
        Log.Write("startup", $"version={typeof(Program).Assembly.GetCustomAttributes(typeof(System.Reflection.AssemblyInformationalVersionAttribute),false).OfType<System.Reflection.AssemblyInformationalVersionAttribute>().FirstOrDefault()?.InformationalVersion}; data={Store.Root}");
        var window = new Hud(args);
        var registration=ThreadPool.RegisterWaitForSingleObject(reopen,(_,_)=>app.Dispatcher.BeginInvoke(window.Reopen),null,Timeout.Infinite,false);
        app.Run(window);
        registration.Unregister(null);
        Log.Write("completion", "HUD exited; no VM lifecycle command sent");
        return 0;
    }

    private static async Task<int> CheckController(string output, bool connections)
    {
        try {
            using var client = new ControllerClient(Store.Load().Endpoint, CredentialStore.Read() ?? "");
            var status = await client.Status(CancellationToken.None);
            new StatusReducer().Accept(status, DateTimeOffset.UtcNow);
            var health = await client.Health(CancellationToken.None);
            var safe = new { status.Revision, status.ObservedAt, Workspaces = status.Workspaces.Select(w =>
                new { w.Id, w.State, w.ObservedAt, w.Queue, w.PendingSync, Pages = w.Pages.Length }),
                Configured = status.Connections, Uncertain = health.GetProperty("uncertain").GetArrayLength(),
                PendingSync = health.GetProperty("pendingSync").GetInt32(),
                Connections = connections ? await client.Connections(CancellationToken.None) : null };
            using var file = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            await JsonSerializer.SerializeAsync(file, safe, new JsonSerializerOptions { WriteIndented = true });
            return 0;
        } catch (Exception error) {
            Log.Write("connection-probe-error", error.GetType().Name);
            return 1;
        }
    }
}

public sealed record Preferences(string Endpoint = "", double Left = 80, double Top = 80, bool Topmost = false, bool Expanded = false);
public static class Store
{
    public static string Root => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        Environment.GetEnvironmentVariable("MUSE_MONITOR_TEST_HOME") is { Length: > 0 } test ? test : "MuseMonitor");
    public static Preferences Load()
    {
        Directory.CreateDirectory(Root);
        try { var p = JsonSerializer.Deserialize<Preferences>(File.ReadAllText(Path.Combine(Root, "settings.json"))) ?? new(); Log.Write("config", "settings loaded"); return p; }
        catch (FileNotFoundException) { Log.Write("config", "first launch"); return new(); }
        catch (Exception e) { Log.Write("config-error", e.GetType().Name); return new(); }
    }
    public static void Save(Preferences preferences)
    {
        var path = Path.Combine(Root, "settings.json");
        File.WriteAllText(path + ".tmp", JsonSerializer.Serialize(preferences)); File.Move(path + ".tmp", path, true);
    }
}
public static class Log
{
    private static readonly object Gate = new();
    private static readonly Dictionary<string,long> Last=new();
    public static void Limited(string action,string detail,long intervalMs=60000)
    {
        lock(Gate) {var now=Environment.TickCount64;if(Last.TryGetValue(action,out var prior)&&now-prior<intervalMs)return;Last[action]=now;}
        Write(action,detail);
    }
    public static void Write(string action, string detail)
    {
        lock (Gate) try {
            Directory.CreateDirectory(Store.Root);
            var path = Path.Combine(Store.Root, "diagnostics.log");
            if (File.Exists(path) && new FileInfo(path).Length > 256 * 1024) File.Move(path, path + ".previous", true);
            // Call sites only supply event names, durations, paths and exception TYPES.
            // Never log credentials, request bodies, page URLs, text, or server content.
            File.AppendAllText(path, $"{DateTimeOffset.UtcNow:O} {action} {detail}\n");
        } catch { /* A diagnostic failure must not repeat a remote action. */ }
    }
}

public sealed class Hud : Window
{
    private readonly TextBlock connection = Text("Offline · no controller configured", 13);
    private readonly TextBlock detail = Text("Open Settings to connect to the VM.", 12);
    private readonly StackPanel rows = new();
    private readonly ComboBox workspaces = new() { MinWidth = 150, DisplayMemberPath = "Name", Margin = new Thickness(0, 8, 0, 6) };
    private readonly ComboBox pages = new() { DisplayMemberPath = "Title", Margin = new Thickness(0, 0, 0, 6) };
    private readonly Image frame = new() { Stretch = Stretch.Uniform, Focusable = true, MinHeight = 120 };
    private readonly TextBlock viewerState = Text("Watch is read only. No live view connected.", 12);
    private readonly StackPanel viewer = new() { Visibility = Visibility.Collapsed };
    private readonly Button pause, takeover, resume, watch, record;
    private readonly System.Windows.Forms.NotifyIcon tray;
    private readonly DispatcherTimer poll = new() { Interval = TimeSpan.FromSeconds(2) };
    private readonly DispatcherTimer stream = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private readonly string[] args;
    private Preferences preferences;
    private ControllerClient? client;
    private StatusReducer reducer = new();
    private ControlGrant? grant;
    private CancellationTokenSource session = new();
    private bool polling, streaming, mutation, exiting, viewing, binding;
    private DateTimeOffset? lastContact;
    private long connectionGeneration;
    private readonly Dictionary<string,(string? Actor,long Generation)> loggedOwners=new();
    private ManagerWindow? manager;
    private WorkspaceState? Workspace => workspaces.SelectedItem as WorkspaceState;
    private PageState? Page => pages.SelectedItem as PageState;

    public Hud(string[] args)
    {
        this.args = args; preferences = Store.Load();
        Title = "Muse Monitor"; Width = preferences.Expanded ? 780 : 420; Height = preferences.Expanded ? 650 : 470;
        MinWidth = 380; MinHeight = 380; Topmost = preferences.Topmost; ShowActivated = false;
        Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(245, 246, 248)); Foreground = Brushes.Black;
        FontFamily = new System.Windows.Media.FontFamily("Segoe UI"); FontSize = 13;
        var root = new DockPanel { Margin = new Thickness(18) }; Content = root;
        var heading = new StackPanel(); DockPanel.SetDock(heading, Dock.Top); root.Children.Add(heading);
        var top = new DockPanel(); heading.Children.Add(top);
        var title = Text("Muse Monitor", 22); title.FontWeight = FontWeights.SemiBold; top.Children.Add(title);
        var tools = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = System.Windows.HorizontalAlignment.Right };
        top.Children.Add(tools);
        tools.Children.Add(Action("Settings", Settings));
        var pinned = new CheckBox { Content = "On top", IsChecked = Topmost, Margin = new Thickness(8, 8, 0, 0) };
        pinned.Click += (_, _) => { Topmost = pinned.IsChecked == true; Save(); }; tools.Children.Add(pinned);
        connection.Margin = new Thickness(0, 12, 0, 4); heading.Children.Add(connection); heading.Children.Add(detail);
        heading.Children.Add(new ScrollViewer { Content=rows, MaxHeight=88, VerticalScrollBarVisibility=ScrollBarVisibility.Auto });
        heading.Children.Add(Action("Compact / expanded", () => {
            rows.Visibility=rows.Visibility==Visibility.Visible?Visibility.Collapsed:Visibility.Visible;
            if(!viewing) { Height=rows.Visibility==Visibility.Visible?470:370; Width=420; }
            Save();
        }));
        heading.Children.Add(workspaces); heading.Children.Add(pages);
        var actions = new WrapPanel(); heading.Children.Add(actions);
        watch = Action("Watch live", () => { viewing = !viewing; viewer.Visibility = viewing ? Visibility.Visible : Visibility.Collapsed;
            watch!.Content = viewing ? "Close view" : "Watch live"; if(viewing) { Width = Math.Max(Width,780); Height = Math.Max(Height,650); stream.Start(); } else { stream.Stop(); frame.Source = null; } Save(); });
        pause = Action("Pause", () => _ = Control("pause"));
        takeover = Action("Take over", () => _ = Control("takeover"));
        resume = Action("Resume", () => _ = Control("resume"));
        record = Action("Open record", OpenRecord);
        foreach (var b in new[] { watch, pause, takeover, resume, record }) actions.Children.Add(b);
        var managerActions = new WrapPanel();
        managerActions.Children.Add(Action("Reconnect", Connect));
        managerActions.Children.Add(Action("Setup & Connections", () => OpenManager()));
        managerActions.Children.Add(Action("Work Board", () => OpenManager("Work Board")));
        heading.Children.Add(managerActions);
        viewer.Children.Add(viewerState); viewer.Children.Add(frame);
        var input = new TextBox { Margin = new Thickness(0, 8, 0, 4), ToolTip = "Text is sent only with a valid human takeover grant." };
        AutomationProperties.SetName(input, "Text to enter into the controlled page"); viewer.Children.Add(input);
        viewer.Children.Add(Action("Enter text", () => { var value = input.Text; input.Clear(); _ = Send(new("text", Text:value)); }));
        var keys = new WrapPanel(); viewer.Children.Add(keys);
        foreach (var key in new[] { "Tab", "Enter", "Escape", "Backspace", "ArrowUp", "ArrowDown" }) keys.Children.Add(Action(key, () => _ = Send(new("key", Key:key))));
        root.Children.Add(new ScrollViewer { Content = viewer, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        frame.MouseLeftButtonUp += (_, e) => {
            if (frame.Source is not BitmapSource bitmap || !CanInput()) return;
            var scale = Math.Min(frame.ActualWidth / bitmap.PixelWidth, frame.ActualHeight / bitmap.PixelHeight);
            var w = bitmap.PixelWidth * scale; var h = bitmap.PixelHeight * scale; var p = e.GetPosition(frame);
            var x = (p.X - (frame.ActualWidth-w)/2)/w; var y = (p.Y - (frame.ActualHeight-h)/2)/h;
            if(x >= 0 && x <= 1 && y >= 0 && y <= 1) _ = Send(new("click",x,y));
        };
        workspaces.SelectionChanged += (_, _) => { if(binding) return; grant = null; frame.Source=null; BindPages(); RefreshButtons(); };
        pages.SelectionChanged += (_, _) => { if(binding) return; if(grant?.Page != Page?.Id || grant?.PageGeneration != Page?.Generation) grant = null; frame.Source = null; RefreshButtons(); };
        tray = new System.Windows.Forms.NotifyIcon { Icon = System.Drawing.SystemIcons.Application, Text = "Muse Monitor — Offline", Visible = true };
        tray.DoubleClick += (_, _) => Dispatcher.Invoke(Reopen);
        var menu = new System.Windows.Forms.ContextMenuStrip();
        menu.Items.Add("Open Muse Monitor", null, (_, _) => Dispatcher.Invoke(Reopen));
        menu.Items.Add("Reconnect", null, (_, _) => Dispatcher.Invoke(Connect));
        menu.Items.Add("Setup & Connections", null, (_, _) => Dispatcher.Invoke(() => OpenManager()));
        menu.Items.Add("Work Board", null, (_, _) => Dispatcher.Invoke(() => OpenManager("Work Board")));
        menu.Items.Add("Open diagnostic logs", null, (_, _) => Dispatcher.Invoke(OpenDiagnostics));
        menu.Items.Add("Exit HUD (leave VM running)", null, (_, _) => Dispatcher.Invoke(Exit)); tray.ContextMenuStrip = menu;
        Closing += (_, e) => { if (!exiting) { e.Cancel = true; Hide(); viewing = false; viewer.Visibility=Visibility.Collapsed; watch.Content="Watch live"; stream.Stop(); frame.Source = null; Save(); Log.Write("tray", "window hidden; remote work untouched"); } };
        SourceInitialized += (_, _) => RestorePosition();
        StateChanged += (_, _) => {TunePolling();if(WindowState!=WindowState.Minimized)_=Poll();};
        IsVisibleChanged += (_, _) => TunePolling();
        LocationChanged += (_, _) => { if(IsVisible) Save(); };
        poll.Tick += async (_, _) => await Poll(); stream.Tick += async (_, _) => await Frame();
        Loaded += async (_, _) => { Connect();if(args.Contains("--background"))Hide(); if(args.Contains("--smoke")) await Smoke(); };
        RefreshButtons();
    }
    private static TextBlock Text(string value, double size) => new() { Text=value, FontSize=size, TextWrapping=TextWrapping.Wrap };
    private static Button Action(string label, System.Action action)
    {
        var button = new Button { Content = label, Padding = new Thickness(9, 5, 9, 5), Margin = new Thickness(0, 3, 6, 3), MinHeight = 30 };
        button.Click += (_, _) => action(); return button;
    }
    private void RestorePosition()
    {
        var areas = System.Windows.Forms.Screen.AllScreens.Select(s=>s.WorkingArea).ToArray();
        var source = PresentationSource.FromVisual(this); var transform = source?.CompositionTarget?.TransformFromDevice ?? Matrix.Identity;
        var saved = new System.Windows.Point(preferences.Left, preferences.Top);
        bool visible = areas.Any(a => { var p=transform.Transform(new System.Windows.Point(a.Left,a.Top)); var q=transform.Transform(new System.Windows.Point(a.Right,a.Bottom)); return saved.X >= p.X && saved.Y >= p.Y && saved.X+100 < q.X && saved.Y+60 < q.Y; });
        Left = visible && double.IsFinite(saved.X) ? saved.X : SystemParameters.WorkArea.Left+30;
        Top = visible && double.IsFinite(saved.Y) ? saved.Y : SystemParameters.WorkArea.Top+30;
        // Keep the title bar reachable after monitor removal or a DPI change.
        SystemParameters.StaticPropertyChanged += (_, _) => { if(!IsVisible) return; Left=Math.Clamp(Left,SystemParameters.VirtualScreenLeft,SystemParameters.VirtualScreenLeft+SystemParameters.VirtualScreenWidth-120); Top=Math.Clamp(Top,SystemParameters.VirtualScreenTop,SystemParameters.VirtualScreenTop+SystemParameters.VirtualScreenHeight-80); };
    }
    private void Save()
    {
        try { preferences = preferences with { Left=Left, Top=Top, Topmost=Topmost, Expanded=Width>500 }; Store.Save(preferences); }
        catch(Exception e) { Log.Write("settings-save-error",e.GetType().Name); }
    }
    internal void Reopen() { Show();if(WindowState==WindowState.Minimized)WindowState=WindowState.Normal; Activate();TunePolling();_=Poll();Log.Write("tray", "explicit reopen"); }
    private void OpenDiagnostics() {Directory.CreateDirectory(Store.Root);Process.Start(new ProcessStartInfo("explorer.exe",Store.Root){UseShellExecute=true});}
    private void OpenManager(string tab = "Setup & Connections")
    {
        if (manager is null) {
            manager = new ManagerWindow(() => new ControllerClient(preferences.Endpoint, CredentialStore.Read() ?? "",
                timeout: TimeSpan.FromSeconds(40)));
            manager.Closed += (_, _) => manager = null;
            manager.Show();
        }
        manager.SelectTab(tab);
        if (manager.WindowState == WindowState.Minimized) manager.WindowState = WindowState.Normal;
        manager.Activate();
    }
    private void TunePolling()
    {
        poll.Interval=PollingPolicy.Status(IsVisible,WindowState==WindowState.Minimized);
        stream.Interval=PollingPolicy.Frame(CanInput());
        if(PollingPolicy.FetchFrame(IsVisible,WindowState==WindowState.Minimized,viewing))stream.Start();
        else {stream.Stop();frame.Source=null;}
    }
    private void ObserveOwners(StatusEnvelope status)
    {
        foreach(var w in status.Workspaces) {
            var actor=w.Actor is "main" or "alt" or "dad" or "sister"?w.Actor:w.State=="human"?"human":w.Actor is null?null:"other";
            if(loggedOwners.TryGetValue(w.Id,out var old) && old.Actor==actor && old.Generation==w.Generation)continue;
            if(old.Actor!=null)Log.Write("worker-end",$"workspace={w.Id} actor={old.Actor} generation={old.Generation}");
            if(actor!=null)Log.Write("worker-start",$"workspace={w.Id} actor={actor} generation={w.Generation}");
            loggedOwners[w.Id]=(actor,w.Generation);
        }
    }
    private void Exit() { exiting=true; Save(); poll.Stop(); stream.Stop(); session.Cancel(); client?.Dispose(); tray.Dispose(); Application.Current.Shutdown(); }
    private void Connect()
    {
        connectionGeneration++; session.Cancel(); session.Dispose(); session=new(); client?.Dispose(); client=null;
        reducer=new(); grant=null; lastContact=null; frame.Source=null;
        try {
            if(string.IsNullOrWhiteSpace(preferences.Endpoint)) throw new ProtocolException("No controller configured. Open Settings after the VM bridge has been deployed.");
            var token = CredentialStore.Read() ?? throw new ProtocolException("No paired controller token. Open Settings to pair this Windows client.");
            client=new ControllerClient(preferences.Endpoint,token);
            Log.Write("connect", "configured endpoint; protocol=1");
        } catch(Exception e) { SetOffline(e is ArgumentException or ProtocolException ? e.Message : "Could not load controller pairing. Check Settings."); }
        poll.Start(); _ = Poll();
    }
    private async Task Poll()
    {
        if(polling || client is null) { RefreshButtons(); return; }
        polling=true; var epoch=connectionGeneration; var active=client; var ct=session.Token; var clock=Stopwatch.StartNew();
        try {
            var status=await active.Status(ct);
            if(epoch != connectionGeneration) return;
            if(reducer.RequiresReconnect(status,DateTimeOffset.UtcNow)) {
                Log.Write("controller-restart","old grants cleared; establishing a new connection");
                Connect();return;
            }
            if(!reducer.Accept(status,DateTimeOffset.UtcNow)) throw new ProtocolException("Stale telemetry or controller restart detected. Reconnect to revalidate ownership.");
            lastContact=DateTimeOffset.UtcNow;ObserveOwners(status);Render();
            Log.Limited(clock.ElapsedMilliseconds>=1000?"status-slow":"status",$"ok duration_ms={clock.ElapsedMilliseconds}");
        } catch(OperationCanceledException) { if(epoch==connectionGeneration) SetOffline("Controller request timed out. Input is locked; reconnect or inspect the VM connection."); }
        catch(Exception e) { if(epoch==connectionGeneration) SetOffline(e is ProtocolException ? e.Message : "VM unreachable. Check that the host, VM and private connection are awake."); Log.Limited("status-error",e.GetType().Name,10000); }
        finally { polling=false; RefreshButtons(); }
    }
    private void Render()
    {
        var status=reducer.Current!; var selected=Workspace?.Id; var oldPage=Page?.Id;
        var fresh=StatusReducer.Fresh(status.ObservedAt,DateTimeOffset.UtcNow);
        connection.Text=$"{(fresh ? "Connected" : "Stale telemetry")} · controller {status.Revision}";
        detail.Text=$"Observed {Math.Max(0,(DateTimeOffset.UtcNow-status.ObservedAt).TotalSeconds):0}s ago · VM activity only";
        if(status.Connections?.TryGetValue("google",out var google)==true && google.GetString()!="authorized") detail.Text+=" · Google setup required";
        rows.Children.Clear();
        foreach(var w in status.Workspaces.OrderByDescending(w=>w.Blocker is not null).ThenBy(w=>w.Id)) {
            var age=Math.Max(0,(DateTimeOffset.UtcNow-w.ObservedAt).TotalSeconds);
            var line=Text($"{w.Name} · {w.State} · {w.Actor ?? "no observed worker"}{(w.Role is null ? "" : " / "+w.Role)}\n{w.Task ?? "No observed task"}{(w.Phase is null ? "" : " · "+w.Phase)}\n{(w.Blocker is null ? "" : w.Blocker+" · ")}{age:0}s old · queue {w.Queue} · pending sync {w.PendingSync}" +
                (w.ActionStartedAt is { } start ? $" · action {Math.Max(0,(DateTimeOffset.UtcNow-start).TotalSeconds):0}s" : ""),12);
            line.Margin=new Thickness(0,12,0,0); rows.Children.Add(line);
        }
        // Restore the selection without firing a user-selection change (which revokes grants).
        var keptGrant=grant;
        var oldWorkspace=Workspace;
        var oldDocument=Page?.Generation;
        binding=true;
        try {
            workspaces.ItemsSource=status.Workspaces;
            workspaces.SelectedItem=status.Workspaces.FirstOrDefault(w=>w.Id==selected) ?? status.Workspaces.FirstOrDefault();
            BindPages(oldPage);
        } finally { binding=false; }
        if(Workspace?.Id!=oldWorkspace?.Id || Workspace?.Generation!=oldWorkspace?.Generation || Page?.Id!=oldPage || Page?.Generation!=oldDocument) frame.Source=null;
        grant=keptGrant;
        if(grant != null && !CanInput()) grant=null;
        tray.Text=$"Muse Monitor — {(fresh ? "Connected" : "Stale")}"; RefreshButtons();
    }
    private void BindPages(string? id=null)
    {
        id ??= Page?.Id; pages.ItemsSource=Workspace?.Pages;
        pages.SelectedItem=Workspace?.Pages.FirstOrDefault(p=>p.Id==id) ?? Workspace?.Pages.FirstOrDefault();
    }
    private void SetOffline(string reason)
    {
        grant=null; lastContact=null; connection.Text="Offline · input locked"; detail.Text=reason;
        viewerState.Text="Live view disconnected. Last frame cleared; human pause is not automatically resumed.";
        frame.Source=null; tray.Text="Muse Monitor — Offline"; RefreshButtons();
    }
    private bool Fresh() => lastContact is {} last && DateTimeOffset.UtcNow-last<TimeSpan.FromSeconds(10) &&
        reducer.Current is {} status && StatusReducer.Fresh(status.ObservedAt,DateTimeOffset.UtcNow) && Workspace is {} w && StatusReducer.Fresh(w.ObservedAt,DateTimeOffset.UtcNow);
    private bool CanInput() => Fresh() && StatusReducer.CanInput(grant,Workspace,Page,DateTimeOffset.UtcNow);
    private void RefreshButtons()
    {
        var ready=Fresh() && !mutation;
        if(!Fresh()) { frame.Source=null; viewerState.Text="Live telemetry is stale or offline. Input locked; no current frame."; }
        watch.IsEnabled=ready && Page != null; pause.IsEnabled=ready && Workspace?.State is not ("paused" or "human");
        takeover.IsEnabled=ready && Page != null && Workspace?.State is "paused" or "human";
        resume.IsEnabled=ready && CanInput();
        record.IsEnabled=SafeRecord(Workspace?.RecordUrl); frame.Cursor=CanInput()?System.Windows.Input.Cursors.Cross:System.Windows.Input.Cursors.Arrow;
    }
    private async Task Control(string action)
    {
        if(client is null || Workspace is not {} workspace || !Fresh() || mutation) return;
        mutation=true; RefreshButtons(); var epoch=connectionGeneration;
        try {
            var reply=await client.Control(action,workspace,Page,grant,session.Token);
            if(epoch!=connectionGeneration) return;
            if(!reducer.Accept(reply.Status,DateTimeOffset.UtcNow)) throw new ProtocolException("Control response is stale. Inspect status before acting again.");
            grant=reply.Grant; lastContact=DateTimeOffset.UtcNow; Render(); Log.Write("control",$"{action} acknowledged");
        } catch(Exception e) { if(epoch==connectionGeneration) SetOffline(e is ProtocolException ? e.Message : "Control outcome uncertain. Inspect the VM before retrying; no action was replayed."); Log.Write("control-error",$"{action} {e.GetType().Name}"); }
        finally { mutation=false; RefreshButtons(); }
    }
    private async Task Frame()
    {
        TunePolling();
        if(!PollingPolicy.FetchFrame(IsVisible,WindowState==WindowState.Minimized,viewing) || streaming || !Fresh() || client is null || Workspace is not {} w || Page is not {} p) return;
        streaming=true; var epoch=connectionGeneration;
        try {
            var bytes=await client.Frame(w,p,session.Token);
            if(epoch!=connectionGeneration || !viewing || !IsVisible || !Fresh() || Workspace?.Id!=w.Id || Workspace?.Generation!=w.Generation || Page?.Id!=p.Id || Page?.Generation!=p.Generation) return;
            var bitmap=new BitmapImage(); bitmap.BeginInit(); bitmap.CacheOption=BitmapCacheOption.OnLoad; bitmap.StreamSource=new MemoryStream(bytes); bitmap.EndInit(); bitmap.Freeze();
            frame.Source=bitmap; viewerState.Text=CanInput()?"Human control · click the live view or use the input controls":"Watch · read only · refreshed live";
        } catch(Exception e) { if(epoch==connectionGeneration && Workspace?.Id==w.Id && Workspace?.Generation==w.Generation && Page?.Id==p.Id && Page?.Generation==p.Generation) { frame.Source=null; grant=null; viewerState.Text="Live view unavailable. Input locked; inspect the connection before continuing."; } Log.Limited("frame-error",e.GetType().Name,10000); }
        finally { streaming=false; }
    }
    private async Task Send(InputAction action)
    {
        if(!CanInput() || mutation || client is null || Workspace is not {} w || Page is not {} p) return;
        mutation=true; RefreshButtons(); var epoch=connectionGeneration;
        try { await client.Input(w,p,grant,action,session.Token); Log.Write("viewer-input",$"{action.Kind} acknowledged"); }
        catch(Exception e) { if(epoch==connectionGeneration) SetOffline(e is ProtocolException?e.Message:"Input outcome uncertain. Inspect the VM; no input was replayed."); Log.Write("input-error",e.GetType().Name); }
        finally { mutation=false; RefreshButtons(); }
    }
    private static bool SafeRecord(string? value) => Uri.TryCreate(value,UriKind.Absolute,out var uri) && uri.Scheme=="https" && uri.UserInfo.Length==0 && (uri.Host=="docs.google.com" || uri.Host=="drive.google.com");
    private void OpenRecord() { if(SafeRecord(Workspace?.RecordUrl)) Process.Start(new ProcessStartInfo(Workspace!.RecordUrl!){UseShellExecute=true}); }
    private void Settings()
    {
        var dialog=new Window { Title="Muse Monitor connection", Width=470, Height=340, Owner=this, ResizeMode=ResizeMode.NoResize, WindowStartupLocation=WindowStartupLocation.CenterOwner };
        var panel=new StackPanel { Margin=new Thickness(20) }; dialog.Content=panel;
        panel.Children.Add(Text("Private controller connection",19));
        panel.Children.Add(Text("Use HTTPS, or a local authenticated tunnel. The VM bridge must support Muse protocol 1. Main’s browser SSH key is not a development or forwarding key.",12));
        panel.Children.Add(Text("Controller URL",13)); var endpoint=new TextBox {Text=preferences.Endpoint,Margin=new Thickness(0,5,0,10)}; panel.Children.Add(endpoint);
        panel.Children.Add(Text("Pairing token · leave blank to keep the saved token",12)); var password=new PasswordBox {Margin=new Thickness(0,5,0,8)}; panel.Children.Add(password);
        var error=Text("Token is stored in Windows Credential Manager; never in settings or Git.",12); panel.Children.Add(error);
        panel.Children.Add(Action("Save and reconnect",()=> {
            try {
                var token=password.Password.Length>0?password.Password:CredentialStore.Read()??"";
                using var validate=new ControllerClient(endpoint.Text.Trim(),token);
                CredentialStore.Write(token); preferences=preferences with {Endpoint=validate.Endpoint.AbsoluteUri}; Save(); dialog.Close(); Connect();
            } catch(Exception e) { error.Text=e is ArgumentException?e.Message:"Could not save pairing. Check Windows Credential Manager availability."; Log.Write("pairing-error",e.GetType().Name); }
        }));
        dialog.ShowDialog();
    }
    private async Task Smoke()
    {
        await Dispatcher.InvokeAsync(()=>{},DispatcherPriority.ApplicationIdle);
        var target=args.SkipWhile(a=>a!="--smoke").Skip(1).FirstOrDefault() ?? Path.Combine(Store.Root,"smoke.png");
        if(client != null || watch.IsEnabled || takeover.IsEnabled || resume.IsEnabled) throw new InvalidOperationException("Offline actions must be locked in isolated smoke run.");
        var bitmap=new RenderTargetBitmap((int)ActualWidth,(int)ActualHeight,96,96,PixelFormats.Pbgra32); bitmap.Render(this);
        var encoder=new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); using(var output=File.Create(target)) encoder.Save(output);
        var fixtureNow=DateTimeOffset.UtcNow;
        var fixtureRows=Enumerable.Range(1,20).Select(i=>new WorkspaceState($"fixture-{i}",$"Fixture workspace {i}",1,"waiting","fixture actor","layout test","Synthetic task used only to verify scroll bounds","Waiting for fixture",null,fixtureNow,null,3,2,null,[new PageState("fixture-page","Disposable layout fixture","fixture-document")])).ToArray();
        reducer.Accept(new StatusEnvelope(1,"fixture-only",1,fixtureNow,"SYNTHETIC LAYOUT FIXTURE",fixtureRows),fixtureNow); lastContact=fixtureNow; Render();
        await Dispatcher.InvokeAsync(()=>{},DispatcherPriority.ApplicationIdle);
        var populated=new RenderTargetBitmap((int)ActualWidth,(int)ActualHeight,96,96,PixelFormats.Pbgra32); populated.Render(this);
        var populatedEncoder=new PngBitmapEncoder(); populatedEncoder.Frames.Add(BitmapFrame.Create(populated)); using(var output=File.Create(Path.ChangeExtension(target,"populated.png"))) populatedEncoder.Save(output);
        if(!pause.IsVisible || !record.IsVisible || rows.ActualHeight<88) throw new InvalidOperationException("Populated fixture layout failed.");
        reducer=new(); lastContact=null; RefreshButtons();
        Close(); if(IsVisible) throw new InvalidOperationException("Close must hide in tray."); Reopen(); if(!IsVisible) throw new InvalidOperationException("Tray reopen failed.");
        Topmost=true; Save(); if(!Store.Load().Topmost) throw new InvalidOperationException("Topmost persistence failed.");
        Log.Write("smoke", "offline input locked; rendered; close-to-tray; reopen; settings persistence PASS"); Exit();
    }
}

internal static class CredentialStore
{
    private const string Name="MuseMonitor/controller/v1";
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] private struct Credential {
        public uint Flags,Type; public string TargetName; public string? Comment; public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize; public IntPtr CredentialBlob; public uint Persist,AttributeCount; public IntPtr Attributes; public string? TargetAlias,UserName;
    }
    [DllImport("advapi32.dll",EntryPoint="CredWriteW",CharSet=CharSet.Unicode,SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool WriteNative(ref Credential credential,uint flags);
    [DllImport("advapi32.dll",EntryPoint="CredReadW",CharSet=CharSet.Unicode,SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] private static extern bool ReadNative(string target,uint type,uint flags,out IntPtr credential);
    [DllImport("advapi32.dll")] private static extern void CredFree(IntPtr credential);
    public static string? Read() {
        if(!ReadNative(Name,1,0,out var ptr)) { if(Marshal.GetLastWin32Error()==1168) return null; throw new System.ComponentModel.Win32Exception(); }
        try {var c=Marshal.PtrToStructure<Credential>(ptr); return Marshal.PtrToStringUni(c.CredentialBlob,(int)c.CredentialBlobSize/2);} finally {CredFree(ptr);}
    }
    public static void Write(string value) {
        var ptr=Marshal.StringToCoTaskMemUni(value);
        try {var c=new Credential {Type=1,TargetName=Name,CredentialBlob=ptr,CredentialBlobSize=(uint)Encoding.Unicode.GetByteCount(value),Persist=2,UserName="MuseMonitor"}; if(!WriteNative(ref c,0)) throw new System.ComponentModel.Win32Exception();}
        finally {Marshal.ZeroFreeCoTaskMemUnicode(ptr);}
    }
}
