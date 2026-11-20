namespace MuseMonitor.Client;

// Viewing is optional overhead. Hidden/minimized HUDs never request images;
// ownership freshness and input checks remain independent of polling frequency.
public static class PollingPolicy
{
    public static TimeSpan Status(bool visible, bool minimized) =>
        TimeSpan.FromSeconds(visible && !minimized ? 2 : 8);
    public static TimeSpan Frame(bool human) => TimeSpan.FromMilliseconds(human ? 500 : 1000);
    public static bool FetchFrame(bool visible, bool minimized, bool viewing) => visible && !minimized && viewing;
}
