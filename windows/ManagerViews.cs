using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Automation;
using MuseMonitor.Client;
using Button = System.Windows.Controls.Button;
using CheckBox = System.Windows.Controls.CheckBox;
using ComboBox = System.Windows.Controls.ComboBox;
using Orientation = System.Windows.Controls.Orientation;
using Panel = System.Windows.Controls.Panel;
using TextBox = System.Windows.Controls.TextBox;

namespace MuseMonitor;

internal sealed partial class ManagerWindow
{
    private readonly StackPanel goalsView = new();
    private readonly StackPanel agentsView = new();
    private readonly StackPanel workView = new();
    private readonly StackPanel reviewsView = new();
    private readonly StackPanel activityView = new();
    private readonly StackPanel relayView = new();
    private readonly StackPanel vaultView = new();
    private bool relayConfigured;
    private bool crosswalkAssignment;
    private bool autoCrosswalkReview;
    private (string Id, string Name)[] crosswalkTargets = [];

    private void AddManagerTabs()
    {
        AddTab("Goals", goalsView);
        AddTab("Agents", agentsView);
        AddTab("Work Board", workView);
        AddTab("Reviews", reviewsView);
        AddTab("Activity", activityView);
        AddTab("Research Relay", relayView);
        AddTab("AgentVault", vaultView);
        var query = Field(vaultView, "Search approved local notes", "");
        var found = new StackPanel();
        vaultView.Children.Add(Action("Search vault", () => {
            found.Children.Clear();
            try {
                foreach (var result in ReadVault(query.Text)) {
                    found.Children.Add(Text(result.Name, true));
                    found.Children.Add(Text(result.Excerpt));
                    found.Children.Add(Text($"Provenance {result.Source} · {result.Updated}"));
                }
                if (found.Children.Count == 0) found.Children.Add(Text("No matching approved local notes found."));
            } catch (Exception error) when (error is IOException or UnauthorizedAccessException) {
                found.Children.Add(Text("Could not read the existing AgentVault. Check the local vault path."));
                Log.Write("manager-vault-error", error.GetType().Name);
            }
        }));
        vaultView.Children.Add(found);
    }

