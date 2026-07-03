"""Built-in pairing page served on GET / — lets a novice point this host at a
LAMA CARAVAN controller from a browser instead of editing config.json."""
from __future__ import annotations

PAIR_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caravan Scout</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; background: #12141a; color: #e6e6ea;
         font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
         display: flex; align-items: center; justify-content: center; padding: 24px; }
  .wrap { width: 100%; max-width: 640px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h1 .llama { margin-right: 8px; }
  .sub { color: #9a9aa5; margin: 0 0 20px; font-size: 13px; }
  .card { background: #1a1d26; border: 1px solid #2a2e3b; border-radius: 12px;
          padding: 16px 18px; margin-bottom: 14px; }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
             color: #8b8b96; margin: 0 0 10px; }
  .row { display: flex; justify-content: space-between; gap: 12px; padding: 3px 0;
         font-size: 14px; }
  .row .k { color: #9a9aa5; }
  .row .v { text-align: right; word-break: break-all; }
  .pill { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12px; }
  .pill.ok    { background: #133b26; color: #58d68d; }
  .pill.err   { background: #3b1616; color: #ec7063; }
  .pill.off   { background: #2a2e3b; color: #9a9aa5; }
  form { display: flex; gap: 8px; margin-top: 6px; }
  input[type=url], input[type=text] {
    flex: 1; background: #12141a; color: #e6e6ea; border: 1px solid #2a2e3b;
    border-radius: 8px; padding: 9px 12px; font-size: 14px; }
  input:focus { outline: none; border-color: #4a7dbd; }
  button { background: #2e6bb0; color: #fff; border: 0; border-radius: 8px;
           padding: 9px 16px; font-size: 14px; cursor: pointer; }
  button:hover { background: #3a7cc4; }
  button:disabled { opacity: .5; cursor: default; }
  .hint { color: #8b8b96; font-size: 12.5px; margin: 8px 0 0; }
  .msg { margin-top: 10px; font-size: 13.5px; display: none; }
  .msg.show { display: block; }
  .msg.ok { color: #58d68d; }
  .msg.err { color: #ec7063; }
  a { color: #6db3f2; }
  .foot { color: #6f6f7a; font-size: 12px; margin-top: 16px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="llama">&#129433;</span>Caravan Scout</h1>
  <p class="sub">This machine is ready to join a <a
     href="https://github.com/thepr0metheus/lama-caravan" target="_blank"
     rel="noopener">LAMA CARAVAN</a> fleet.</p>

  <div class="card">
    <h2>This host</h2>
    <div class="row"><span class="k">Host ID</span><span class="v" id="hostId">…</span></div>
    <div class="row"><span class="k">Hostname / IP</span><span class="v" id="hostAddr">…</span></div>
    <div class="row"><span class="k">Platform</span><span class="v" id="platform">…</span></div>
    <div class="row"><span class="k">GPUs</span><span class="v" id="gpus">…</span></div>
    <div class="row"><span class="k">Local agents detected</span><span class="v" id="agents">…</span></div>
    <div class="row"><span class="k">llama server cells</span><span class="v" id="cells">…</span></div>
  </div>

  <div class="card">
    <h2>Controller</h2>
    <div class="row"><span class="k">Paired with</span><span class="v" id="controller">—</span></div>
    <div class="row"><span class="k">Heartbeat</span><span class="v" id="hb"><span class="pill off">not configured</span></span></div>
    <form id="pairForm">
      <input type="text" id="urlInput" placeholder="http://controller-ip:8090"
             autocomplete="off" spellcheck="false">
      <button type="submit" id="pairBtn">Pair</button>
    </form>
    <p class="hint">Enter the address of the machine running the LAMA CARAVAN
      admin (default port <b>8090</b>). This host will start sending heartbeats
      and appear on its topology board within a minute.</p>
    <p class="msg" id="msg"></p>
  </div>

  <p class="foot">caravan-scout · HTTP API on this port — see
    <a href="https://github.com/thepr0metheus/caravan-scout" target="_blank"
       rel="noopener">docs</a></p>
</div>
<script>
(function () {
  var boardUrl = "";
  function $(id) { return document.getElementById(id); }
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }

  function render(st) {
    var host = st.host || {};
    $("hostId").textContent = host.id || "?";
    $("hostAddr").textContent = (host.hostname || "?") + " / " + (host.ip || "?");
    $("platform").textContent = st.platform || "?";
    var gpus = st.gpus || [];
    $("gpus").textContent = gpus.length
      ? gpus.map(function (g) { return g.name || g.model || "GPU"; }).join(", ")
      : "none (CPU host)";
    var agents = st.agents || [];
    $("agents").textContent = agents.length
      ? agents.length + " (" + agents.slice(0, 4).map(function (a) { return a.name || a.id; }).join(", ")
        + (agents.length > 4 ? ", …" : "") + ")"
      : "none";
    var nodes = st.llamaNodes || [];
    var running = nodes.filter(function (n) { return n.running; }).length;
    $("cells").textContent = nodes.length ? running + " running / " + nodes.length : "none";

    var ctl = st.controllerUrl || "";
    boardUrl = ctl;
    $("controller").innerHTML = ctl
      ? '<a href="' + esc(ctl) + '" target="_blank" rel="noopener">' + esc(ctl) + "</a>"
      : "—";
    if (!$("urlInput").value && ctl) $("urlInput").placeholder = ctl;

    var hb = st.heartbeat || {};
    var el = $("hb");
    if (!ctl) {
      el.innerHTML = '<span class="pill off">not configured</span>';
    } else if (hb.state === "ok") {
      var when = hb.lastAt ? new Date(hb.lastAt * 1000).toLocaleTimeString() : "";
      el.innerHTML = '<span class="pill ok">ok' + (when ? " · " + when : "") + "</span>";
    } else if (hb.state === "error") {
      el.innerHTML = '<span class="pill err" title="' + esc(hb.error || "") + '">error</span> '
        + '<span style="color:#9a9aa5;font-size:12.5px">' + esc((hb.error || "").slice(0, 80)) + "</span>";
    } else {
      el.innerHTML = '<span class="pill off">waiting…</span>';
    }
  }

  function refresh() {
    fetch("/api/state").then(function (r) { return r.json(); }).then(render).catch(function () {});
  }

  $("pairForm").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var url = $("urlInput").value.trim();
    var msg = $("msg");
    if (!url) { return; }
    $("pairBtn").disabled = true;
    msg.className = "msg";
    fetch("/api/controller-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url })
    }).then(function (r) { return r.json(); }).then(function (res) {
      $("pairBtn").disabled = false;
      if (res.ok) {
        var hb = res.heartbeat || {};
        if (hb.state === "ok") {
          msg.className = "msg show ok";
          msg.innerHTML = "Paired! This host should now be visible on the "
            + '<a href="' + esc(res.controllerUrl) + '" target="_blank" rel="noopener">topology board</a>.';
        } else {
          msg.className = "msg show err";
          msg.textContent = "Saved, but the controller did not answer: "
            + (hb.error || "unknown error") + " — check the address and that the admin is running.";
        }
      } else {
        msg.className = "msg show err";
        msg.textContent = res.error || "failed";
      }
      refresh();
    }).catch(function (e) {
      $("pairBtn").disabled = false;
      msg.className = "msg show err";
      msg.textContent = String(e);
    });
  });

  refresh();
  setInterval(refresh, 3000);
})();
</script>
</body>
</html>
"""


def pair_page_bytes() -> bytes:
    return PAIR_PAGE.encode("utf-8")
