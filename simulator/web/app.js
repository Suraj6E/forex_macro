/* Replay page: WebSocket client, controls, order ticket, review mode.
 * The server owns the clock, candles and broker; this page only draws what
 * it is sent and sends back what the trader asks for. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var PIP = 0.0001, DIGITS = 5, INST = null;  // set from the session's instrument
  var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var TAGS = ["breakout", "pullback", "reversal", "range", "news", "plan", "fomo", "revenge"];
  var LS_SESSION = "sim-session";

  var S = {
    ws: null, session: null, status: null, quote: null, book: null,
    timeframes: [], tfSeconds: 900, chartTf: null,
    side: "buy", otype: "market", sizeMode: "lots",
    trades: [], lastTrade: null, loadingOlder: false, olderDone: false,
    review: null, reconnect: true, attached: false
  };

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function fmtClock(ms) {
    var d = new Date(ms);
    var t = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
    if (S.session && S.session.hide_dates) return DAYS[d.getUTCDay()] + " " + t;
    return DAYS[d.getUTCDay()] + " " + d.toISOString().slice(0, 10) + " " + t + " UTC";
  }
  function fmtTime(ms) {
    var d = new Date(ms);
    var hm = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
    if (S.session && S.session.hide_dates) return DAYS[d.getUTCDay()] + " " + hm;
    return d.toISOString().slice(0, 10) + " " + hm;
  }
  function px(p) { return p === null || p === undefined ? "-" : Number(p).toFixed(DIGITS); }
  function num(v) { return Number(v).toLocaleString("en-US"); }
  function usd(v) { return (v < 0 ? "-" : "") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function signed(v, digits) { return (v > 0 ? "+" : v < 0 ? "-" : "") + Math.abs(v).toFixed(digits); }
  function cls(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function isoMinute(ms) { return new Date(ms).toISOString().slice(0, 16); }

  function toast(text, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    el.textContent = text;
    $("toasts").appendChild(el);
    setTimeout(function () { el.remove(); }, kind === "err" ? 7000 : 3500);
    while ($("toasts").children.length > 5) $("toasts").firstChild.remove();
  }

  // ---------------------------------------------------------------- chart

  var C = window.SimChart.create($("chart"), {});
  var D = window.SimDrawings.create(C, {
    getTimeframe: function () { return S.chartTf || "m15"; },
    getTimeframeSeconds: function () { return S.tfSeconds; },
    onSave: function (d) { send({ type: "drawing_save", drawing: d }); },
    onDelete: function (id) { send({ type: "drawing_delete", id: id }); },
    onSelect: showDrawbar,
    onPaint: showDrawbar,
    onTool: function (tool) {
      document.querySelectorAll(".tools [data-tool]").forEach(function (b) {
        b.classList.toggle("on", (b.dataset.tool || null) === (tool || null));
      });
    }
  });

  var N = window.SimNews.create(C, {
    getSession: function () { return S.session; },
    getClockMs: function () { return S.status ? S.status.sim_ms : (S.review && C.lastTime() ? (C.lastTime() + S.tfSeconds) * 1000 : null); },
    getTfSeconds: function () { return S.tfSeconds; },
    fmtTime: function (ms) { return fmtTime(ms); },
    hideDates: function () { return !!(S.session && S.session.hide_dates); }
  });

  C.chart.subscribeCrosshairMove(function (param) {
    var bar = param && param.seriesData ? param.seriesData.get(C.series) : null;
    var vol = param && param.seriesData ? param.seriesData.get(C.volume) : null;
    showOhlc(bar, vol ? vol.value : null);
  });
  var lastBar = null;
  function showOhlc(bar, vol) {
    if (!bar) { bar = lastBar; vol = lastBar ? lastBar.volume : null; }
    if (!bar) { $("ohlc").textContent = ""; return; }
    $("ohlc").innerHTML = "O <span class='mono'>" + px(bar.open) + "</span> H <span class='mono'>" + px(bar.high) +
      "</span> L <span class='mono'>" + px(bar.low) + "</span> C <span class='mono'>" + px(bar.close) + "</span>" +
      (vol !== null && vol !== undefined ? " V <span class='mono' title='Tick volume: quotes in the candle'>" + num(vol) + "</span>" : "");
  }

  function setInstrument(inst) {
    INST = inst;
    PIP = inst.pip; DIGITS = inst.digits;
    C.setInstrument(inst);
    D.setInstrument(inst);
    $("symbolLabel").textContent = inst.label;
    $("symLabel").textContent = inst.label;
    document.title = inst.label + " · FX Replay";
    var step = Math.pow(10, -DIGITS).toFixed(DIGITS);
    ["priceIn", "slIn", "tpIn"].forEach(function (id) { $(id).step = step; });
  }

  C.onRangeChange(function (range) {
    if (!range || S.review || S.loadingOlder || S.olderDone || !S.attached) return;
    if (range.from < 30 && C.firstTime() !== null) {
      S.loadingOlder = true;
      send({ type: "more_history", before: C.firstTime(), timeframe: S.chartTf });
    }
  });

  // ---------------------------------------------------------------- socket

  function connect() {
    var ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    S.ws = ws;
    ws.onopen = function () {
      var params = new URLSearchParams(location.search);
      var pendingStart = sessionStorage.getItem("sim-new-start");
      if (pendingStart) {
        sessionStorage.removeItem("sim-new-start");
        openSessionDialog({ start: pendingStart });
        return;
      }
      var sid = params.get("session") || localStorage.getItem(LS_SESSION);
      send({ type: "hello", session: sid });
    };
    ws.onmessage = function (e) { onMessage(JSON.parse(e.data)); };
    ws.onclose = function () {
      S.attached = false;
      if (S.reconnect) setTimeout(connect, 1200);
    };
  }

  function send(msg) {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(msg));
  }

  function onMessage(m) {
    switch (m.type) {
      case "preparing":
        // The server is still building the market data (first start), or
        // cannot. Show its progress and reconnect once it is ready.
        S.reconnect = false;
        openSessionDialog({ preparing: true });
        waitUntilReady();
        break;
      case "no_session":
        localStorage.removeItem(LS_SESSION);
        openSessionDialog({ meta: m.meta });
        break;
      case "session":
        S.session = m.session;
        S.attached = true;
        setInstrument(m.session.instrument);
        N.clear();
        S.timeframes = m.session.timeframes;
        localStorage.setItem(LS_SESSION, m.session.id);
        if (new URLSearchParams(location.search).get("session")) history.replaceState(null, "", "/");
        C.setHideDates(m.session.hide_dates);
        document.body.classList.toggle("live-session", m.session.mode === "live");
        buildTimeframes(m.session.timeframes, function (tf) { send({ type: "timeframe", timeframe: tf }); });
        buildSpeeds(m.session.speeds);
        $("sessionInfo").textContent = (m.session.mode === "live" ? "Live · " : "Replay · ") + m.session.feed_label + " · " +
          (m.session.name ? m.session.name + " · " : "") + "started " + fmtTime(m.session.start_ms) + " · id " + m.session.id;
        if ($("sessionDlg").open) $("sessionDlg").close();
        loadTrades();
        break;
      case "history":
        S.status = m.status;
        setTimeframe(m.status.timeframe);
        C.setHistory(m.bars, S.tfSeconds);
        lastBar = m.bars.length ? m.bars[m.bars.length - 1] : null;
        showOhlc(null);
        S.olderDone = false; S.loadingOlder = false;
        C.scrollToEnd();
        D.setAll(m.drawings);
        onQuote(m.quote);
        onBook(m.book);
        onStatus(m.status);
        refreshMarkers(true);
        N.reload();
        break;
      case "frame":
        onStatus(m.status);
        if (m.status.timeframe === S.chartTf && m.bars.length) {
          C.updateBars(m.bars);
          lastBar = m.bars[m.bars.length - 1];
          showOhlc(null);
        }
        onQuote(m.quote);
        onBook(m.book);
        break;
      case "older":
        S.loadingOlder = false;
        if (m.timeframe !== S.chartTf) break;
        if (!C.prependBars(m.bars)) S.olderDone = true;
        refreshMarkers(true);
        N.reload();
        break;
      case "events":
        onEvents(m.events);
        onBook(m.book);
        break;
      case "status":
        onStatus(m.status);
        if (m.message) toast(m.message);
        break;
      case "error":
        toast(m.message, "err");
        if (S.book) renderLines(S.book); // snap dragged lines back
        break;
      case "taken_over":
        S.reconnect = false;
        S.attached = false;
        toast(m.message + " Reload this page to take it back.", "err");
        break;
      case "ended":
        localStorage.removeItem(LS_SESSION);
        S.attached = false;
        toast("Session ended. Its trades are in the journal.");
        N.clear();
        openSessionDialog({});
        break;
      case "drawing_saved": break;
      case "annotated": toast("Saved to the journal."); break;
      case "noted": toast("Note added."); $("sessionNote").value = ""; break;
    }
  }

  // ---------------------------------------------------------------- status / quote / book

  function onStatus(st) {
    S.status = st;
    if (st.live) $("clock").innerHTML = "<span class='livetag'>LIVE</span>" + esc(fmtClock(st.sim_ms));
    else $("clock").textContent = fmtClock(st.sim_ms) + (st.finished ? "  (end of data)" : "");
    $("clock").classList.toggle("playing", st.playing);
    $("playBtn").textContent = st.playing ? "Pause" : "Play";
    $("playBtn").disabled = st.finished;
    if ($("speedSel").value !== st.speed) $("speedSel").value = st.speed;
    N.onClock(st.sim_ms);
  }

  function onQuote(q) {
    if (!q) return;
    S.quote = q;
    C.setQuote(q);
    $("bidPx").textContent = px(q.bid);
    $("askPx").textContent = px(q.ask);
    $("spreadPx").textContent = ((q.ask - q.bid) / PIP).toFixed(1);
    lotInfo();
    updateTicket();
  }

  // What a lot is and what a pip is worth: they differ a lot between
  // EUR/USD, a yen cross, silver and oil.
  function lotInfo() {
    if (!INST) return;
    var a = S.book && S.book.account;
    var pv = a && a.pip_value ? " · pip " + a.pip_value.toFixed(2) + " USD/lot" : "";
    $("lotInfo").textContent = "1 lot = " + num(INST.contract) + " " + INST.unit + pv;
    $("lotInfo").title = "One pip is " + INST.pip + " in price. Pip value in USD at the current price.";
  }

  function onBook(book) {
    if (!book) return;
    S.book = book;
    var a = book.account;
    var rows = [
      ["Balance", usd(a.balance)],
      ["Equity", usd(a.equity)],
      ["Open P/L", "<span class='" + cls(a.unrealized) + "'>" + usd(a.unrealized) + "</span>"],
      ["Used margin", usd(a.used_margin)],
      ["Free margin", usd(a.free_margin)],
      ["Margin level", a.margin_level === null ? "-" : a.margin_level.toFixed(0) + "%"],
      ["Leverage", a.leverage + ":1"],
      ["Costs", a.commission_per_lot + " USD/lot · " + a.slippage_pips + " pip slip"]
    ];
    $("account").innerHTML = rows.map(function (r) { return "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>"; }).join("");
    lotInfo();

    $("posCount").textContent = book.positions.length || "";
    $("positions").innerHTML = book.positions.length ? book.positions.map(function (p) {
      return "<div class='item'><div class='what'>#" + p.id + " " + p.side.toUpperCase() + " " + p.lots.toFixed(2) +
        " @ <span class='mono'>" + px(p.entry_price) + "</span></div>" +
        "<div class='pnl " + cls(p.pnl) + "'>" + usd(p.pnl) + "<br><span class='tiny'>" + signed(p.pips, 1) + " pips</span></div>" +
        "<div class='meta'>SL " + px(p.sl) + " · TP " + px(p.tp) + " · " + fmtTime(p.entry_ms) + "</div>" +
        "<div class='meta' style='display:flex;justify-content:flex-end'>" +
        (p.closing ? "<span class='faint'>closing on next tick</span>" : "<button data-close='" + p.id + "'>Close</button>") + "</div></div>";
    }).join("") : "<div class='empty'>No open positions.</div>";

    $("ordCount").textContent = book.pending.length || "";
    $("pending").innerHTML = book.pending.length ? book.pending.map(function (o) {
      var what = o.side.toUpperCase() + " " + (o.type === "market" ? "MARKET" : o.type.toUpperCase()) + " " + o.lots.toFixed(2);
      return "<div class='item'><div class='what'>#" + o.id + " " + what + (o.price ? " @ <span class='mono'>" + px(o.price) + "</span>" : "") + "</div>" +
        "<div><button data-cancel='" + o.id + "'>Cancel</button></div>" +
        "<div class='meta'>SL " + px(o.sl) + " · TP " + px(o.tp) + (o.type === "market" ? " · fills on the next tick" : "") + "</div></div>";
    }).join("") : "<div class='empty'>No pending orders.</div>";

    renderLines(book);
    refreshMarkers();
    updateTicket();
  }

  function renderLines(book) {
    var T = C.tokens();
    var specs = [];
    function mod(target, id, field) {
      return function (price) {
        var m = { type: "modify", target: target, id: id };
        m[field] = price;
        send(m);
      };
    }
    book.pending.forEach(function (o) {
      var color = o.side === "buy" ? T.up : T.down;
      if (o.price) specs.push({ id: "o" + o.id, price: o.price, color: color, style: "dashed", draggable: true,
        title: "#" + o.id + " " + o.side.toUpperCase() + " " + o.type.toUpperCase() + " " + o.lots.toFixed(2), onDrop: mod("order", o.id, "price") });
      if (o.sl) specs.push({ id: "osl" + o.id, price: o.sl, color: T.down, style: "dotted", draggable: true, title: "SL #" + o.id, onDrop: mod("order", o.id, "sl") });
      if (o.tp) specs.push({ id: "otp" + o.id, price: o.tp, color: T.up, style: "dotted", draggable: true, title: "TP #" + o.id, onDrop: mod("order", o.id, "tp") });
    });
    book.positions.forEach(function (p) {
      specs.push({ id: "p" + p.id, price: p.entry_price, color: T.accent, style: "solid",
        title: "#" + p.id + " " + p.side.toUpperCase() + " " + p.lots.toFixed(2) + "  " + usd(p.pnl) });
      if (p.sl) specs.push({ id: "psl" + p.id, price: p.sl, color: T.down, style: "dashed", width: 1, draggable: true, title: "SL #" + p.id, onDrop: mod("position", p.id, "sl") });
      if (p.tp) specs.push({ id: "ptp" + p.id, price: p.tp, color: T.up, style: "dashed", width: 1, draggable: true, title: "TP #" + p.id, onDrop: mod("position", p.id, "tp") });
    });
    C.setLines(specs);
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-close],[data-cancel]");
    if (!t) return;
    if (t.dataset.close) send({ type: "close", id: +t.dataset.close });
    if (t.dataset.cancel) send({ type: "cancel", id: +t.dataset.cancel });
  });

  // ---------------------------------------------------------------- events, snapshots, last trade

  function snapshot(positionId, kind) {
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        try { send({ type: "snapshot", position_id: positionId, kind: kind, png: C.screenshot() }); } catch (e) { /* ignore */ }
      });
    });
  }

  function onEvents(events) {
    events.forEach(function (e) {
      var o = e.order;
      if (e.kind === "placed") {
        toast("Order #" + o.id + ": " + o.side.toUpperCase() + " " + (o.type === "market" ? "" : o.type.toUpperCase() + " ") +
          o.lots.toFixed(2) + (o.price ? " at " + px(o.price) : " at market, next tick"));
      } else if (e.kind === "filled") {
        toast("Filled #" + o.id + ": " + o.side.toUpperCase() + " " + o.lots.toFixed(2) + " at " + px(o.fill_price));
        snapshot(e.position_id, "entry");
      } else if (e.kind === "rejected") {
        toast("Rejected #" + o.id + ": " + o.reason, "err");
      } else if (e.kind === "cancelled") {
        toast("Cancelled #" + o.id);
      } else if (e.kind === "close_requested") {
        toast("Closing #" + e.position_id + " on the next tick");
      } else if (e.kind === "closed") {
        var t = e.trade;
        toast("Closed #" + t.position_id + " (" + t.exit_reason.toUpperCase() + "): " + signed(t.pips, 1) + " pips, " +
          usd(t.net_usd) + " USD" + (t.r_multiple !== null ? ", " + signed(t.r_multiple, 2) + "R" : ""), t.net_usd < 0 ? "err" : "ok");
        snapshot(t.position_id, "exit");
        S.trades.push(t);
        showLastTrade(t);
      }
    });
  }

  function showLastTrade(t) {
    S.lastTrade = t;
    $("lastTradePanel").classList.remove("hidden");
    $("lastTrade").innerHTML = "<div class='item'><div class='what'>#" + t.id + " " + t.side.toUpperCase() + " " + t.lots.toFixed(2) +
      " · " + t.exit_reason.toUpperCase() + "</div><div class='pnl " + cls(t.net_usd) + "'>" + usd(t.net_usd) +
      (t.r_multiple !== null ? "<br><span class='tiny'>" + signed(t.r_multiple, 2) + "R</span>" : "") + "</div>" +
      "<div class='meta'>" + px(t.entry_price) + " → " + px(t.exit_price) + " · " + signed(t.pips, 1) + " pips</div></div>";
    $("tagsIn").value = "";
    $("noteIn").value = "";
  }

  $("tagChips").innerHTML = TAGS.map(function (t) { return "<button type='button' data-tag='" + t + "'>" + t + "</button>"; }).join("");
  $("tagChips").addEventListener("click", function (e) {
    var b = e.target.closest("[data-tag]");
    if (!b) return;
    var tags = $("tagsIn").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    var i = tags.indexOf(b.dataset.tag);
    if (i >= 0) tags.splice(i, 1); else tags.push(b.dataset.tag);
    $("tagsIn").value = tags.join(", ");
  });
  $("saveNote").onclick = function () {
    if (!S.lastTrade) return;
    send({ type: "annotate", trade_id: S.lastTrade.id, tags: $("tagsIn").value, note: $("noteIn").value });
  };
  $("addNote").onclick = function () {
    var body = $("sessionNote").value.trim();
    if (body) send({ type: "note", body: body });
  };
  $("endSession").onclick = function () {
    if (confirm("End this session? Open positions close at the last price and pending orders are cancelled.")) send({ type: "end_session" });
  };

  function loadTrades() {
    if (!S.session) return;
    fetch("/api/trades?session=" + encodeURIComponent(S.session.id)).then(function (r) { return r.json(); }).then(function (rows) {
      S.trades = rows;
      refreshMarkers(true);
    });
  }

  var markerKey = "";
  function refreshMarkers(force) {
    var T = C.tokens();
    var first = C.firstTime(), last = C.lastTime();
    if (first === null) return;
    var key = [S.chartTf, first, (S.trades || []).length, document.documentElement.dataset.theme,
      ((S.book && S.book.positions) || []).map(function (p) { return p.id; }).join("-")].join("|");
    if (key === markerKey && !force) return;
    markerKey = key;
    var tf = S.tfSeconds;
    function barTime(ms) { return Math.floor(ms / 1000 / tf) * tf; }
    var ms = [];
    function add(m) { if (m.time >= first && m.time <= last) ms.push(m); }
    (S.trades || []).forEach(function (t) {
      add({ time: barTime(t.entry_ms), position: t.side === "buy" ? "belowBar" : "aboveBar", color: t.side === "buy" ? T.up : T.down,
        shape: t.side === "buy" ? "arrowUp" : "arrowDown", text: (t.side === "buy" ? "B " : "S ") + t.lots.toFixed(2) });
      add({ time: barTime(t.exit_ms), position: t.side === "buy" ? "aboveBar" : "belowBar", color: T.muted, shape: "circle",
        text: t.exit_reason.toUpperCase() + " " + signed(t.pips, 1) });
    });
    ((S.book && S.book.positions) || []).forEach(function (p) {
      add({ time: barTime(p.entry_ms), position: p.side === "buy" ? "belowBar" : "aboveBar", color: p.side === "buy" ? T.up : T.down,
        shape: p.side === "buy" ? "arrowUp" : "arrowDown", text: (p.side === "buy" ? "B " : "S ") + p.lots.toFixed(2) });
    });
    ms.sort(function (a, b) { return a.time - b.time; });
    C.setMarkers(ms);
  }

  // ---------------------------------------------------------------- controls

  function buildTimeframes(tfs, onPick) {
    $("tfGroup").innerHTML = tfs.map(function (t) { return "<button data-tf='" + t.key + "'>" + t.label + "</button>"; }).join("");
    $("tfGroup").onclick = function (e) {
      var b = e.target.closest("[data-tf]");
      if (b) onPick(b.dataset.tf);
    };
    if (S.chartTf) setTimeframe(S.chartTf);
  }

  function setTimeframe(tf) {
    S.chartTf = tf;
    var t = S.timeframes.filter(function (x) { return x.key === tf; })[0];
    if (t) { S.tfSeconds = t.seconds; $("tfLabel").textContent = t.label; }
    document.querySelectorAll("#tfGroup [data-tf]").forEach(function (b) { b.classList.toggle("on", b.dataset.tf === tf); });
  }

  function buildSpeeds(speeds) {
    $("speedSel").innerHTML = speeds.map(function (s) { return "<option value='" + s + "'>" + s + "</option>"; }).join("");
  }

  $("playBtn").onclick = function () { send({ type: S.status && S.status.playing ? "pause" : "play" }); };
  $("speedSel").onchange = function () { send({ type: "speed", value: this.value }); };
  $("stepTick").onclick = function () { send({ type: "step", kind: "tick" }); };
  $("stepCandle").onclick = function () { send({ type: "step", kind: "candle" }); };
  $("jumpBtn").onclick = function () {
    var v = $("jumpInput").value;
    if (v) send({ type: "jump", time: v });
  };

  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName) || "";
    if (S.review || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || $("sessionDlg").open || $("dataDlg").open) return;
    if (document.body.classList.contains("live-session")) return; // the market has its own clock
    if (e.code === "Space") { e.preventDefault(); $("playBtn").click(); }
    else if (e.key === "ArrowRight" && e.shiftKey) { e.preventDefault(); send({ type: "step", kind: "tick" }); }
    else if (e.key === "ArrowRight") { e.preventDefault(); send({ type: "step", kind: "candle" }); }
  });

  document.querySelectorAll(".tools [data-tool]").forEach(function (b) {
    b.onclick = function () { D.setTool(b.dataset.tool || null); };
  });
  $("delDrawing").onclick = function () { D.deleteSelected(); };

  // ---------------------------------------------------------------- drawbar (selected drawing)

  function showDrawbar(d) {
    var bar = $("drawbar");
    if (!d) { bar.classList.add("hidden"); return; }
    var p = D.screenPoint(d);
    if (!p || p.x === null || p.y === null) { bar.classList.add("hidden"); return; }
    $("drawToOrder").classList.toggle("hidden", d.kind !== "position" || !!S.review);
    bar.classList.remove("hidden");
    var wrap = $("chart").getBoundingClientRect();
    var x = Math.min(Math.max(p.x + 10, 4), wrap.width - bar.offsetWidth - 70);
    var y = Math.min(Math.max(p.y - 44, 4), wrap.height - 80);
    bar.style.left = x + "px";
    bar.style.top = y + "px";
  }
  $("drawDelete").onclick = function () { D.deleteSelected(); };
  $("drawToOrder").onclick = function () {
    var d = D.selected();
    if (!d || d.kind !== "position" || !S.quote) return;
    var entry = d.points[0].price, stop = d.points[1].price, target = d.points[2].price;
    var long = stop < entry;
    var type = long ? (entry < S.quote.ask ? "limit" : "stop") : (entry > S.quote.bid ? "limit" : "stop");
    var msg = { type: "order", side: long ? "buy" : "sell", order_type: type, price: entry, sl: stop, tp: target };
    var size = parseFloat($("sizeIn").value);
    if (S.sizeMode === "risk") msg.risk_pct = size; else msg.lots = size;
    send(msg);
  };

  // ---------------------------------------------------------------- order ticket

  function setSide(side) {
    S.side = side;
    $("sideBuy").classList.toggle("on", side === "buy");
    $("sideSell").classList.toggle("on", side === "sell");
    updateTicket();
  }
  $("sideBuy").onclick = function () { setSide("buy"); };
  $("sideSell").onclick = function () { setSide("sell"); };

  $("typeSeg").onclick = function (e) {
    var b = e.target.closest("[data-type]");
    if (!b) return;
    S.otype = b.dataset.type;
    document.querySelectorAll("#typeSeg button").forEach(function (x) { x.classList.toggle("on", x === b); });
    $("priceIn").disabled = S.otype === "market";
    $("priceIn").placeholder = S.otype === "market" ? "at market" : "trigger price";
    if (S.otype !== "market" && !$("priceIn").value && S.quote) $("priceIn").value = (S.side === "buy" ? S.quote.ask : S.quote.bid).toFixed(DIGITS);
    updateTicket();
  };
  $("sizeSeg").onclick = function (e) {
    var b = e.target.closest("[data-size]");
    if (!b) return;
    S.sizeMode = b.dataset.size;
    document.querySelectorAll("#sizeSeg button").forEach(function (x) { x.classList.toggle("on", x === b); });
    $("sizeLabel").textContent = S.sizeMode === "risk" ? "Risk %" : "Lots";
    $("sizeIn").value = S.sizeMode === "risk" ? "1" : "0.10";
    $("sizeIn").step = S.sizeMode === "risk" ? "0.1" : "0.01";
    updateTicket();
  };
  ["sizeIn", "priceIn", "slIn", "tpIn"].forEach(function (id) { $(id).addEventListener("input", updateTicket); });

  function ticketValues() {
    var v = function (id) { var x = parseFloat($(id).value); return isFinite(x) ? x : null; };
    return { size: v("sizeIn"), price: S.otype === "market" ? null : v("priceIn"), sl: v("slIn"), tp: v("tpIn") };
  }

  function updateTicket() {
    var v = ticketValues();
    var btn = $("placeBtn");
    btn.className = "place " + S.side;
    var typeText = S.otype === "market" ? "" : " " + S.otype;
    var where = S.otype === "market" ? "at market" : "at " + (v.price ? v.price.toFixed(DIGITS) : "?");
    var hint = S.otype === "market" ? "Fills on the next tick, never the one on screen." :
      (S.side === "buy" ? (S.otype === "limit" ? "Fills when the Ask falls to the price." : "Fills when the Ask rises to the price, possibly worse.") :
        (S.otype === "limit" ? "Fills when the Bid rises to the price." : "Fills when the Bid falls to the price, possibly worse."));
    if (S.sizeMode === "risk") {
      var ref = v.price || (S.quote ? (S.side === "buy" ? S.quote.ask : S.quote.bid) : null);
      var bal = S.book ? S.book.account.balance : null;
      var pipValue = S.book && S.book.account.pip_value;
      if (v.size && v.sl && ref && bal && pipValue) {
        var pips = Math.round(Math.abs(ref - v.sl) / PIP * 10) / 10;
        var lots = pips ? Math.floor(bal * v.size / 100 / (pips * pipValue) * 100 + 1e-9) / 100 : 0;
        btn.textContent = (S.side === "buy" ? "Buy" : "Sell") + typeText + " " + lots.toFixed(2) + " " + where;
        hint = "Risk " + usd(bal * v.size / 100) + " USD over " + pips.toFixed(1) + " pips. " + hint;
      } else {
        btn.textContent = (S.side === "buy" ? "Buy" : "Sell") + typeText + " " + where;
        hint = "Sizing by risk needs a stop loss.";
      }
    } else {
      btn.textContent = (S.side === "buy" ? "Buy" : "Sell") + typeText + " " + (v.size ? v.size.toFixed(2) : "?") + " " + where;
    }
    $("ticketHint").textContent = hint;
  }

  $("placeBtn").onclick = function () {
    var v = ticketValues();
    var msg = { type: "order", side: S.side, order_type: S.otype, price: v.price, sl: v.sl, tp: v.tp };
    if (S.sizeMode === "risk") msg.risk_pct = v.size; else msg.lots = v.size;
    send(msg);
  };

  // ---------------------------------------------------------------- session dialog

  var DLG = { mode: "replay", meta: null };

  function openSessionDialog(opts) {
    opts = opts || {};
    var dlg = $("sessionDlg");
    $("dataNotice").innerHTML = "";
    $("sessionStart").disabled = !!opts.preparing;
    if (opts.preparing) $("dataNotice").innerHTML = "<div class='notice' id='prepNotice'>Preparing the market data…</div>";
    var metaP = opts.meta ? Promise.resolve(opts.meta) : fetch("/api/meta").then(function (r) { return r.json(); });
    metaP.then(function (meta) {
      if (!meta.ready) return;
      DLG.meta = meta;
      $("newTf").innerHTML = meta.timeframes.map(function (t) { return "<option value='" + t.key + "'>" + t.label + "</option>"; }).join("");
      $("newTf").value = meta.defaults.timeframe;
      if (opts.start) $("newStart").value = opts.start;
      var keep = opts.symbol || $("newSymbol").value || (S.session && S.session.instrument && S.session.instrument.symbol) || meta.defaults.symbol;
      $("newSymbol").innerHTML = meta.instruments.map(function (i) {
        var hist = i.feeds.filter(function (f) { return !f.live; })[0];
        var live = i.feeds.some(function (f) { return f.live; });
        var note = hist ? hist.first_day + " → " + hist.last_day : (live ? "live only" : "downloading…");
        return "<option value='" + i.symbol + "'" + (i.feeds.length ? "" : " disabled") + ">" + esc(i.label) + " · " + note + "</option>";
      }).join("");
      var usable = meta.instruments.filter(function (i) { return i.feeds.length; }).map(function (i) { return i.symbol; });
      $("newSymbol").value = usable.indexOf(keep) >= 0 ? keep : (usable[0] || keep);
      setMode(opts.mode || DLG.mode);
    });
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (rows) {
      var active = rows.filter(function (r) { return r.status === "active" && (!S.session || r.id !== S.session.id || !S.attached); });
      $("resumeBox").classList.toggle("hidden", !active.length);
      $("resumeList").innerHTML = active.slice(0, 12).map(function (r) {
        return "<button type='button' data-resume='" + r.id + "'><span>" + (r.mode === "live" ? "<b>Live</b> · " : "") +
          "<b>" + esc(r.symbol || "") + "</b> · " + esc(r.name || "Session " + r.id) + " · at " + esc(fmtTime(r.sim_ms)) + "</span><span class='mono " + cls(r.net_usd) + "'>" +
          r.trades + " trades · " + usd(r.net_usd) + "</span></button>";
      }).join("");
    });
    $("sessionCancel").classList.toggle("hidden", !S.attached);
    if (!dlg.open) dlg.showModal();
  }

  function setMode(mode) {
    DLG.mode = mode;
    var meta = DLG.meta;
    document.querySelectorAll("#modeSeg [data-mode]").forEach(function (b) { b.classList.toggle("on", b.dataset.mode === mode); });
    document.querySelectorAll("#sessionForm .replay-field").forEach(function (el) { el.classList.toggle("hidden", mode === "live"); });
    if (!meta) return;
    var inst = currentInstrument();
    var feeds = (inst ? inst.feeds : []).filter(function (f) { return mode === "replay" || f.live; });
    $("newFeed").innerHTML = feeds.map(function (f) {
      var range = f.live && f.last_tick_ms ? "recorded " + f.first_day + " → now" : f.first_day + " → " + f.last_day;
      return "<option value='" + f.key + "'>" + esc(f.label) + " (" + range + ", " + f.days + " days)</option>";
    }).join("");
    $("newFeed").closest("label").classList.toggle("hidden", !feeds.length);
    $("liveNote").innerHTML = "";
    $("sessionStart").disabled = false;
    if (mode === "live") {
      if (!feeds.length) {
        $("liveNote").innerHTML = "No live " + esc(inst ? inst.label : "") + " data yet. Start the recorder in <a href='#' id='openDataFromDlg'>Market data</a>: " +
          "open your MT5 terminal, pick it there, and come back.";
        $("sessionStart").disabled = true;
        $("openDataFromDlg").onclick = function (e) { e.preventDefault(); openDataDialog(); };
      } else {
        $("liveNote").textContent = "The clock is the real clock: candles form as ticks arrive, orders fill on the next live tick, " +
          "and the chart's history is what the recorder has saved from this broker.";
      }
      $("sessionStart").textContent = "Start live session";
    } else {
      $("sessionStart").textContent = "Start replay";
      updateStartRange();
    }
  }

  function currentInstrument() {
    var meta = DLG.meta;
    return meta ? meta.instruments.filter(function (i) { return i.symbol === $("newSymbol").value; })[0] : null;
  }

  function updateStartRange() {
    var inst = currentInstrument();
    var f = inst && inst.feeds.filter(function (x) { return x.key === $("newFeed").value; })[0];
    if (!f) { $("dataNotice").innerHTML = ""; return; }
    $("newStart").min = f.first_day + "T00:00";
    $("newStart").max = f.last_day + "T23:59";
    var v = $("newStart").value;
    if (!v || v < $("newStart").min || v > $("newStart").max) {
      var d = new Date(f.first_day + "T07:00:00Z");
      d.setUTCDate(d.getUTCDate() + Math.min(365, Math.max(0, f.days - 5)));
      $("newStart").value = d.toISOString().slice(0, 16);
    }
    $("dataNotice").innerHTML = "<p class='tiny muted' style='margin:0 0 12px'>" + esc(f.label) + ": " + esc(inst.label) + " ticks " + f.first_day +
      " to " + f.last_day + " (" + f.days + " days), Bid candles, UTC. 1 lot = " + num(inst.contract) + " " + esc(inst.unit) +
      ". News: " + inst.currencies.join(", ") + " calendar.</p>";
  }

  $("modeSeg").onclick = function (e) {
    var b = e.target.closest("[data-mode]");
    if (b) setMode(b.dataset.mode);
  };
  $("newFeed").onchange = function () { if (DLG.mode === "replay") updateStartRange(); };
  $("newSymbol").onchange = function () { setMode(DLG.mode); };

  $("resumeList").onclick = function (e) {
    var b = e.target.closest("[data-resume]");
    if (!b) return;
    send({ type: "hello", session: b.dataset.resume });
  };
  $("sessionBtn").onclick = function () { openSessionDialog({}); };
  $("sessionDlg").addEventListener("cancel", function (e) { if (!S.attached) e.preventDefault(); });
  $("sessionForm").addEventListener("submit", function (e) {
    var which = e.submitter ? e.submitter.value : "start";
    if (which !== "start") return;
    e.preventDefault();
    var live = DLG.mode === "live";
    send({
      type: "new",
      mode: DLG.mode,
      symbol: $("newSymbol").value,
      feed: $("newFeed").value || null,
      start: live || $("newRandom").checked ? null : $("newStart").value,
      random: !live && $("newRandom").checked,
      hide_dates: !live && $("newHide").checked,
      balance: $("newBalance").value,
      leverage: $("newLeverage").value,
      commission: $("newCommission").value,
      slippage: $("newSlippage").value,
      timeframe: $("newTf").value,
      name: $("newName").value
    });
  });
  $("newRandom").onchange = function () { $("newStart").disabled = this.checked; };

  // ---------------------------------------------------------------- market data panel

  function ago(ms) {
    if (!ms) return "never";
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return s + " s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 172800) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }
  function utc(ms) { return ms ? new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "-"; }

  var REC_STATE = {
    recording: ["on", "Recording"], backfilling: ["wait", "Filling the gap"], connecting: ["wait", "Attaching"],
    waiting: ["wait", "Waiting"], error: ["err", "Problem"], stopped: ["", "Off"]
  };

  function renderChip(d) {
    var job = d.job || {};
    var text;
    if (!d.ready && job.kind === "prepare" && job.running) text = "Preparing data " + pct(job) + "%";
    else if (!d.ready && d.problem) text = "Data missing";
    else if (job.kind === "update" && job.running) text = "Downloading " + (job.symbol || "") + " " + (job.day || "") + " · " + pct(job) + "%";
    else {
      var last = (d.instruments || []).reduce(function (a, i) { return Math.max(a, i.last_tick_ms || 0); }, 0);
      text = last ? "Data to " + utc(last).slice(0, 16) : "Data";
    }
    var rec = d.live && d.live.recorder;
    var st = rec ? (REC_STATE[rec.state] || ["", rec.state]) : ["", "Off"];
    $("liveDot").className = "dot " + st[0];
    $("dataChip").textContent = text + " · Live: " + st[1];
  }

  function pct(job) { return job.total ? Math.floor(100 * job.done / job.total) : 0; }

  function pollData() {
    var open = $("dataDlg").open;
    fetch("/api/data" + (open ? "?terminals=1" : "")).then(function (r) { return r.json(); }).then(function (d) {
      renderChip(d);
      if (open) renderDataDialog(d);
      var prep = $("prepNotice");
      if (prep) {
        var job = d.job || {};
        prep.innerHTML = d.problem ? esc(d.problem) :
          esc(job.message || "Preparing the market data") + (job.total ? " · " + pct(job) + "%" : "") +
          "<br><span class='tiny muted'>Once only: the 2023–24 file is imported, older tick files are compacted, and candles are built. About a minute.</span>";
        prep.classList.toggle("err", !!d.problem);
      }
    }).catch(function () { /* server restarting */ });
  }

  function waitUntilReady() {
    var timer = setInterval(function () {
      fetch("/api/data").then(function (r) { return r.json(); }).then(function (d) {
        if (d.ready) {
          clearInterval(timer);
          S.reconnect = true;
          if ($("sessionDlg").open) $("sessionDlg").close();
          connect();
        }
      }).catch(function () {});
    }, 1000);
  }

  var selectedTerminal = null;

  function renderDataDialog(d) {
    var job = d.job || {};
    renderInstruments(d);
    var running = !!job.running;
    $("jobBar").style.width = (running ? pct(job) : (job.kind ? 100 : 0)) + "%";
    $("jobMsg").textContent = job.kind ? (job.message || "") + (running && job.total ? " · " + job.done + "/" + job.total + " days" : "") +
      (!running && job.finished_ms ? " · " + ago(job.finished_ms) : "") : "";
    $("jobMsg").classList.toggle("down", !!job.error);
    $("updateNow").disabled = running || !d.prepared;
    $("stopJob").classList.toggle("hidden", !(running && job.kind === "update"));

    var live = d.live || {};
    var terms = live.terminals || [];
    var rec = live.recorder;
    var current = rec ? rec.terminal : (live.settings || {}).terminal;
    if (selectedTerminal === null) selectedTerminal = current || (terms[0] && terms[0].path) || null;
    $("termList").innerHTML = terms.length ? terms.map(function (t) {
      var badges = (t.running ? "<span class='badge ok'>open</span> " : "<span class='badge'>closed</span> ") +
        (t.demo ? "<span class='badge ok'>demo</span>" : (t.server ? "<span class='badge warn'>not demo</span>" : ""));
      return "<label class='term'><input type='radio' name='term' value='" + esc(t.path) + "'" + (t.path === selectedTerminal ? " checked" : "") + ">" +
        "<span class='tname'>" + esc(t.name) + "</span><span>" + badges + "</span>" +
        "<span class='tmeta'>Last server: " + esc(t.server || "unknown") + " · " + esc(t.path) + "</span></label>";
    }).join("") : "<div class='empty'>No MT5 terminal found on this computer.</div>";

    $("recStart").classList.toggle("hidden", !!rec);
    $("recStop").classList.toggle("hidden", !rec);
    $("recStart").disabled = !selectedTerminal;
    if (rec) {
      var st = REC_STATE[rec.state] || ["", rec.state];
      var rows = [
        ["State", "<span class='dot " + st[0] + "' style='display:inline-block;margin-right:6px'></span>" + esc(st[1]) + " · " + esc(rec.message || "")],
        ["Terminal", esc(rec.terminal || "")]
      ];
      if (rec.server) rows.push(["Server", esc(rec.server) + " (" + esc(rec.account_type || "?") + ")"]);
      if (rec.offset_hours !== null && rec.offset_hours !== undefined) rows.push(["Server clock", "UTC" + (rec.offset_hours >= 0 ? "+" : "") + rec.offset_hours + " h now, converted to UTC"]);
      if (rec.history_clock) rows.push(["Past days", "server clock taken as " + esc(rec.history_clock)]);
      if (rec.last_tick_ms) rows.push(["Last tick", utc(rec.last_tick_ms) + " · " + ago(rec.last_tick_ms)]);
      if (rec.ticks_today !== undefined) rows.push(["Ticks today", num(rec.ticks_today)]);
      if ((rec.missing || []).length) rows.push(["Not at this broker", esc(rec.missing.join(", "))]);
      var syms = (rec.symbols || []).map(function (x) {
        var h = x.history || {};
        var hist = h.state === "fetching" ? "history: fetching, " + h.pending + " days to go" :
          h.state === "done" ? "history: complete" + (h.floor ? " (broker has nothing before " + h.floor + ")" : "") : "history: off";
        return "<tr><td class='mono'>" + esc(x.broker_symbol) + "</td><td>" + esc(x.label) + "</td><td class='mono'>" +
          (x.last_tick_ms ? ago(x.last_tick_ms) : "-") + "</td><td class='n'>" + num(x.ticks_today || 0) + "</td><td class='tiny'>" + hist + "</td></tr>";
      }).join("");
      $("recStatus").innerHTML = "<dl class='kv'>" + rows.map(function (r) { return "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>"; }).join("") + "</dl>" +
        (syms ? "<div class='tablewrap'><table class='t'><thead><tr><th>Broker symbol</th><th>Instrument</th><th>Last tick</th><th class='n'>Today</th><th>History</th></tr></thead><tbody>" +
          syms + "</tbody></table></div>" : "");
      if (rec.account_type === "real") $("recStatus").innerHTML += "<p class='warnline'>This is a real-money account. Recording only reads prices, but a demo account is the safer source.</p>";
    } else {
      $("recStatus").innerHTML = "<p class='tiny muted'>Not recording.</p>";
    }
    var feeds = live.feeds || [];
    $("feedList").innerHTML = feeds.length ? "<p class='tiny muted'>Saved live data: " + feeds.map(function (f) {
      return esc(f.label) + ", " + f.days + " days from " + f.first_day + (f.last_tick_ms ? ", last tick " + ago(f.last_tick_ms) : "");
    }).join("; ") + ". Start a <b>Live market</b> session from Sessions, or replay it like history.</p>" : "";
  }

  function gb(bytes) { return bytes >= 1e9 ? (bytes / 1e9).toFixed(1) + " GB" : Math.round(bytes / 1e6) + " MB"; }
  function hours(sec) { return sec < 3600 ? Math.max(1, Math.round(sec / 60)) + " min" : (sec / 3600).toFixed(1) + " h"; }

  // One row per instrument: what is stored, from which day downloads start
  // (editable), what is still missing and roughly what it costs.
  var editingFrom = null;
  function renderInstruments(d) {
    var list = d.instruments || [];
    if (!list.length) { $("instTable").innerHTML = "<tbody><tr><td class='faint'>" + esc(d.problem || "Preparing the market data…") + "</td></tr></tbody>"; return; }
    if (editingFrom && document.activeElement && document.activeElement.dataset && document.activeElement.dataset.from) return;
    var job = d.job || {}, per = job.instruments || {};
    var totalPend = 0, totalBytes = 0, totalSec = 0;
    var rows = list.map(function (i) {
      if (i.enabled) { totalPend += i.pending_days; totalBytes += i.pending_bytes; totalSec += i.pending_seconds; }
      var have = i.days ? i.first_day + " → " + (i.last_tick_ms ? utc(i.last_tick_ms).slice(0, 16) : i.last_day) + "<br><span class='pend'>" + i.days + " days · " + gb(i.size_bytes) + "</span>" :
        "<span class='faint'>nothing yet</span>";
      var busy = job.running && job.symbol === i.symbol;
      var miss = !i.enabled ? "<span class='faint'>off</span>" : i.pending_days ?
        i.pending_days + " days<br><span class='pend'>≈ " + hours(i.pending_seconds) + " · " + gb(i.pending_bytes) + "</span>" + (busy ? " <span class='badge ok'>now</span>" : "") :
        "<span class='badge ok'>complete</span>";
      var rec = d.live && d.live.recorder;
      var rt = rec && (rec.symbols || []).filter(function (x) { return x.symbol === i.symbol; })[0];
      var broker = (i.broker || []).map(function (f) {
        return esc(f.label.replace(/^MT5 /, "")) + ": " + f.first_day + " → " + f.last_day + "<br><span class='pend'>" + f.days + " days</span>";
      }).join("<br>");
      if (rt && rt.history && rt.history.state === "fetching") broker += (broker ? "<br>" : "") + "<span class='badge ok'>fetching</span> <span class='pend'>" + rt.history.pending + " days to go</span>";
      if (rec && rec.state === "recording" && !rt && (rec.missing || []).length) broker = broker || "<span class='faint'>not offered by this broker</span>";
      return "<tr class='" + (i.enabled ? "" : "off") + "'><td><b>" + esc(i.label) + "</b><br><span class='pend mono'>" + esc(i.symbol) + " · " + esc(i.currencies.join(" ")) + " news</span></td>" +
        "<td class='mono tiny'>" + have + "</td>" +
        "<td class='mono tiny'>" + (broker || "<span class='faint'>-</span>") + "</td>" +
        "<td><input type='date' data-from='" + i.symbol + "' value='" + i.from + "' min='2003-01-01' max='" + new Date().toISOString().slice(0, 10) + "' title='Download ticks from this day on'></td>" +
        "<td class='tiny'>" + miss + "</td>" +
        "<td><label class='check' title='Download this instrument'><input type='checkbox' data-enabled='" + i.symbol + "'" + (i.enabled ? " checked" : "") + "></label></td></tr>";
    });
    $("instTable").innerHTML = "<thead><tr><th>Instrument</th><th>Dukascopy</th><th>Broker (MT5)</th><th>From</th><th>Missing at Dukascopy</th><th>On</th></tr></thead><tbody>" + rows.join("") + "</tbody>";
    var disk = d.disk || {};
    $("diskInfo").innerHTML = "Still to download: " + totalPend + " days, ≈ " + hours(totalSec) + " and " + gb(totalBytes) + " at the datafeed's pace. " +
      "Disk: " + gb(disk.free_bytes || 0) + " free; downloads stop before it drops under " + gb(disk.min_free_bytes || 0) + ". " +
      "Dukascopy's tick history goes back to 2003 for the currency pairs; an earlier <b>From</b> fetches more, oldest last. " +
      "<b>From</b> and <b>On</b> also set how far back the recorder fetches each instrument's history from your broker.";
    var cal = d.calendar || {};
    $("calInfo").innerHTML = cal.ok ? num(cal.events) + " events with a time, " + utc(cal.first_ms).slice(0, 10) + " → " + utc(cal.last_ms).slice(0, 10) +
      ", rated high, medium or low, read from the fxmacro database (<span class='mono'>" + esc(cal.path) + "</span>). " +
      "It grows when fxmacro collects the calendar; the simulator only reads it. Each instrument shows its own currencies' events." :
      "<span class='down'>" + esc(cal.error || "The calendar is not available.") + "</span>";
  }

  $("instTable").addEventListener("focusin", function (e) { if (e.target.dataset.from) editingFrom = e.target.dataset.from; });
  $("instTable").addEventListener("change", function (e) {
    var t = e.target, body = null;
    if (t.dataset.from) { body = { symbol: t.dataset.from, start: t.value }; editingFrom = null; t.blur(); }
    if (t.dataset.enabled) body = { symbol: t.dataset.enabled, enabled: t.checked };
    if (!body) return;
    fetch("/api/data/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || r.statusText); return j; }); })
      .then(function () {
        toast("Saved. " + (body.start ? "Downloads now start from " + body.start + "." : body.enabled ? "Downloading " + body.symbol + "." : body.symbol + " is not downloaded any more; what is stored stays."));
        fetch("/api/data").then(function (r) { return r.json(); }).then(function (d) { if (!d.job.running && d.prepared) $("updateNow").click(); else pollData(); });
      })
      .catch(function (err) { toast(String(err.message || err), "err"); pollData(); });
  });

  function openDataDialog() {
    selectedTerminal = null;
    if (!$("dataDlg").open) $("dataDlg").showModal();
    pollData();
  }

  $("termList").addEventListener("change", function (e) {
    if (e.target.name === "term") { selectedTerminal = e.target.value; $("recStart").disabled = false; }
  });
  $("dataBtn").onclick = openDataDialog;
  $("sessionData").onclick = openDataDialog;
  // Closing the panel refreshes the session form, so a feed the recorder
  // just started appears in Live mode without reopening anything.
  $("dataDlg").addEventListener("close", function () {
    if ($("sessionDlg").open && DLG.meta) {
      fetch("/api/meta").then(function (r) { return r.json(); }).then(function (meta) {
        if (meta.ready) { DLG.meta = meta; setMode(DLG.mode); }
      });
    }
  });
  $("dataClose").onclick = function () { $("dataDlg").close(); };
  $("updateNow").onclick = function () {
    fetch("/api/data/update", { method: "POST" }).then(function (r) { return r.json(); }).then(function (r) {
      if (!r.started) toast("An update is already running.");
      pollData();
    });
  };
  $("stopJob").onclick = function () { fetch("/api/data/stop", { method: "POST" }).then(pollData); };
  $("recStart").onclick = function () {
    if (!selectedTerminal) return;
    var t = selectedTerminal;
    fetch("/api/live/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ terminal: t }) })
      .then(function (r) { if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || r.statusText); }); })
      .then(function () { toast("Recorder on. It starts recording as soon as that terminal is open."); pollData(); })
      .catch(function (e) { toast(String(e.message || e), "err"); });
  };
  $("recStop").onclick = function () { fetch("/api/live/stop", { method: "POST" }).then(function () { pollData(); }); };

  setInterval(function () { if (!S.review) pollData(); }, 2000);

  // ---------------------------------------------------------------- theme

  $("themeBtn").onclick = function () {
    var light = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = light ? "light" : "dark";
    try { localStorage.setItem("sim-theme", light ? "light" : "dark"); } catch (e) { /* ignore */ }
    C.retheme();
    if (S.book) renderLines(S.book);
    refreshMarkers(true);
    N.redraw();
  };

  // ---------------------------------------------------------------- review mode

  function startReview(sid, tid, tf) {
    S.review = { sid: sid, tid: tid };
    document.body.classList.add("review");
    var tfs = [["m1", "1M", 60], ["m5", "5M", 300], ["m15", "15M", 900], ["h1", "1H", 3600], ["h4", "4H", 14400], ["d1", "1D", 86400]];
    S.timeframes = tfs.map(function (t) { return { key: t[0], label: t[1], seconds: t[2] }; });
    buildTimeframes(S.timeframes, loadReview);
    loadReview(tf || "m15");
  }

  function loadReview(tf) {
    var r = S.review;
    fetch("/api/review/" + r.sid + "/" + r.tid + "?tf=" + tf).then(function (res) {
      if (!res.ok) throw new Error("trade not found");
      return res.json();
    }).then(function (data) {
      var t = data.trade;
      S.reviewTrade = t;
      if (data.instrument) setInstrument(data.instrument);
      S.session = { id: r.sid, hide_dates: !!data.hide_dates };
      C.setHideDates(!!data.hide_dates);
      setTimeframe(tf);
      C.setHistory(data.bars, data.seconds);
      N.reload();
      lastBar = data.bars.length ? data.bars[data.bars.length - 1] : null;
      showOhlc(null);
      S.trades = [t];
      S.book = { positions: [], pending: [] };
      refreshMarkers(true);
      var T = C.tokens();
      var lines = [{ id: "e", price: t.entry_price, color: T.accent, title: "Entry", style: "solid" },
                   { id: "x", price: t.exit_price, color: T.muted, title: "Exit " + t.exit_reason.toUpperCase(), style: "solid" }];
      if (t.initial_sl) lines.push({ id: "sl", price: t.initial_sl, color: T.down, title: "Initial SL", style: "dashed" });
      if (t.tp) lines.push({ id: "tp", price: t.tp, color: T.up, title: "TP", style: "dashed" });
      C.setLines(lines);
      var entryBar = Math.floor(t.entry_ms / 1000 / data.seconds) * data.seconds;
      var idx = data.bars.findIndex(function (b) { return b.time >= entryBar; });
      if (idx >= 0) C.chart.timeScale().setVisibleLogicalRange({ from: idx - 80, to: idx + 60 });
      $("reviewTitle").textContent = "Review · trade #" + t.id;
      var hold = Math.round((t.exit_ms - t.entry_ms) / 60000);
      var rows = [
        ["Instrument", esc(data.instrument ? data.instrument.label : "")],
        ["Side", t.side.toUpperCase() + " " + t.lots.toFixed(2)],
        ["Entry", px(t.entry_price) + " · " + fmtTime(t.entry_ms)],
        ["Exit", px(t.exit_price) + " · " + fmtTime(t.exit_ms)],
        ["Reason", t.exit_reason.toUpperCase()],
        ["Initial SL", px(t.initial_sl)],
        ["TP", px(t.tp)],
        ["Pips", signed(t.pips, 1)],
        ["Net", "<span class='" + cls(t.net_usd) + "'>" + usd(t.net_usd) + " USD</span>"],
        ["R", t.r_multiple === null ? "-" : signed(t.r_multiple, 2)],
        ["Held", hold >= 60 ? Math.floor(hold / 60) + "h " + (hold % 60) + "m" : hold + "m"],
        ["Tags", esc(t.tags || "-")]
      ];
      $("reviewTrade").innerHTML = rows.map(function (r) { return "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>"; }).join("");
      $("reviewNote").innerHTML = (t.note ? "<b>Note:</b> " + esc(t.note) + "<br><br>" : "") +
        (data.session_active ? "This session is still in play, so the chart stops at its clock." : "Review shows what happened after the exit too.");
    }).catch(function (e) { toast(String(e), "err"); });
  }

  $("replayFromEntry").onclick = function () {
    var t = S.reviewTrade;
    if (!t) return;
    sessionStorage.setItem("sim-new-start", isoMinute(t.entry_ms - 60 * 60 * 1000));
    location.href = "/";
  };

  // ---------------------------------------------------------------- boot

  // Console handle for debugging: __sim.state, __sim.chart, __sim.drawings.
  window.__sim = { state: S, chart: C, drawings: D, news: N, send: send };


  var params = new URLSearchParams(location.search);
  if (params.get("review")) {
    var parts = params.get("review").split(":");
    startReview(parts[0], parts[1], params.get("tf"));
  } else {
    connect();
  }
  updateTicket();
})();
