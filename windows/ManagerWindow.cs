using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using MuseMonitor.Client;
using Button = System.Windows.Controls.Button;
using Orientation = System.Windows.Controls.Orientation;
using TabControl = System.Windows.Controls.TabControl;

namespace MuseMonitor;

internal sealed partial class ManagerWindow : Window
{
    private readonly Func<ControllerClient> connect;
    private readonly CancellationTokenSource lifetime = new();
    private readonly StackPanel entries = new();
    private readonly TextBlock status = Text("Checking the paired Ubuntu controller…");
    private readonly StackPanel toolbar = new() { Orientation = Orientation.Horizontal };
    private readonly TabControl tabs = new();
    private bool busy;

    internal ManagerWindow(Func<ControllerClient> connect)
    {
        this.connect = connect;
        Title = "MetaMuse · AI Manager";
        Width = 820;
        Height = 630;
        MinWidth = 550;
        MinHeight = 430;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        FontFamily = new System.Windows.Media.FontFamily("Segoe UI");
        FontSize = 14;
        var layout = new DockPanel { Margin = new Thickness(20) };
        Content = layout;
        status.Margin = new Thickness(0, 0, 0, 10);
        DockPanel.SetDock(status, Dock.Top);
        layout.Children.Add(status);
        layout.Children.Add(tabs);
        var connections = new DockPanel { Margin = new Thickness(10) };
        DockPanel.SetDock(toolbar, Dock.Top);
        toolbar.Children.Add(Action("Refresh status", () => _ = Refresh()));
        toolbar.Children.Add(Action("Verify Google", () => _ = Refresh("google")));
        toolbar.Children.Add(Action("Verify Gmail", () => _ = Refresh("gmail")));
        connections.Children.Add(toolbar);
        connections.Children.Add(new ScrollViewer { Content = entries, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        tabs.Items.Add(new TabItem { Header = "Setup & Connections", Content = connections });
        AddManagerTabs();
        Loaded += (_, _) => _ = Refresh();
        Closed += (_, _) => lifetime.Cancel();
    }

    internal void SelectTab(string name)
    {
        tabs.SelectedItem = tabs.Items.OfType<TabItem>().First(tab => Equals(tab.Header, name));
    }

    private async Task Refresh(string? id = null)
    {
        if (busy) return;
        busy = true;
        toolbar.IsEnabled = false;
        status.Text = id is null ? "Reading current connection status…" : $"Verifying {id} in Ubuntu…";
        try {
            using var client = connect();
            var result = id is null ? await client.Connections(lifetime.Token) : await client.VerifyConnection(id, lifetime.Token);
            if (result.Schema != 1 || result.Entries.Length > 64)
                throw new ProtocolException("Connection response is incompatible.");
            entries.Children.Clear();
            var host = await CheckHost(lifetime.Token);
            foreach (var value in result.Entries) {
                var entry = host.GetValueOrDefault(value.Id, value);
                var card = new StackPanel { Margin = new Thickness(0, 5, 6, 10) };
                card.Children.Add(Text($"{entry.Label}  ·  {entry.Status}{(entry.Stale ? " (stale)" : "")}", true));
                card.Children.Add(Text($"{entry.Account}  ·  {entry.Scope}"));
                card.Children.Add(Text(entry.Detail));
                if (entry.VerifiedAt is { } checkedAt)
                    card.Children.Add(Text($"Last checked {checkedAt.ToLocalTime():MMM d, h:mm tt}"));
                if (ConsentLink(entry.ConsentUrl) is { } link)
                    card.Children.Add(Action("Open official page", () => Process.Start(new ProcessStartInfo(link.AbsoluteUri) { UseShellExecute = true })));
                if (entry.Id == "crosswalk-hermes")
                    card.Children.Add(Action("Verify board membership", () => _ = Crosswalk("crosswalk-check", null)));
                else if (entry.Id.StartsWith("crosswalk-", StringComparison.Ordinal)) {
                    var role = entry.Id["crosswalk-".Length..];
                    card.Children.Add(Action("Post connection test", () => _ = Crosswalk("crosswalk-start", role)));
                    card.Children.Add(Action("Check for verified reply", () => _ = Crosswalk("crosswalk-reconcile", role)));
                }
                entries.Children.Add(card);
            }
            status.Text = $"Ubuntu replied at {result.ObservedAt.ToLocalTime():h:mm:ss tt}";
            if (id is null) await RefreshManager(client);
        } catch (OperationCanceledException) when (lifetime.IsCancellationRequested) {
            // Window closed.
        } catch (Exception error) {
            entries.Children.Clear();
            status.Text = "Connection status unavailable. Check the paired controller and its installed revision.";
            Log.Write("manager-connections-error", error.GetType().Name);
        } finally {
            busy = false;
            toolbar.IsEnabled = true;
        }
    }

    private static async Task<Dictionary<string, ConnectionState>> CheckHost(CancellationToken token)
    {
        var github = HostCommand("gh", ["api", "repos/example-owner/muse-monitor", "--jq",
            "{repo:.full_name, private:.private, push:.permissions.push}"], token);
        var config = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".ssh", "muse-workspace-dev.conf");
        var ssh = File.Exists(config) ? HostCommand("ssh.exe", ["-F", config, "-o", "BatchMode=yes", "-o",
            "ConnectTimeout=5", "muse-workspace-dev", "id -un"], token) : Task.FromResult<string?>(null);
        var vault = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "AgentVault");
        var rootOk = File.Exists(System.IO.Path.Combine(vault, "AGENTS.md")) &&
            File.Exists(System.IO.Path.Combine(vault, "index.md")) && Directory.Exists(System.IO.Path.Combine(vault, "01-Projects"));
        string? proof = await github;
        bool repoOk = false;
        if (proof != null) try {
            using var result = JsonDocument.Parse(proof);
            var value = result.RootElement;
            repoOk = Str(value, "repo") == "example-owner/muse-monitor" &&
                value.GetProperty("private").GetBoolean() && value.GetProperty("push").GetBoolean();
        } catch (Exception error) when (error is JsonException or KeyNotFoundException or InvalidOperationException) {
            Log.Write("manager-github-response", error.GetType().Name);
        }
        bool sshOk = (await ssh)?.Trim() == "muse-operator";
        var now = DateTimeOffset.UtcNow;
        return new() {
            ["github"] = new("github", "Private GitHub repository", "example-owner/muse-monitor", "Private source and release access",
                repoOk ? "Operational" : "Permission limited",
                repoOk ? "GitHub API confirmed private visibility and push access." : "The current GitHub CLI account did not confirm private push access.",
                "https://github.com/example-owner/muse-monitor", now, false),
            ["development"] = new("development", "Windows to Ubuntu development", "muse-operator", "Pinned VMnet8 SSH",
                sshOk ? "Operational" : "Unavailable",
                sshOk ? "The approved development alias returned the expected Ubuntu identity." : "The approved SSH alias did not return the expected identity.",
                null, now, false),
            ["vault"] = new("vault", "AgentVault / Obsidian", "Windows owner", "Curated local Markdown only",
                rootOk ? "Tested read" : "Unavailable",
                rootOk ? "Existing vault, index and approved project directory resolved. Sync is unverified." : "The existing local vault could not be resolved.",
                null, now, false)
        };
    }