    private void AddTab(string title, StackPanel content)
    {
        content.Margin = new Thickness(10);
        var frame = new DockPanel();
        var refresh = Action("Refresh AI Manager", () => _ = RunManager(async client => await RefreshManager(client)));
        DockPanel.SetDock(refresh, Dock.Top);
        if (title != "AgentVault") frame.Children.Add(refresh);
        frame.Children.Add(new ScrollViewer { Content = content, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        tabs.Items.Add(new TabItem { Header = title, Content = frame });
    }

    private async Task RunManager(Func<ControllerClient, Task> action)
    {
        if (busy) return;
        busy = true;
        try {
            using var client = connect();
            await action(client);
        } catch (OperationCanceledException) when (lifetime.IsCancellationRequested) {
        } catch (Exception error) {
            status.Text = error is ProtocolException or ArgumentException ? error.Message :
                "Manager request could not be verified. Refresh the Work Board before retrying.";
            Log.Write("manager-action-error", error.GetType().Name);
        } finally {
            busy = false;
        }
    }

    private async Task RefreshManager(ControllerClient client)
    {
        try {
            var state = await client.Manager(lifetime.Token);
            if (state.GetProperty("schema").GetInt32() != 1) throw new ProtocolException("Unsupported manager response.");
            RenderManager(state);
        } catch (OperationCanceledException) when (lifetime.IsCancellationRequested) {
        } catch (Exception error) {
            foreach (var view in new[] { goalsView, agentsView, workView, reviewsView, activityView, relayView }) {
                view.Children.Clear();
                view.Children.Add(Text("AI Manager data is stale or unavailable. Check the installed controller revision."));
            }
            relayConfigured = false;
            crosswalkTargets = [];
            Log.Write("manager-dashboard-error", error.GetType().Name);
        }
    }

    private void RenderManager(JsonElement state)
    {
        var goals = state.GetProperty("goals").EnumerateArray().ToArray();
        var board = state.GetProperty("board").EnumerateArray().ToArray();
        foreach (var view in new[] { goalsView, agentsView, workView, reviewsView, activityView, relayView }) view.Children.Clear();
        relayConfigured = false;
        crosswalkAssignment = state.TryGetProperty("crosswalkAssignment", out var assignment) &&
            assignment.ValueKind == JsonValueKind.True;
        autoCrosswalkReview = state.TryGetProperty("autoCrosswalkReview", out var automatic) &&
            automatic.ValueKind == JsonValueKind.True;
        crosswalkTargets = state.TryGetProperty("crosswalkTargets", out var targets) ?
            targets.EnumerateArray().Select(role => (Str(role, "id"), Str(role, "label"))).ToArray() : [];
        if (state.TryGetProperty("relayTarget", out var target)) RenderRelayTarget(target);
        else relayView.Children.Add(Text("This controller requires administrator configuration for the Relay target."));
        foreach (var goal in goals) {
            var row = new StackPanel { Margin = new Thickness(0, 7, 0, 12) };
            var id = Str(goal, "id");
            row.Children.Add(Text(Str(goal, "label"), true));
            var active = new CheckBox { Content = "Enabled", IsChecked = goal.GetProperty("enabled").GetBoolean() };
            row.Children.Add(active);
            var weight = Field(row, "Weight from 0 to 100", Str(goal, "weight"));
            var constraints = Field(row, "Constraints, one per line", Join(goal, "constraints"), 2);
            row.Children.Add(Text($"{Str(goal, "verifiedOutcomes", "0")} verified outcomes shown in the recent board."));
            var revision = goal.GetProperty("revision").GetInt32();
            row.Children.Add(Action("Save priority", () => _ = RunManager(async client => {
                if (!int.TryParse(weight.Text, out int score) || score is < 0 or > 100)
                    throw new ArgumentException("Enter a whole-number weight from 0 to 100.");
                await client.ManagerAction(new { action = "goal", goal = id, weight = score,
                    enabled = active.IsChecked == true, constraints = Lines(constraints.Text), revision }, lifetime.Token);
                await RefreshManager(client);
                status.Text = "Goal priority saved in Ubuntu.";
            })));
            goalsView.Children.Add(row);
        }
        foreach (var agent in state.GetProperty("roster").EnumerateArray()) {
            agentsView.Children.Add(Text($"{Str(agent, "provider")} · {Str(agent, "account")} · {Str(agent, "enrollment")}", true));
            agentsView.Children.Add(Text(Str(agent, "role")));
            agentsView.Children.Add(Text(Join(agent, "capabilities", " · ")));
        }
        workView.Children.Add(Action("New task", () => NewTask(goals)));
        foreach (var task in board) {
            var id = Str(task, "taskId");
            var stage = Str(task, "state");
            var summary = new StackPanel { Margin = new Thickness(0, 6, 0, 9) };
            summary.Children.Add(Text($"{stage} · {Str(task, "objective")}", true));
            summary.Children.Add(Text($"Rank {Str(task, "score")} · {Str(task, "owner", "unclaimed")} · {Str(task, "nextSafeAction")}"));
            if (Str(task, "crosswalkReviewer", "") is { Length: > 0 } reviewer)
                summary.Children.Add(Text($"Muse reviewer · {ReviewerName(reviewer)}"));
            if (Str(task, "blocker", "") is { Length: > 0 } blocker) summary.Children.Add(Text(blocker));
            if (task.TryGetProperty("crosswalk", out var dispatch) && dispatch.ValueKind == JsonValueKind.Object)
                summary.Children.Add(Text(DispatchStatus(dispatch)));
            summary.Children.Add(Action("Open details", () => _ = RunManager(client => ReadTask(client, id))));
            workView.Children.Add(summary);
            if (stage is "reviewing" or "blocked" or "done") {
                reviewsView.Children.Add(Text($"{stage} · {Str(task, "objective")}", true));
                reviewsView.Children.Add(Action("View draft and review", () => _ = RunManager(client => ReadTask(client, id))));
            }
            if (task.TryGetProperty("relay", out var relay) && relay.ValueKind == JsonValueKind.Object) {
                relayView.Children.Add(Text($"{Str(relay, "state")} · {Str(task, "objective")}", true));
                relayView.Children.Add(Text(Str(relay, "detail")));
            }
        }
        if (board.Length == 0) workView.Children.Add(Text("No manager tasks in the journal."));
        if (reviewsView.Children.Count == 0) reviewsView.Children.Add(Text("No recorded reviews yet."));
        if (!board.Any(task => task.TryGetProperty("relay", out var relay) && relay.ValueKind == JsonValueKind.Object))
            relayView.Children.Add(Text("No published Relay packets yet."));
        var metrics = state.GetProperty("metrics");
        activityView.Children.Add(Text($"{Str(metrics, "recentItems")} recent items · {Str(metrics, "scope")}", true));
        activityView.Children.Add(Text($"Inference stages reserved today {Str(metrics, "reservedCallsToday")} / {Str(metrics, "dailyCallLimit")}"));
        activityView.Children.Add(Text($"Last provider probe {Str(metrics, "modelLatencyMs", "unmeasured")} ms"));
        foreach (var item in metrics.GetProperty("recentStates").EnumerateObject())
            activityView.Children.Add(Text($"{item.Name} · {item.Value.GetInt32()}"));
    }

    private void RenderRelayTarget(JsonElement target)
    {
        relayConfigured = target.GetProperty("configured").GetBoolean();
        relayView.Children.Add(Text(relayConfigured ? "Approved Relay destination" : "Set the Relay destination", true));
        relayView.Children.Add(Text("Use a private Drive folder and the exact ID of the existing ChatGPT scheduled task that will read it."));
        var folder = Field(relayView, "Google Drive folder link or ID", Str(target, "folderId", ""));
        var consumer = Field(relayView, "Scheduled task ID", Str(target, "consumerTaskId", ""));
        var revision = target.GetProperty("revision").GetInt32();
        relayView.Children.Add(Action("Verify and save destination", () => _ = RunManager(async client => {
            var folderId = folder.Text.Trim();
            if (Uri.TryCreate(folderId, UriKind.Absolute, out var url) && url.Scheme == Uri.UriSchemeHttps &&
                url.Host == "drive.google.com" && url.UserInfo.Length == 0 && url.IsDefaultPort) {
                var match = Regex.Match(url.AbsolutePath, @"^/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)/?$");
                if (match.Success) folderId = match.Groups[1].Value;
            }
            var consumerTaskId = consumer.Text.Trim();
            if (!Regex.IsMatch(folderId, @"\A[A-Za-z0-9_-]{15,150}\z") ||
                !Regex.IsMatch(consumerTaskId, @"\A[A-Za-z0-9_-]{8,150}\z"))
                throw new ArgumentException("Enter a valid Drive folder link or ID and the exact scheduled task ID.");
            try {
                await client.ManagerAction(new { action = "relay-configure", folderId, consumerTaskId, revision }, lifetime.Token);
            } catch (ProtocolException) {
                throw new ProtocolException("Could not save the Relay destination. Verify Google, confirm folder access, and refresh before retrying.");
            }
            await RefreshManager(client);
            status.Text = "Private Relay destination verified and saved.";
        })));
    }

    private void NewTask(JsonElement[] goals)
    {
        var form = new StackPanel { Margin = new Thickness(18) };
        var dialog = new Window { Owner = this, Title = "New AI Manager task", Width = 640, Height = 680,
            WindowStartupLocation = WindowStartupLocation.CenterOwner, Content = new ScrollViewer { Content = form } };
        var goal = new ComboBox { ItemsSource = goals.Select(g => new { Id = Str(g, "id"), Label = Str(g, "label") }).ToArray(),
            DisplayMemberPath = "Label", SelectedValuePath = "Id", SelectedIndex = 0 };
        var capability = new ComboBox { ItemsSource = new[] { "research", "email-review" }, SelectedIndex = 0 };
        form.Children.Add(Text("Goal", true)); form.Children.Add(goal);
        form.Children.Add(Text("Capability", true)); form.Children.Add(capability);
        var objective = Field(form, "Objective", "", 2);
        var criteria = Field(form, "Observable acceptance criteria, one per line", "", 3);
        var priority = Field(form, "Priority from 1 to 5", "3");
        var due = Field(form, "Optional due date in local time", "");
        var researchFields = new StackPanel();
        var sources = Field(researchFields, "Public GitHub issue or Release, Reddit discussion, or approved documentation URL, one per line", "", 3);
        var reviewer = ReviewerPicker(crosswalkTargets.Prepend(("", "Choose later")));
        var autoReview = new CheckBox { Content = "Send the public links automatically when Hermes finishes",
            IsChecked = true, IsEnabled = false, Margin = new Thickness(0, 5, 0, 5) };
        if (crosswalkAssignment) {
            researchFields.Children.Add(Text("Muse reviewer", true));
            researchFields.Children.Add(reviewer);
            if (crosswalkTargets.Length == 0)
                researchFields.Children.Add(Text("Verify a Muse account in Setup & Connections to assign reviews."));
            else if (autoCrosswalkReview) {
                researchFields.Children.Add(autoReview);
                reviewer.SelectionChanged += (_, _) => autoReview.IsEnabled =
                    reviewer.SelectedValue is string { Length: > 0 };
            } else
                researchFields.Children.Add(Text("You can send the public links to this account after Hermes finishes."));
        }
        form.Children.Add(researchFields);
        var emailFields = new StackPanel { Visibility = Visibility.Collapsed };
        var recipient = Field(emailFields, "Recipient name or role", "");
        var subject = Field(emailFields, "Subject", "");
        var draft = Field(emailFields, "Original draft", "", 5);
        var facts = Field(emailFields, "Approved facts to share with Hermes, one per line", "", 3);
        form.Children.Add(emailFields);
        capability.SelectionChanged += (_, _) => {
            bool isEmail = (string?)capability.SelectedItem == "email-review";
            researchFields.Visibility = isEmail ? Visibility.Collapsed : Visibility.Visible;
            emailFields.Visibility = isEmail ? Visibility.Visible : Visibility.Collapsed;
        };
        var requestId = Guid.NewGuid().ToString("N");
        var message = Text("");
        var submit = Action("Queue task", () => _ = RunManager(async client => {
            if (!int.TryParse(priority.Text, out int p) || p is < 1 or > 5) {
                message.Text = "Enter a priority from 1 to 5.";
                return;
            }
            long? dueAt = null;
            if (due.Text.Trim().Length > 0) {
                if (!DateTimeOffset.TryParse(due.Text, CultureInfo.CurrentCulture, DateTimeStyles.AssumeLocal, out var values)) {
                    message.Text = "Enter a recognizable date and optional time.";
                    return;
                }
                dueAt = values.ToUnixTimeSeconds();
            }
            bool email = (string?)capability.SelectedItem == "email-review";
            var selectedReviewer = !email && crosswalkAssignment && reviewer.SelectedValue is string { Length: > 0 } role
                ? role : null;
            try {
                await client.ManagerAction(new { action = "create", requestId, goal = goal.SelectedValue,
                    capability = capability.SelectedItem, objective = objective.Text, criteria = Lines(criteria.Text), priority = p, dueAt,
                    sources = email ? [] : Lines(sources.Text), facts = email ? Lines(facts.Text) : [],
                    recipient = email ? recipient.Text : null, subject = email ? subject.Text : null, draft = email ? draft.Text : null,
                    crosswalkReviewer = selectedReviewer,
                    autoCrosswalkReview = selectedReviewer is not null && autoCrosswalkReview && autoReview.IsChecked == true },
                    lifetime.Token);
                dialog.Close();
                await RefreshManager(client);
                status.Text = "Task accepted by the controller journal.";
            } catch (Exception error) {
                message.Text = "The controller did not acknowledge the task. Check required fields and refresh before retrying.";
                Log.Write("manager-intake-error", error.GetType().Name);
            }
        }));
        submit.IsDefault = true;
        form.Children.Add(submit);
        var cancel = Action("Cancel", dialog.Close);
        cancel.IsCancel = true;
        form.Children.Add(cancel);
        form.Children.Add(message);
        dialog.ShowDialog();
    }

    private async Task ReadTask(ControllerClient client, string id)
    {
        var value = await client.ManagerAction(new { action = "read", taskId = id }, lifetime.Token);
        var resultsView = new StackPanel();
        resultsView.Children.Add(Text(Str(value.GetProperty("packet"), "objective"), true));
        resultsView.Children.Add(Text($"{Str(value, "state")} · {Str(value, "nextSafeAction")}"));
        if (Str(value.GetProperty("packet"), "crosswalkReviewer", "") is { Length: > 0 } reviewer)
            resultsView.Children.Add(Text($"Muse reviewer · {ReviewerName(reviewer)}"));
        foreach (var name in new[] { "plan", "result", "review", "history", "sourceEvidence" }) {
            if (value.TryGetProperty(name, out var part)) {
                resultsView.Children.Add(Text(name switch { "sourceEvidence" => "Collected evidence", "history" => "Activity history",
                    "plan" => "Plan", "review" => "Review", _ => "Result" }, true));
                if (name == "result" && part.TryGetProperty("draft", out var draft))
                    Field(resultsView, "Reviewed draft", draft.GetString() ?? "", 6).IsReadOnly = true;
                if (name == "result" && value.GetProperty("packet").TryGetProperty("draft", out var original))
                    Field(resultsView, "Original draft", original.GetString() ?? "", 5).IsReadOnly = true;
                resultsView.Children.Add(Text(Readable(part)));
            }
        }
        var dialog = new Window { Title = "AI Manager task", Owner = this, Width = 700, Height = 660,
            WindowStartupLocation = WindowStartupLocation.CenterOwner, Content = new ScrollViewer { Content = resultsView } };
        resultsView.Margin = new Thickness(18);
        var stage = Str(value, "state");
        var revision = value.GetProperty("revision").GetInt32();
        if (stage == "done" && Str(value.GetProperty("packet"), "capability") == "research") {
            var relayAction = value.TryGetProperty("relay", out var relay) ?
                Str(relay, "state") == "stored" ? null : "relay-reconcile" : relayConfigured ? "relay-publish" : null;
            if (relayAction is not null) resultsView.Children.Add(Action(
                relayAction == "relay-publish" ? "Save to approved Research Relay" : "Reconcile Relay file",
                () => _ = RunManager(async connection => {
                    var receipt = await connection.ManagerAction(new { action = relayAction, taskId = id, revision }, lifetime.Token);
                    dialog.Close();
                    await RefreshManager(connection);
                    status.Text = Str(receipt, "detail", "Research Relay received the request.");
                })));
            if (value.TryGetProperty("relay", out relay))
                resultsView.Children.Add(Text(Str(relay, "detail")));
            else if (!relayConfigured)
                resultsView.Children.Add(Text("Set the private Drive destination in Research Relay to publish this result."));
            AddCrosswalkDispatch(resultsView, dialog, value, id, revision);
        }
        foreach (var verb in stage switch { "blocked" or "unknown" => new[] { "retry", "cancel" },
            "done" or "cancelled" => [], _ => new[] { "cancel" } }) {
            var button = Action(verb == "retry" ? "Retry bounded analysis" : "Cancel task", () => _ = RunManager(async connection => {
                await connection.ManagerAction(new { action = verb, taskId = id, revision }, lifetime.Token);
                dialog.Close();
                await RefreshManager(connection);
                status.Text = "Task state acknowledged.";
            }));
            resultsView.Children.Add(button);
        }
        dialog.Closed += (_, _) => { if (dialog.Content is ScrollViewer holder) holder.Content = null; };
        dialog.Show();
    }

    private void AddCrosswalkDispatch(Panel root, Window dialog, JsonElement value, string taskId, int revision)
    {
        if (value.TryGetProperty("crosswalk", out var record) && record.ValueKind == JsonValueKind.Object) {
            root.Children.Add(Text($"Sent to {ReviewerName(Str(record, "role", ""))}"));
            root.Children.Add(Text(DispatchStatus(record)));
            if (Str(record, "state") != "reply-verified")
                root.Children.Add(Action("Check Crosswalk reply", () => _ = RunManager(async client => {
                    var result = await client.ManagerAction(new { action = "crosswalk-dispatch-reconcile", taskId }, lifetime.Token);
                    dialog.Close();
                    await RefreshManager(client);
                    status.Text = DispatchStatus(result);
                })));
            return;
        }
        var assigned = Str(value.GetProperty("packet"), "crosswalkReviewer", "");
        var eligible = crosswalkTargets.Where(role => assigned.Length == 0 || role.Id == assigned).ToArray();
        if (eligible.Length == 0) {
            if (assigned.Length > 0)
                root.Children.Add(Text("Verify the assigned account in Setup & Connections before sending this review."));
            return;
        }
        root.Children.Add(Text("Send the cited public sources for an independent review", true));
        var roles = ReviewerPicker(assigned.Length == 0 ? eligible.Prepend(("", "Select a Muse account")) : eligible);
        if (assigned.Length == 0) root.Children.Add(roles);
        var send = Action(assigned.Length == 0 ? "Request review on private Crosswalk board" : $"Request review from {eligible[0].Name}", () => _ = RunManager(async client => {
            var receipt = await client.ManagerAction(new {
                action = "crosswalk-dispatch", taskId, revision, role = (string?)roles.SelectedValue
            }, lifetime.Token);
            dialog.Close();
            await RefreshManager(client);
            status.Text = DispatchStatus(receipt);
        }));
        send.IsEnabled = assigned.Length > 0;
        roles.SelectionChanged += (_, _) => send.IsEnabled = roles.SelectedValue is string { Length: > 0 };
        root.Children.Add(send);
    }

    private static ComboBox ReviewerPicker(IEnumerable<(string Id, string Name)> choices)
    {
        var picker = new ComboBox { ItemsSource = choices.Select(role => new { role.Id, role.Name }).ToArray(),
            DisplayMemberPath = "Name", SelectedValuePath = "Id", SelectedIndex = 0 };
        AutomationProperties.SetName(picker, "Muse reviewer");
        return picker;
    }

    private string ReviewerName(string role) => role switch {
        "main" => "Main", "alt" => "Alt", "dad" => "Dad", "sister" => "Sister", _ => role
    };

    private static string DispatchStatus(JsonElement record) => Str(record, "state") switch {
        "reply-verified" => "Crosswalk reply matched the intended role and original post. Review the findings on the private board.",
        "posted" => "Crosswalk review request posted. Awaiting a verified reply.",
        _ => "Crosswalk post outcome is unknown. Check for a reply before taking any further action."
    };

    private static TextBox Field(Panel panel, string label, string value, int lines = 1)
    {
        panel.Children.Add(Text(label));
        var field = new TextBox { Text = value, MinLines = lines, AcceptsReturn = lines > 1,
            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 4, 0, 7),
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        AutomationProperties.SetName(field, label);
        panel.Children.Add(field);
        return field;
    }

    private static string[] Lines(string value) => value.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
    private static string Str(JsonElement value, string key, string fallback = "—") =>
        value.TryGetProperty(key, out var element) && element.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined) ? element.ToString() : fallback;
    private static string Join(JsonElement value, string key, string separator = "\n") =>
        value.TryGetProperty(key, out var element) && element.ValueKind == JsonValueKind.Array ?
            string.Join(separator, element.EnumerateArray().Select(e => e.ToString())) : "";
    private static string Readable(JsonElement value) => value.ValueKind switch {
        JsonValueKind.Array => string.Join("\n\n", value.EnumerateArray().Select(Readable)),
        JsonValueKind.Object => string.Join("\n", value.EnumerateObject().Where(p => p.Name is not ("draft" or "etag" or "lastModified"))
            .Select(p => $"{p.Name}  {Readable(p.Value)}")),
        _ => value.ToString()
    };

    private static IEnumerable<(string Name, string Excerpt, string Source, string Updated)> ReadVault(string query)
    {
        var terms = Regex.Matches(query, "[\\p{L}\\p{Nd}]{2,}").Select(m => m.Value).Distinct(StringComparer.OrdinalIgnoreCase).Take(8).ToArray();
        if (terms.Length == 0) yield break;
        var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "AgentVault");
        if (!Directory.Exists(root) || (File.GetAttributes(root) & FileAttributes.ReparsePoint) != 0) yield break;
        var candidates = new List<(string Name, string Excerpt, string Source, string Updated, int Score)>();
        foreach (var name in new[] { "01-Projects", "00-Rules" }) {
            var directory = Path.Combine(root, name);
            if (!Directory.Exists(directory) || (File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0) continue;
            foreach (var file in Directory.EnumerateFiles(directory, "*.md").Take(200)) {
                if ((File.GetAttributes(file) & FileAttributes.ReparsePoint) != 0 || new FileInfo(file).Length > 32_768) continue;
                var value = File.ReadAllText(file).Replace("\r\n", "\n");
                var split = value.IndexOf("\n---\n", 4, StringComparison.Ordinal);
                if (!value.StartsWith("---\n") || split < 0) continue;
                var metadata = value[..split];
                if (!metadata.Contains("\nstatus: canon\n") || !Regex.IsMatch(metadata, "confidence: extracted(?:\\n|$)")) continue;
                var body = value[(split + 5)..];
                var score = terms.Count(term => body.Contains(term, StringComparison.OrdinalIgnoreCase));
                if (score == 0) continue;
                var excerpt = string.Join("\n", body.Split('\n').Where(line => terms.Any(term =>
                    line.Contains(term, StringComparison.OrdinalIgnoreCase))).Take(5));
                candidates.Add((Path.GetFileNameWithoutExtension(file), excerpt[..Math.Min(1600, excerpt.Length)],
                    Regex.Match(metadata, "(?m)^source: (.+)$").Groups[1].Value,
                    Regex.Match(metadata, "(?m)^updated: (.+)$").Groups[1].Value, score));
            }
        }
        foreach (var item in candidates.OrderByDescending(candidate => candidate.Score).ThenByDescending(candidate => candidate.Updated).Take(6))
            yield return (item.Name, item.Excerpt, item.Source, item.Updated);
    }
}
