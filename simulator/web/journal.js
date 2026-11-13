/* Journal page (guide step 5.6): statistics, equity curve, splits, and the
 * trade table with tags, notes, chart snapshots and a link back into the
 * chart at each trade. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var sessions = [];
  var current = "all";

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function usd(v) { return v == null ? "-" : (v < 0 ? "-" : "") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function signed(v, d) { return v == null ? "-" : (v > 0 ? "+" : v < 0 ? "-" : "") + Math.abs(v).toFixed(d); }
  function pct(v) { return v == null ? "-" : (100 * v).toFixed(1) + "%"; }
  function cls(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
  function when(ms) { return new Date(ms).toISOString().slice(0, 16).replace("T", " "); }
  function dur(sec) {
    if (sec == null) return "-";
    var m = Math.round(sec / 60);
    if (m < 60) return m + "m";
    var h = Math.floor(m / 60);
    return h < 48 ? h + "h " + (m % 60) + "m" : Math.round(h / 24) + "d";
  }
  function toast(text, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    el.textContent = text;
    $("toasts").appendChild(el);
    setTimeout(function () { el.remove(); }, 3000);
  }
  function json(url, opts) { return fetch(url, opts).then(function (r) { if (!r.ok) throw new Error(r.statusText); return r.json(); }); }

  // ---- sessions ----

  function loadSessions() {
    return json("/api/sessions").then(function (rows) {
      sessions = rows;
      var opts = ["<option value='all'>All sessions (" + rows.length + ")</option>"].concat(rows.map(function (s) {
        return "<option value='" + s.id + "'>" + esc(s.name || "Session " + s.id) + " · " + when(s.start_ms) + " · " +
          s.trades + " trades" + (s.status === "ended" ? " · ended" : "") + "</option>";
      }));
      $("sessionSel").innerHTML = opts.join("");
      var want = new URLSearchParams(location.search).get("session");
      if (want && rows.some(function (s) { return s.id === want; })) current = want;
      $("sessionSel").value = current;
    });
  }

  function selected() { return sessions.filter(function (s) { return s.id === current; })[0] || null; }

  $("sessionSel").onchange = function () {
    current = this.value;
    history.replaceState(null, "", current === "all" ? "/journal" : "/journal?session=" + current);
    render();
  };
  $("renameBtn").onclick = function () {
    var s = selected();
    if (!s) return;
    var name = prompt("Session name", s.name || "");
    if (name === null) return;
    json("/api/sessions/" + s.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name }) })
      .then(loadSessions).then(render);
  };
  $("deleteBtn").onclick = function () {
    var s = selected();
    if (!s || !confirm("Delete this session and all its trades, notes, drawings and snapshots? This cannot be undone.")) return;
    json("/api/sessions/" + s.id, { method: "DELETE" }).then(function () { current = "all"; return loadSessions(); }).then(render);
  };
  $("resumeBtn").onclick = function () {
    var s = selected();
    if (s) location.href = "/?session=" + s.id;
  };

  // ---- render ----

  function card(k, v, s, extra) {
    return "<div class='card'><div class='k'>" + k + "</div><div class='v " + (extra || "") + "'>" + v + "</div>" +
      (s ? "<div class='s'>" + s + "</div>" : "") + "</div>";
  }

  function splitTable(rows) {
    if (!rows.length) return "<div class='empty'>Nothing yet.</div>";
    return "<table class='t'><thead><tr><th></th><th class='n'>Trades</th><th class='n'>Win rate</th><th class='n'>95% range</th>" +
      "<th class='n'>Exp. R</th><th class='n'>Net USD</th></tr></thead><tbody>" + rows.map(function (r) {
        var ci = r.win_rate_ci ? (100 * r.win_rate_ci[0]).toFixed(0) + "-" + (100 * r.win_rate_ci[1]).toFixed(0) + "%" : "-";
        return "<tr><td>" + esc(r.label) + "</td><td class='n'>" + r.trades + "</td><td class='n'>" + pct(r.win_rate) +
          "</td><td class='n faint'>" + ci + "</td><td class='n'>" + signed(r.expectancy_r, 2) +
          "</td><td class='n " + cls(r.net_usd) + "'>" + usd(r.net_usd) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function curveSvg(points, kind) {
    if (points.length < 2) return "<div class='empty'>The curve appears after the first closed trade.</div>";
    var cs = getComputedStyle(document.documentElement);
    var line = cs.getPropertyValue("--accent").trim(), grid = cs.getPropertyValue("--border").trim(), muted = cs.getPropertyValue("--muted").trim();
    var W = 1000, H = 220, L = 70, R = 10, T = 10, B = 24;
    var ys = points.map(function (p) { return p.equity; });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    if (hi === lo) { hi += 1; lo -= 1; }
    var n = points.length - 1;
    function X(i) { return L + (W - L - R) * i / n; }
    function Y(v) { return T + (H - T - B) * (1 - (v - lo) / (hi - lo)); }
    var d = points.map(function (p, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(p.equity).toFixed(1); }).join(" ");
    var base0 = points[0].equity;
    var marks = [lo, hi];
    if (Math.abs(Y(base0) - Y(lo)) > 14 && Math.abs(Y(base0) - Y(hi)) > 14) marks.push(base0);
    var ticks = marks.map(function (v) {
      return "<line x1='" + L + "' x2='" + (W - R) + "' y1='" + Y(v) + "' y2='" + Y(v) + "' stroke='" + grid + "'/>" +
        "<text x='" + (L - 8) + "' y='" + (Y(v) + 4) + "' text-anchor='end' font-size='11' fill='" + muted + "'>" + usd(v) + "</text>";
    }).join("");
    var base = points[0].equity;
    var baseLine = "<line x1='" + L + "' x2='" + (W - R) + "' y1='" + Y(base) + "' y2='" + Y(base) + "' stroke='" + muted + "' stroke-dasharray='4 4'/>";
    var last = points[points.length - 1];
    return "<svg class='curve' viewBox='0 0 " + W + " " + H + "' preserveAspectRatio='none' role='img' aria-label='" + kind + " after each trade'>" +
      ticks + baseLine + "<path d='" + d + "' fill='none' stroke='" + line + "' stroke-width='2' vector-effect='non-scaling-stroke'/>" +
      "<circle cx='" + X(n) + "' cy='" + Y(last.equity) + "' r='3.5' fill='" + line + "'/>" +
      "<text x='" + (W - R) + "' y='" + (H - 6) + "' text-anchor='end' font-size='11' fill='" + muted + "'>trade " + n + "</text>" +
      "<text x='" + L + "' y='" + (H - 6) + "' font-size='11' fill='" + muted + "'>start</text></svg>";
  }

  function render() {
    var s = selected();
    ["renameBtn", "deleteBtn"].forEach(function (id) { $(id).classList.toggle("hidden", !s); });
    $("resumeBtn").classList.toggle("hidden", !s || s.status !== "active");
    $("sessionMeta").textContent = s ? ("Balance " + usd(s.starting_balance) + " USD · leverage " + s.leverage + ":1 · commission " +
      s.commission_per_lot + " USD/lot · slippage " + s.slippage_pips + " pips · " + (s.status === "active" ? "in play at " + when(s.sim_ms) + " UTC" : "ended")) :
      "All sessions pooled. Results state their own assumptions per session; pick one to see them.";

    var q = current === "all" ? "" : "?session=" + current;
    Promise.all([json("/api/stats" + q), json("/api/trades" + q)]).then(function (res) {
      var st = res[0], trades = res[1];
      var ci = st.win_rate_ci ? (100 * st.win_rate_ci[0]).toFixed(0) + "% to " + (100 * st.win_rate_ci[1]).toFixed(0) + "%" : "";
      var small = st.small_sample && st.trades ? "<span class='warnline'>Under 30 trades: mostly noise</span>" : "";
      $("cards").innerHTML = [
        card("Trades", st.trades, st.wins + " won · " + st.losses + " lost" + (st.breakeven ? " · " + st.breakeven + " even" : "")),
        card("Win rate", pct(st.win_rate), (ci ? "95% range " + ci : "") + (small ? "<br>" + small : "")),
        card("Expectancy", st.expectancy_r == null ? "-" : signed(st.expectancy_r, 2) + "R", st.trades_with_r + " trades had a stop, so an R"),
        card("Avg win / loss", (st.avg_win_r == null ? "-" : signed(st.avg_win_r, 2)) + " / " + (st.avg_loss_r == null ? "-" : signed(st.avg_loss_r, 2)), "in R"),
        card("Profit factor", st.profit_factor == null ? "-" : st.profit_factor.toFixed(2), "gross won / gross lost"),
        card("Net", usd(st.net_usd), "USD after commission", cls(st.net_usd)),
        card("Max drawdown", usd(st.max_drawdown_usd), st.max_drawdown_pct == null ? "on closed trades" : st.max_drawdown_pct.toFixed(1) + "% from peak"),
        card("Avg hold", dur(st.avg_hold_seconds), "entry to exit, market time")
      ].join("");
      $("curveTitle").textContent = (st.curve_kind === "equity" ? "Account equity" : "Cumulative profit") + " after each closed trade";
      $("curve").innerHTML = curveSvg(st.equity_curve, st.curve_kind);
      $("bySession").innerHTML = splitTable(st.by_session);
      $("byWeekday").innerHTML = splitTable(st.by_weekday);
      $("byTag").innerHTML = splitTable(st.by_tag);
      $("byExit").innerHTML = splitTable(st.by_exit);
      renderTrades(trades);
      var notes = st.notes || [];
      $("notesBox").classList.toggle("hidden", !notes.length);
      $("notes").innerHTML = notes.map(function (n) {
        return "<p><span class='mono faint'>" + when(n.sim_ms) + "</span> " + esc(n.body) + "</p>";
      }).join("");
    }).catch(function (e) { toast(String(e), "err"); });
  }

  function renderTrades(trades) {
    if (!trades.length) { $("trades").innerHTML = "<div class='empty'>No closed trades yet. Trades land here the moment they close.</div>"; return; }
    var rows = trades.slice().reverse().map(function (t) {
      var snap = function (kind, has) {
        if (!has) return "<span class='faint'>-</span>";
        var src = "/api/snapshot/" + t.session_id + "/" + t.position_id + "/" + kind + ".png";
        return "<a href='" + src + "' target='_blank' title='" + kind + " snapshot'><img class='thumb' loading='lazy' src='" + src + "' alt='" + kind + "'></a>";
      };
      return "<tr>" +
        "<td class='mono'>" + (current === "all" ? "<span class='faint'>" + esc(t.session_name || t.session_id) + "</span><br>" : "") + "#" + t.id + "</td>" +
        "<td>" + t.side.toUpperCase() + "</td><td class='n'>" + t.lots.toFixed(2) + "</td>" +
        "<td class='mono'>" + when(t.entry_ms) + "<br>" + t.entry_price.toFixed(5) + "</td>" +
        "<td class='mono'>" + when(t.exit_ms) + "<br>" + t.exit_price.toFixed(5) + "</td>" +
        "<td>" + t.exit_reason.toUpperCase() + "</td>" +
        "<td class='n'>" + signed(t.pips, 1) + "</td>" +
        "<td class='n " + cls(t.net_usd) + "'>" + usd(t.net_usd) + "</td>" +
        "<td class='n'>" + (t.r_multiple == null ? "-" : signed(t.r_multiple, 2)) + "</td>" +
        "<td><input class='tags' data-s='" + t.session_id + "' data-t='" + t.id + "' data-f='tags' value='" + esc(t.tags) + "'></td>" +
        "<td><textarea class='note' data-s='" + t.session_id + "' data-t='" + t.id + "' data-f='note'>" + esc(t.note) + "</textarea></td>" +
        "<td>" + snap("entry", t.has_entry_png) + " " + snap("exit", t.has_exit_png) + "</td>" +
        "<td><a href='/?review=" + t.session_id + ":" + t.id + "'>Review</a></td></tr>";
    });
    $("trades").innerHTML = "<table class='t'><thead><tr><th>Trade</th><th>Side</th><th class='n'>Lots</th><th>Entry (UTC)</th><th>Exit (UTC)</th>" +
      "<th>Exit</th><th class='n'>Pips</th><th class='n'>Net USD</th><th class='n'>R</th><th>Tags</th><th>Note</th><th>Chart</th><th></th></tr></thead><tbody>" +
      rows.join("") + "</tbody></table>";
  }

  $("trades").addEventListener("change", function (e) {
    var el = e.target.closest("[data-f]");
    if (!el) return;
    var body = {};
    body[el.dataset.f] = el.value;
    json("/api/trades/" + el.dataset.s + "/" + el.dataset.t, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function () { toast("Saved."); if (el.dataset.f === "tags") render(); })
      .catch(function (err) { toast(String(err), "err"); });
  });

  $("themeBtn").onclick = function () {
    var light = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = light ? "light" : "dark";
    try { localStorage.setItem("sim-theme", light ? "light" : "dark"); } catch (e) { /* ignore */ }
    render();
  };

  loadSessions().then(render);
})();
