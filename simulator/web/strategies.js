/* Strategies: the six concepts of "Six Trading Concepts on Real EURUSD" on
 * the chart, and the routine's setups in the Strategies panel.
 *
 * The server works everything out at the session's clock (strategies.py),
 * from candles complete by then: this file asks for it (live every 2 s; a
 * replay when its clock has moved, at most once a second), draws it and
 * lists it. Which concepts are on, the target, the largest stop and the
 * profile's period are remembered in this browser; all six are on at first.
 *
 * Drawn: levels (dashed, amber) with their sweeps; supply and demand zones
 * (red and blue boxes, visits counted); order blocks (violet) and fair value
 * gaps (pink); breaks of structure; the daily EMA (teal steps); absorption
 * dots (orange); the volume profile on the right; and the setups, as an
 * entry with its stop and target boxes.
 */
(function () {
  "use strict";

  var CONCEPTS = [
    { key: "liquidity", name: "Liquidity sweep", color: "--s4", q: "Did the break of a level fail?",
      what: "Yesterday's high and low, equal highs and lows, the 20-day range's edges. A sweep goes through and closes back on a 15-minute candle; a close beyond is a real break." },
    { key: "smc", name: "Smart Money Concepts", color: "--s7", q: "What is each step of the reversal?",
      what: "1-hour breaks of structure and changes of character, order blocks (the last candle the other way before a break), fair value gaps, and the 20-day range's premium and discount halves." },
    { key: "zones", name: "Supply and demand", color: "--neg", q: "Where does the limit order go?",
      what: "1-hour bases of strong moves (1.5 average ranges), from the extreme wick to the body; only the first two returns count, a close through breaks it." },
    { key: "price_action", name: "Price action", color: "--s2", q: "What are the candles saying?",
      what: "A wide candle closing through the last small low; the 10-candle count of bodies and wicks; dots on busy candles (for the time of day) with little progress." },
    { key: "ema", name: "EMA 20, daily", color: "--s3", q: "Which side does the bigger picture favour?",
      what: "Sells only below a falling daily EMA, buys only above a rising one, from the day's open. Flat, or the open on the other side: the 20-day range's halves." },
    { key: "profile", name: "Volume profile", color: "--muted", q: "Is there room for the target?",
      what: "Tick volume per price over this session and the one before: the point of control and the value area (70 %). Inside value the room ends at the middle." }
  ];
  var STEPS = ["Side", "Level", "Room", "Failure", "Entry"];
  var LS_KEY = "sim-strategies";
  var STATUS = { waiting: "waiting", active: "open", won: "won", lost: "lost", expired: "expired", skipped: "skipped" };

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function rgba(hex, a) {
    var h = (hex || "#888").replace("#", "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    return "rgba(" + parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4, 6), 16) + "," + a + ")";
  }

  function create(C, opts) {
    var cfg = load();
    var ST = { data: null, busy: false, queued: false, lastFetch: 0, lastAt: null, key: "" };
    var $ = function (id) { return document.getElementById(id); };
    var overlay = C.attachOverlay(draw);

    // ---- settings ----

    function load() {
      var on = {}, v = null;
      try { v = JSON.parse(localStorage.getItem(LS_KEY) || "null"); } catch (e) { v = null; }
      CONCEPTS.forEach(function (c) { on[c.key] = !(v && v.on && v.on[c.key] === false); });
      return {
        on: on,
        target: v && v.target > 0 ? v.target : 10,
        maxStop: v && v.maxStop > 0 ? v.maxStop : 10,
        profile: v && ["session", "day", "rolling"].indexOf(v.profile) >= 0 ? v.profile : "session",
        skipped: !!(v && v.skipped)
      };
    }
    function save() { try { localStorage.setItem(LS_KEY, JSON.stringify(cfg)); } catch (e) { /* ignore */ } }
    function anyOn() { return CONCEPTS.some(function (c) { return cfg.on[c.key]; }); }
    function query() {
      return "on=" + CONCEPTS.filter(function (c) { return cfg.on[c.key]; }).map(function (c) { return c.key; }).join(",") +
        "&target=" + cfg.target + "&max_stop=" + cfg.maxStop + "&profile=" + cfg.profile;
    }

    // ---- data ----

    function clear() {
      if (!ST.data) return;
      ST.data = null;
      overlay.request();
      render();
    }

    function tick(force) {
      var s = opts.getSession();
      if (!s || !s.id || !opts.attached() || opts.review() || !anyOn()) { ST.key = ""; clear(); return; }
      var live = s.mode === "live";
      var st = opts.getStatus();
      var at = st ? st.sim_ms : null;
      var key = s.id + "|" + opts.getTf() + "|" + query();
      if (key !== ST.key) {  // another session, timeframe or setting: nothing old stays drawn
        force = true;
        if (ST.data && ST.data.session !== s.id) clear();
      }
      var now = Date.now();
      if (ST.busy) { if (force) ST.queued = true; return; }
      if (!force && (now - ST.lastFetch < (live ? 2000 : 1000) || (!live && at === ST.lastAt))) return;
      ST.busy = true; ST.lastFetch = now; ST.lastAt = at; ST.key = key;
      fetch("/api/strategies?session=" + encodeURIComponent(s.id) + "&tf=" + encodeURIComponent(opts.getTf()) + "&" + query())
        .then(function (r) { if (!r.ok) throw new Error(r.statusText); return r.json(); })
        .then(function (d) {
          ST.busy = false;
          if (key === ST.key) { d.session = s.id; ST.data = d; overlay.request(); render(); }
          if (ST.queued) { ST.queued = false; tick(true); }
        })
        .catch(function () { ST.busy = false; if (ST.queued) { ST.queued = false; tick(true); } });
    }
    setInterval(tick, 500);

    // ---- drawing ----

    function draw(ctx, size) {
      var d = ST.data;
      if (!d || !d.at || d.problem) return;
      var T = C.tokens();
      var col = { liq: cssVar("--s4"), smc: cssVar("--s7"), fvg: cssVar("--s5"), pa: cssVar("--s2"), ema: cssVar("--s3") };
      var W = size.width;
      var font = "10px " + (cssVar("--mono") || "monospace");
      ctx.save();
      ctx.font = font;
      ctx.textBaseline = "middle";

      function x(t) { var v = C.timeToX(t); return v === null ? null : Math.max(-20, Math.min(W + 20, v)); }
      function y(p) { return C.priceToY(p); }
      function text(s, tx, ty, color, align) {
        ctx.textAlign = align || "left";
        var w = ctx.measureText(s).width + 6;
        var bx = align === "right" ? tx - w : align === "center" ? tx - w / 2 : tx;
        ctx.fillStyle = rgba(T.surface || "#101010", 0.75);
        ctx.fillRect(bx, ty - 7, w, 14);
        ctx.fillStyle = color;
        ctx.fillText(s, align === "right" ? tx - 3 : align === "center" ? tx : tx + 3, ty);
        ctx.textAlign = "left";
      }
      function hline(p, x0, x1, color, dash, width) {
        var yy = y(p);
        if (yy === null || x0 === null || x1 === null) return null;
        ctx.strokeStyle = color; ctx.lineWidth = width || 1; ctx.setLineDash(dash || []);
        ctx.beginPath(); ctx.moveTo(x0, Math.round(yy) + 0.5); ctx.lineTo(x1, Math.round(yy) + 0.5); ctx.stroke();
        ctx.setLineDash([]);
        return yy;
      }

      // volume profile, behind everything: rows from the right edge
      var pf = d.profile;
      if (pf && cfg.on.profile) {
        var maxW = Math.min(120, W * 0.16);
        for (var i = 0; i < pf.bins.length; i++) {
          var lo = pf.base + i * pf.step, hi = lo + pf.step;
          var y0 = y(hi), y1 = y(lo);
          if (y0 === null || y1 === null) continue;
          var inside = lo >= pf.val - 1e-9 && hi <= pf.vah + 1e-9;
          ctx.fillStyle = rgba(T.muted, inside ? 0.2 : 0.09);
          ctx.fillRect(W - pf.bins[i] * maxW, Math.min(y0, y1) + 0.5, pf.bins[i] * maxW, Math.max(1, Math.abs(y1 - y0) - 1));
        }
        var xs = x(pf.start);
        if (xs !== null) {
          hline(pf.poc, xs, W, rgba(T.text, 0.55), [], 1);
          hline(pf.vah, xs, W, rgba(T.muted, 0.6), [2, 3], 1);
          hline(pf.val, xs, W, rgba(T.muted, 0.6), [2, 3], 1);
          var yp = y(pf.poc);
          if (yp !== null) text("POC", Math.max(xs, 4), yp, T.muted);
        }
      }

      // zones, order blocks, gaps: the three nearest price on each side, and
      // ones that ended in the last four hours (faint)
      var ref = lastPrice(d);
      var recent = d.at - 4 * 3600;
      var areas = (d.areas || []).filter(function (a) { return a.until && a.until >= recent; });
      ["sell", "buy"].forEach(function (side) {
        areas = areas.concat((d.areas || []).filter(function (a) { return !a.until && a.side === side; })
          .sort(function (p, q) { return side === "sell" ? (p.lo - ref) - (q.lo - ref) : (ref - p.hi) - (ref - q.hi); })
          .filter(function (a) { return side === "sell" ? a.hi >= ref : a.lo <= ref; })
          .slice(0, 3));
      });
      areas.forEach(function (a) {
        var x0 = x(a.origin), x1 = a.until ? x(a.until) : W;
        var y0 = y(a.hi), y1 = y(a.lo);
        if (x0 === null || x1 === null || y0 === null || y1 === null || x1 < 0) return;
        var c = a.kind === "supply" ? T.down : a.kind === "demand" ? T.up : a.kind === "fvg" ? col.fvg : col.smc;
        var ended = !!a.until || a.visits > 2;  // a zone past its second return is used up
        ctx.fillStyle = rgba(c, ended ? 0.05 : a.kind === "fvg" ? 0.13 : 0.11);
        ctx.fillRect(x0, Math.min(y0, y1), x1 - x0, Math.max(1, Math.abs(y1 - y0)));
        ctx.strokeStyle = rgba(c, ended ? 0.2 : 0.55);
        ctx.lineWidth = 1;
        ctx.setLineDash(a.kind === "ob" ? [4, 3] : []);
        ctx.strokeRect(x0 + 0.5, Math.min(y0, y1) + 0.5, x1 - x0, Math.max(1, Math.abs(y1 - y0)));
        ctx.setLineDash([]);
        if (!a.until) {
          var name = a.kind === "supply" ? "Supply" : a.kind === "demand" ? "Demand" : a.kind === "fvg" ? "FVG" : "OB";
          if (a.kind === "supply" || a.kind === "demand") name += " · " + (a.visits ? a.visits + (a.visits === 1 ? " visit" : " visits") : "fresh");
          text(name, Math.max(x0, 2) + 2, Math.min(y0, y1) + 8, c);
        }
      });

      // liquidity levels: active ones, and ones that ended in the last four hours
      (d.levels || []).filter(function (l) { return !l.until || l.until >= recent; }).forEach(function (l) {
        var x0 = x(l.origin), x1 = l.until ? x(l.until) : W;
        if (x0 === null || x1 === null || x1 < 0) return;
        var ended = !!l.until;
        var yy = hline(l.lo, x0, x1, rgba(col.liq, ended ? 0.35 : 0.9), [5, 4], 1);
        if (yy !== null && !ended) {
          var short = { pdh: "PDH", pdl: "PDL", eqh: "EQH", eql: "EQL", rh: "20D high", rl: "20D low" }[l.kind] || l.label;
          text(short + " " + l.lo.toFixed(d.digits), Math.min(x1, W - 170) - 4, yy + (l.side === "sell" ? -8 : 8), col.liq, "right");
        }
      });

      // daily EMA, as steps: each day's value is the one the filter used
      if (cfg.on.ema && d.ema && d.ema.length) {
        ctx.strokeStyle = col.ema; ctx.lineWidth = 2;
        ctx.beginPath();
        var started = false;
        d.ema.forEach(function (s, k) {
          var x0 = x(s.start), x1 = k === d.ema.length - 1 ? W : x(s.end), yy = y(s.value);
          if (x0 === null || x1 === null || yy === null || x1 < 0) return;
          if (!started) { ctx.moveTo(x0, yy); started = true; } else ctx.lineTo(x0, yy);
          ctx.lineTo(x1, yy);
        });
        ctx.stroke();
        var last = d.ema[d.ema.length - 1], yl = y(last.value);
        if (yl !== null) text("EMA 20 D", Math.max(x(last.start) || 0, 4) + 2, yl - 9, col.ema);
      }

      // breaks of structure: the latest four
      (d.breaks || []).slice(-4).forEach(function (b) {
        var x0 = x(b.swing), x1 = x(b.time);
        if (x0 === null || x1 === null || x1 < 0) return;
        var yy = hline(b.level, x0, x1, rgba(col.smc, 0.85), [2, 2], 1);
        if (yy !== null) text(b.kind, (x0 + x1) / 2, yy + (b.side === "buy" ? -8 : 8), col.smc, "center");
      });

      // sweeps: through the level and back; a break went through for good
      (d.sweeps || []).forEach(function (w) {
        var xm = x(w.start + 450), yy = y(w.extreme);
        if (xm === null || yy === null || xm < 0) return;
        var up = w.side === "sell";  // a high was swept: price went up through it
        ctx.fillStyle = w.broke ? rgba(col.liq, 0.5) : col.liq;
        ctx.beginPath();
        var dy = up ? -6 : 6;
        ctx.moveTo(xm, yy + dy * 0.2); ctx.lineTo(xm - 5, yy + dy * 1.6); ctx.lineTo(xm + 5, yy + dy * 1.6); ctx.closePath();
        if (w.broke) { ctx.strokeStyle = col.liq; ctx.lineWidth = 1; ctx.stroke(); } else ctx.fill();
      });

      // absorption: busy for the time of day, little progress
      if (cfg.on.price_action) {
        ctx.strokeStyle = col.pa; ctx.lineWidth = 1.5;
        (d.marks || []).forEach(function (m) {
          var xm = x(m.time + (d.tf_seconds || 0) / 2), yy = y(m.price);
          if (xm === null || yy === null || xm < 0) return;
          ctx.beginPath(); ctx.arc(xm, yy + (m.above ? -7 : 7), 3, 0, Math.PI * 2); ctx.stroke();
        });
      }

      // setups: entry, stop and target from the failure's close until the end
      (d.setups || []).forEach(function (s) {
        if (s.status === "skipped" && !cfg.skipped) return;
        var x0 = x(s.time), x1 = s.closed ? x(s.closed) : (s.status === "expired" ? x(s.time + 3600) : W);
        var ye = y(s.entry), ys = y(s.stop), yt = y(s.target);
        if (x0 === null || x1 === null || ye === null || ys === null || yt === null || x1 < 0) return;
        if (x1 - x0 < 6) x1 = x0 + 6;
        var faint = s.status === "skipped" || s.status === "expired";
        ctx.fillStyle = rgba(T.down, faint ? 0.05 : 0.14);
        ctx.fillRect(x0, Math.min(ye, ys), x1 - x0, Math.abs(ys - ye));
        ctx.fillStyle = rgba(T.up, faint ? 0.05 : 0.14);
        ctx.fillRect(x0, Math.min(ye, yt), x1 - x0, Math.abs(yt - ye));
        hline(s.entry, x0, x1, faint ? rgba(T.text, 0.4) : T.text, faint ? [3, 3] : [], 1.5);
        var word = (s.side === "sell" ? "Sell" : "Buy") + (s.market ? "" : " limit") + " · " + STATUS[s.status] +
          (s.pips !== null && s.pips !== undefined ? " " + (s.pips > 0 ? "+" : "") + s.pips : "");
        text(word, Math.max(x0, 2), s.side === "sell" ? Math.min(ye, ys) - 9 : Math.max(ye, ys) + 9,
          faint ? T.muted : s.status === "won" ? T.up : s.status === "lost" ? T.down : T.text);
      });
      ctx.restore();
    }

    // The price at the data's clock: the server's, else the last candle's.
    function lastPrice(d) {
      if (d.price) return d.price;
      var q = opts.getQuote && opts.getQuote();
      return q ? q.bid : 0;
    }

    // ---- panel ----

    function buildConcepts() {
      $("stConcepts").innerHTML = CONCEPTS.map(function (c) {
        return "<label class='strow' title='" + esc(c.what) + "'><input type='checkbox' data-c='" + c.key + "'" + (cfg.on[c.key] ? " checked" : "") + ">" +
          "<i class='sw " + (c.key === "zones" ? "sw2" : "") + "' style='--c:var(" + c.color + ")'></i>" +
          "<span><b>" + esc(c.name) + "</b><small>" + esc(c.q) + "</small></span></label>";
      }).join("");
      $("stConcepts").querySelectorAll("input[data-c]").forEach(function (box) {
        box.onchange = function () { cfg.on[box.dataset.c] = box.checked; changed(); };
      });
      $("stTarget").value = cfg.target;
      $("stMaxStop").value = cfg.maxStop;
      $("stProfile").value = cfg.profile;
      $("stSkipped").checked = cfg.skipped;
    }
    function changed() { save(); buildConcepts(); tick(true); overlay.request(); render(); }
    $("stAll").onclick = function () { CONCEPTS.forEach(function (c) { cfg.on[c.key] = true; }); changed(); };
    $("stNone").onclick = function () { CONCEPTS.forEach(function (c) { cfg.on[c.key] = false; }); changed(); };
    $("stTarget").onchange = function () { var v = parseFloat(this.value); if (v > 0) cfg.target = v; changed(); };
    $("stMaxStop").onchange = function () { var v = parseFloat(this.value); if (v > 0) cfg.maxStop = v; changed(); };
    $("stProfile").onchange = function () { cfg.profile = this.value; changed(); };
    $("stSkipped").onchange = function () { cfg.skipped = this.checked; changed(); };
    buildConcepts();

    function mark(ok) { return ok === null ? "<span class='stmark'>·</span>" : ok ? "<span class='stmark ok'>✓</span>" : "<span class='stmark no'>✗</span>"; }
    function pips(v) { return v === null || v === undefined ? "" : Number(v).toFixed(1).replace(/\.0$/, "") + " pips"; }
    function price(p) { return Number(p).toFixed(ST.data ? ST.data.digits : 5); }
    function when(t) { return opts.fmtTime(t * 1000); }

    function stepRow(n, name, ok, html) {
      return "<li><span class='stn'>" + n + "</span><div><div class='sth'>" + mark(ok) + "<b>" + name + "</b></div><div class='stt'>" + html + "</div></div></li>";
    }

    function render() {
      if (!$("stNow")) return;
      var d = ST.data;
      var s = opts.getSession();
      if (!anyOn()) {
        $("stWhen").textContent = "";
        $("stNow").innerHTML = "<li class='muted'>No strategy: every concept is off. Tick one above, or <b>All six</b>.</li>";
        $("stSetups").innerHTML = ""; $("stSummary").textContent = "";
        return;
      }
      if (!d) {
        $("stWhen").textContent = "";
        $("stNow").innerHTML = "<li class='muted'>" + (s && s.id ? "Working it out…" : "Open a session first.") + "</li>";
        $("stSetups").innerHTML = ""; $("stSummary").textContent = "";
        return;
      }
      $("stWhen").textContent = "· " + (d.live ? "now" : "at " + when(d.at));
      if (d.problem) { $("stNow").innerHTML = "<li class='muted'>" + esc(d.problem) + "</li>"; $("stSetups").innerHTML = ""; return; }
      var n = d.now || {};
      var rows = [];
      // 1 side
      var sd = n.side || {};
      var sides = (sd.sell ? "Sells" : "") + (sd.sell && sd.buy ? " and buys" : sd.buy ? "Buys" : "") + (sd.sell || sd.buy ? " allowed" : "No side");
      rows.push(stepRow(1, "Side · " + esc(sides), (cfg.on.ema || cfg.on.smc) ? !!(sd.sell !== sd.buy) : null, esc(sd.text || "")));
      // 2 levels and zones
      function lv(a) { return esc(a.label) + " " + (a.lo === a.hi ? price(a.lo) : price(a.lo) + "–" + price(a.hi)) + " <span class='faint'>(" + pips(a.pips) + ")</span>"; }
      var lvl = "";
      if (n.above && n.above.length) lvl += "<div>Above: " + n.above.map(lv).join(" · ") + "</div>";
      if (n.below && n.below.length) lvl += "<div>Below: " + n.below.map(lv).join(" · ") + "</div>";
      var levelsOn = cfg.on.liquidity || cfg.on.zones || cfg.on.smc;
      rows.push(stepRow(2, "Level and zone", levelsOn ? !!lvl : null, levelsOn ? (lvl || "Nothing active near price.") :
        "Turn on Liquidity sweep, Smart Money Concepts or Supply and demand: setups need a level or a zone."));
      // 3 room
      if (n.room) {
        var rs = n.room.sell, rb = n.room.buy;
        var r = "<div>" + mark(rs.open || (rs.pips !== null && rs.pips >= cfg.target)) + "Sell: " + (rs.pips !== null ? pips(rs.pips) + " " : "") + esc(rs.text) + "</div>" +
          "<div>" + mark(rb.open || (rb.pips !== null && rb.pips >= cfg.target)) + "Buy: " + (rb.pips !== null ? pips(rb.pips) + " " : "") + esc(rb.text) + "</div>";
        rows.push(stepRow(3, "Room for " + cfg.target + " pips", null, r));
      } else rows.push(stepRow(3, "Room", null, "Volume profile is off: room is not checked."));
      // 4 failure
      var rd = n.read || {};
      var f = "";
      (n.watching || []).slice(0, 3).forEach(function (w) {
        f += "<div><b>" + esc(w.label) + "</b> " + esc(w.what) + " at " + esc(when(w.time)) + ": waiting for a 1-minute close " +
          (w.side === "sell" ? "below the last small low" : "above the last small high") + ".</div>";
      });
      f += "<div class='faint'>Last 10 one-minute candles: up-bodies " + rd.up_body + " vs down-bodies " + rd.down_body + " pips; " +
        rd.up_rejections + " up and " + rd.down_rejections + " down candles rejected; " + esc(rd.reading) + ".</div>";
      rows.push(stepRow(4, "Failure", null, f));
      // 5 entry: the latest live setup
      var cur = (d.setups || []).filter(function (x) { return x.status === "waiting" || x.status === "active"; }).slice(-1)[0];
      rows.push(stepRow(5, "Entry", cur ? true : null, cur ? setupLine(cur) + " <button type='button' class='stfill' data-fill='" + cur.id + "'>Fill the ticket</button>" :
        "No setup open now."));
      $("stNow").innerHTML = rows.join("");
      renderSetups(d);
      bindFill();
    }

    function setupLine(x) {
      return "<b>" + (x.side === "sell" ? "Sell" : "Buy") + (x.market ? " at market " : " limit ") + price(x.entry) + "</b>, stop " + price(x.stop) +
        " (" + pips(x.risk_pips) + "), target " + price(x.target);
    }

    function renderSetups(d) {
      var all = d.setups || [];
      var real = all.filter(function (x) { return x.status !== "skipped"; });
      var won = real.filter(function (x) { return x.status === "won"; }).length;
      var lost = real.filter(function (x) { return x.status === "lost"; }).length;
      var net = real.reduce(function (a, x) { return a + (x.pips || 0); }, 0);
      $("stSummary").textContent = real.length ? "· " + real.length + " since yesterday's open: " + won + " won, " + lost + " lost, " +
        (net > 0 ? "+" : "") + net.toFixed(1) + " pips" + (all.length > real.length ? " (" + (all.length - real.length) + " skipped)" : "") :
        (all.length ? "· " + all.length + " skipped, none passed" : "· none yet");
      var shown = all.filter(function (x) { return cfg.skipped || x.status !== "skipped"; }).slice().reverse();
      $("stSetups").innerHTML = shown.length ? shown.map(function (x) {
        var checks = x.checks.map(function (c) {
          return "<li>" + mark(c.required ? c.ok : (c.ok ? true : null)) + "<b>" + esc(c.name) + "</b> " + esc(c.text) + "</li>";
        }).join("");
        return "<details class='stsetup " + x.status + "'><summary><span class='stst " + x.status + "'>" + STATUS[x.status] +
          (x.pips !== null && x.pips !== undefined ? " " + (x.pips > 0 ? "+" : "") + x.pips : "") + "</span>" +
          "<span class='mono'>" + esc(when(x.time)) + "</span> " + setupLine(x) +
          "<div class='faint'>" + esc(x.areas.join(" + ")) + "</div></summary>" +
          "<ul class='stchecks'>" + checks + "</ul>" +
          "<div class='row'><button type='button' data-goto='" + x.time + "'>Show on chart</button>" +
          (x.status === "waiting" || x.status === "active" ? "<button type='button' class='stfill' data-fill='" + x.id + "'>Fill the ticket</button>" : "") +
          "</div></details>";
      }).join("") : "<p class='tiny muted'>" + (all.length ? "Every setup was skipped by a check. Tick “Show skipped” to see why." :
        "No setup since yesterday's open: a level or zone reached, then a 1-minute close back through the last small low.") + "</p>";
      $("stSetups").querySelectorAll("[data-goto]").forEach(function (b) {
        b.onclick = function () { opts.goto(parseInt(b.dataset.goto, 10)); };
      });
    }

    function bindFill() {
      document.querySelectorAll("#stNow [data-fill], #stSetups [data-fill]").forEach(function (b) {
        b.onclick = function (e) {
          e.preventDefault();
          var x = (ST.data && ST.data.setups || []).filter(function (s) { return s.id === b.dataset.fill; })[0];
          if (x) opts.fillTicket(x);
        };
      });
    }

    return {
      data: function () { return ST.data; },
      refresh: function () { tick(true); },
      clear: function () { ST.key = ""; clear(); },
      redraw: function () { overlay.request(); },
      render: render
    };
  }

  window.SimStrategies = { create: create };
})();