    private static async Task<string?> HostCommand(string exe, string[] args, CancellationToken token)
    {
        using var ceiling = CancellationTokenSource.CreateLinkedTokenSource(token);
        ceiling.CancelAfter(TimeSpan.FromSeconds(8));
        var options = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var arg in args) options.ArgumentList.Add(arg);
        try {
            using var child = Process.Start(options);
            if (child is null) return null;
            var output = child.StandardOutput.ReadToEndAsync(ceiling.Token);
            var errors = child.StandardError.ReadToEndAsync(ceiling.Token);
            try {
                await child.WaitForExitAsync(ceiling.Token);
                var text = await output;
                await errors;
                return child.ExitCode == 0 && text.Length <= 4096 ? text : null;
            } catch (OperationCanceledException) {
                if (!child.HasExited) child.Kill();
                return null;
            }
        } catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException) {
            Log.Write("manager-host-check", error.GetType().Name);
            return null;
        }
    }

    private Task Crosswalk(string action, string? role) => RunManager(async client => {
        var receipt = await client.ManagerAction(new { action, role }, lifetime.Token);
        status.Text = action == "crosswalk-check" ? "Private Crosswalk board membership verified." :
            $"Crosswalk connection test · {Str(receipt, "state")}. A post does not confirm a reply.";
    });

    internal static Uri? ConsentLink(string? value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.Scheme != "https" || !uri.IsDefaultPort ||
            uri.UserInfo.Length != 0 || uri.Fragment.Length != 0) return null;
        return (uri.Host, uri.AbsolutePath, uri.Query) switch {
            ("auth.openai.com", "/codex/device", "") or ("muse.ai", "/", "") or
            ("crosswalk.to", "/keys", "" or "?for=muse") or ("chatgpt.com", "/schedules", "") or
            ("console.cloud.google.com", "/auth/overview", "") or
            ("github.com", "/example-owner/muse-monitor", "") => uri,
            _ => null
        };
    }

    private static TextBlock Text(string value, bool bold = false) =>
        new() { Text = value, TextWrapping = TextWrapping.Wrap, FontWeight = bold ? FontWeights.SemiBold : FontWeights.Normal };

    private static Button Action(string name, Action handler)
    {
        var button = new Button { Content = name, Margin = new Thickness(0, 5, 8, 5), Padding = new Thickness(10, 5, 10, 5),
            HorizontalAlignment = System.Windows.HorizontalAlignment.Left };
        button.Click += (_, _) => handler();
        return button;
    }
}
