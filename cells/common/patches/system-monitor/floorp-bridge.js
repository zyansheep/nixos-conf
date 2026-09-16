// Runs from packaged, privileged AutoConfig after the browser finishes startup.
(() => {
  const { classes: Cc, interfaces: Ci, utils: Cu } = Components;
  Cu.importGlobalProperties(["IOUtils", "PathUtils", "TextDecoder"]);
  const runtime = Services.env.get("XDG_RUNTIME_DIR");
  if (!runtime) return;
  const directory = PathUtils.join(runtime, "floorp-monitor");
  const path = PathUtils.join(directory, `${Services.appinfo.processID}.json`);
  let busy = false;
  const clean = value => String(value || "").replace(/[\x00-\x1f\x7f]/g, " ").slice(0, 160);
  async function sample() {
    if (busy) return;
    busy = true;
    try {
      const info = await ChromeUtils.requestProcInfo();
      const processes = [];
      for (const child of info.children) {
        // Origin attributes include privateBrowsingId. Do not export private sites.
        if (!child.origin || /(?:\^|&)privateBrowsingId=[1-9]/.test(child.origin)) continue;
        if (!["webIsolated", "web", "withCoopCoep", "webServiceWorker"].includes(child.type)) continue;
        let stat;
        // procfs reports a zero file size, so request a bounded byte read explicitly.
        try {
          stat = new TextDecoder().decode(await IOUtils.read(`/proc/${child.pid}/stat`, { maxBytes: 4096 }));
        } catch { continue; }
        const start = Number(stat.slice(stat.lastIndexOf(")") + 2).split(/\s+/)[19]);
        if (!Number.isSafeInteger(start)) continue;
        const titles = [...new Set((child.windows || [])
          .filter(win => win.isProcessRoot && win.documentTitle)
          .map(win => clean(win.documentTitle)))];
        processes.push({ pid: child.pid, start,
          origin: clean(child.origin.split("^")[0]), titles: titles.slice(0, 3) });
      }
      await IOUtils.makeDirectory(directory, { permissions: 0o700, ignoreExisting: true });
      const temporary = `${path}.tmp`;
      await IOUtils.writeUTF8(temporary, JSON.stringify({ timestamp: Date.now() / 1000, processes }));
      await IOUtils.setPermissions(temporary, 0o600);
      await IOUtils.move(temporary, path);
    } catch (error) {
      // Failure leaves a stale snapshot, which the collector ignores automatically.
      Cu.reportError(`Floorp process bridge: ${error}`);
    } finally { busy = false; }
  }
  const timer = Cc["@mozilla.org/timer;1"].createInstance(Ci.nsITimer);
  const start = () => {
    sample();
    timer.initWithCallback(sample, 5000, Ci.nsITimer.TYPE_REPEATING_SLACK);
  };
  Services.obs.addObserver(function ready() {
    Services.obs.removeObserver(ready, "final-ui-startup");
    start();
  }, "final-ui-startup");
  Services.obs.addObserver(() => { timer.cancel(); }, "quit-application");
})();
