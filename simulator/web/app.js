/* Replay page: WebSocket client, controls, order ticket, review mode.
 * The server owns the clock, candles and broker; this page only draws what
 * it is sent and sends back what the trader asks for. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var PIP = 0.0001;
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
  function px(p) { return p === null || p === undefined ? "-" : Number(p).toFixed(5); }
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

  C.chart.subscribeCrosshairMove(function (param) {
    var bar = param && param.seriesData ? param.seriesData.get(C.series) : null;
    showOhlc(bar);
  });
  var lastBar = null;
  function showOhlc(bar) {
    bar = bar || lastBar;
    if (!bar) { $("ohlc").textContent = ""; return; }
    $("ohlc").innerHTML = "O <span class='mono'>" + px(bar.open) + "</span> H <span class='mono'>" + px(bar.high) +
      "</span> L <span class='mono'>" + px(bar.low) + "</span> C <span class='mono'>" + px(bar.close) + "</span>";
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
      case "fatal":
        S.reconnect = false;
        openSessionDialog({ problem: m.message });
        break;
      case "no_session":
        localStorage.removeItem(LS_SESSION);
        openSessionDialog({ meta: m.meta });
        break;
      case "session":
        S.session = m.session;
        S.attached = true;
        S.timeframes = m.session.timeframes;
        localStorage.setItem(LS_SESSION, m.session.id);
        if (new URLSearchParams(location.search).get("session")) history.replaceState(null, "", "/");
        C.setHideDates(m.session.hide_dates);
        buildTimeframes(m.session.timeframes, function (tf) { send({ type: "timeframe", timeframe: tf }); });
        buildSpeeds(m.session.speeds);
        $("sessionInfo").textContent = (m.session.name ? m.session.name + " · " : "") + "started " +
          fmtTime(m.session.start_ms) + " · id " + m.session.id;
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
    $("clock").textContent = fmtClock(st.sim_ms) + (st.finished ? "  (end of data)" : "");
    $("clock").classList.toggle("playing", st.playing);
    $("playBtn").textContent = st.playing ? "Pause" : "Play";
    $("playBtn").disabled = st.finished;
    if ($("speedSel").value !== st.speed) $("speedSel").value = st.speed;
  }

  function onQuote(q) {
    if (!q) return;
    S.quote = q;
    C.setQuote(q);
    $("bidPx").textContent = px(q.bid);
    $("askPx").textContent = px(q.ask);
    $("spreadPx").textContent = ((q.ask - q.bid) / PIP).toFixed(1);
    updateTicket();
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
    if (S.review || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || $("sessionDlg").open) return;
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
    if (S.otype !== "market" && !$("priceIn").value && S.quote) $("priceIn").value = (S.side === "buy" ? S.quote.ask : S.quote.bid).toFixed(5);
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
    var where = S.otype === "market" ? "at market" : "at " + (v.price ? v.price.toFixed(5) : "?");
    var hint = S.otype === "market" ? "Fills on the next tick, never the one on screen." :
      (S.side === "buy" ? (S.otype === "limit" ? "Fills when the Ask falls to the price." : "Fills when the Ask rises to the price, possibly worse.") :
        (S.otype === "limit" ? "Fills when the Bid rises to the price." : "Fills when the Bid falls to the price, possibly worse."));
    if (S.sizeMode === "risk") {
      var ref = v.price || (S.quote ? (S.side === "buy" ? S.quote.ask : S.quote.bid) : null);
      var bal = S.book ? S.book.account.balance : null;
      if (v.size && v.sl && ref && bal) {
        var pips = Math.round(Math.abs(ref - v.sl) / PIP * 10) / 10;
        var lots = pips ? Math.floor(bal * v.size / 100 / (pips * 10) * 100 + 1e-9) / 100 : 0;
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

  function openSessionDialog(opts) {
    opts = opts || {};
    var dlg = $("sessionDlg");
    $("dataNotice").innerHTML = "";
    $("sessionStart").disabled = false;
    if (opts.problem) {
      $("dataNotice").innerHTML = "<div class='notice err'>" + esc(opts.problem).replace(/`([^`]+)`/g, "<code>$1</code>") + "</div>";
      $("sessionStart").disabled = true;
    }
    var metaP = opts.meta ? Promise.resolve(opts.meta) : fetch("/api/meta").then(function (r) { return r.json(); });
    metaP.then(function (meta) {
      if (!meta.ready) return;
      $("newTf").innerHTML = meta.timeframes.map(function (t) { return "<option value='" + t.key + "'>" + t.label + "</option>"; }).join("");
      $("newTf").value = meta.defaults.timeframe;
      $("newStart").min = meta.first_day + "T00:00";
      $("newStart").max = meta.last_day + "T23:59";
      if (opts.start) $("newStart").value = opts.start;
      else if (!$("newStart").value) {
        var d = new Date(meta.first_day + "T07:00:00Z");
        d.setUTCDate(d.getUTCDate() + 365);
        $("newStart").value = d.toISOString().slice(0, 16);
      }
      $("dataNotice").innerHTML += "<p class='tiny muted' style='margin:0 0 12px'>EUR/USD ticks " + meta.first_day + " to " + meta.last_day +
        " (" + meta.trading_days + " trading days), Bid candles, UTC.</p>";
    });
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (rows) {
      var active = rows.filter(function (r) { return r.status === "active" && (!S.session || r.id !== S.session.id || !S.attached); });
      $("resumeBox").classList.toggle("hidden", !active.length);
      $("resumeList").innerHTML = active.slice(0, 12).map(function (r) {
        return "<button type='button' data-resume='" + r.id + "'><span>" + esc(r.name || "Session " + r.id) + " · at " +
          esc(fmtTime(r.sim_ms)) + "</span><span class='mono " + cls(r.net_usd) + "'>" + r.trades + " trades · " + usd(r.net_usd) + "</span></button>";
      }).join("");
    });
    $("sessionCancel").classList.toggle("hidden", !S.attached);
    if (!dlg.open) dlg.showModal();
  }

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
    send({
      type: "new",
      start: $("newRandom").checked ? null : $("newStart").value,
      random: $("newRandom").checked,
      hide_dates: $("newHide").checked,
      balance: $("newBalance").value,
      leverage: $("newLeverage").value,
      commission: $("newCommission").value,
      slippage: $("newSlippage").value,
      timeframe: $("newTf").value,
      name: $("newName").value
    });
  });
  $("newRandom").onchange = function () { $("newStart").disabled = this.checked; };

  // ---------------------------------------------------------------- theme

  $("themeBtn").onclick = function () {
    var light = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = light ? "light" : "dark";
    try { localStorage.setItem("sim-theme", light ? "light" : "dark"); } catch (e) { /* ignore */ }
    C.retheme();
    if (S.book) renderLines(S.book);
    refreshMarkers(true);
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
      setTimeframe(tf);
      C.setHistory(data.bars, data.seconds);
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
  window.__sim = { state: S, chart: C, drawings: D, send: send };


  var params = new URLSearchParams(location.search);
  if (params.get("review")) {
    var parts = params.get("review").split(":");
    startReview(parts[0], parts[1], params.get("tf"));
  } else {
    connect();
  }
  updateTicket();
})();
