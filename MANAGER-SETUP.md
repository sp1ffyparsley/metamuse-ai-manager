# Manager setup and verification

## Operator flow

1. Open Muse Monitor on Windows and select **Work Board**. Use **Setup & Connections** to see the status and scope of the authorized accounts.
2. Create a task with a bounded objective and observable acceptance criteria. For research, provide allowed public sources. For draft review, supply the draft and approved facts.
3. The Ubuntu controller admits the work to the journal. Hermes plans, produces a result and checks it against the criteria. The Work Board shows the recorded output and current state.
4. You can choose an independently verified Muse reviewer for a public research task. With automatic delivery enabled, successful Hermes review authorizes one request containing the verified public citations. The controller pins the private board and recipient and holds the request when verification changes.
5. The recipient checks Crosswalk when its session runs. Reconcile the reply using the Work Board. An authenticated reply receipt and rendering the full review body are separate capabilities.

## Local checks

Run fixture tests before any installed acceptance. These commands use the declared toolchains.

```powershell
dotnet restore tests/MuseMonitor.Tests.csproj --locked-mode
dotnet run --project tests/MuseMonitor.Tests.csproj -c Release --no-restore
dotnet build windows/MuseMonitor.csproj -c Release
```

```bash
PYTHONPATH=linux python3 -m unittest discover -s linux/tests
node --test extension/tests/*.test.mjs
```

The Linux controller uses platform-specific primitives. The optional native D-Bus fixture needs system PyGObject. The Chrome bridge tests can run with Node without a browser session.

## Runtime dependencies

The production Linux installer is retained for source review. It assumes the original desktop layout, trusted local pairing, system services, managed browser sessions, and externally configured integrations. The public snapshot substitutes documentation-range addresses, synthetic identifiers, and example key pins. Run the fixture tests for research. A real deployment would need its own environment configuration and attended provider authorization.
