/* Chart adapter (guide step 3.3).
 *
 * The only file that talks to TradingView Lightweight Charts. Everything else
 * calls these functions, so swapping to Advanced Charts later touches this
 * file alone:
 *
 *   setHistory(bars, tfSeconds)   replace all candles
 *   updateBars(bars)              apply forming / newly completed candles
 *   prependBars(bars)             older candles when scrolling back
 *   setQuote(quote)               Bid and Ask lines and labels (candles are Bid)
 *   setLines(lines)               order / position lines, some draggable
 *   setMarkers(markers)           arrows for entries and exits
 *   showIndicator(on)             the strength pane under the chart (same candles)
 *   setInstrument(inst)           price precision of the instrument
 *   attachOverlay(drawFn)         canvas callback (drawings.js, news.js)
 *   screenshot()                  PNG data URL of the chart
 *
 * plus the coordinate helpers drawings need: time <-> x and price <-> y,
 * which also work between candles, across timeframes and to the right of
 * the last candle.
 */
(function () {
  "use strict";
  var LWC = window.LightweightCharts;

  function tokens() {
    var s = getComputedStyle(document.documentElement);
    function v(n) { return s.getPropertyValue(n).trim(); }
    return { text: v("--text"), muted: v("--muted"), grid: v("--border"), up: v("--pos"), down: v("--neg"),
             accent: v("--accent"), faint: v("--faint"), draw: v("--draw"), surface: v("--surface") };
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  function createSimChart(el, options) {
    options = options || {};
    var t = tokens();
    var hideDates = !!options.hideDates;

    var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    // type: 0 year, 1 month, 2 day of month, 3 time, 4 time with seconds
    function tickLabel(ts, type) {
      var d = new Date(ts * 1000);
      var hm = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
      if (hideDates) return type <= 2 ? DAYS[d.getUTCDay()] : hm;
      if (type === 0) return String(d.getUTCFullYear());
      if (type === 1) return MONTHS[d.getUTCMonth()];
      if (type === 2) return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()];
      return hm;
    }

    function timeLabel(ts) {
      var d = new Date(ts * 1000);
      var hm = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
      if (hideDates) return DAYS[d.getUTCDay()] + " " + hm;
      return d.toISOString().slice(0, 10) + " " + hm + " UTC";
    }

    var chart = LWC.createChart(el, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: t.muted, fontSize: 11 },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderColor: t.grid },
      timeScale: {
        borderColor: t.grid, timeVisible: true, secondsVisible: false, rightOffset: 12,
        tickMarkFormatter: tickLabel
      },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      localization: { timeFormatter: timeLabel, priceFormatter: function (p) { return p.toFixed(digits); } }
    });
    var digits = 5;

    // No last-price label or line of the series' own: it is the Bid, but
    // coloured by the candle's direction, so it turned blue like the BUY
    // side whenever the candle was up. Bid and Ask get their own (setQuote).
    var series = chart.addCandlestickSeries({
      upColor: t.up, downColor: t.down, borderUpColor: t.up, borderDownColor: t.down,
      wickUpColor: t.up, wickDownColor: t.down,
      lastValueVisible: false, priceLineVisible: false,
      priceFormat: { type: "price", precision: 5, minMove: 0.00001 }
    });
    // Candles keep the top 78% of the pane; tick volume sits in the bottom
    // 18% on its own hidden scale, so the two never overlap.
    series.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
    var volume = chart.addHistogramSeries({
      priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });

    function hexA(hex, a) {
      var h = (hex || "#888").replace("#", "");
      if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      return "rgba(" + parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4, 6), 16) + "," + a + ")";
    }
    var volColors = { up: hexA(t.up, 0.35), down: hexA(t.down, 0.35) };
    function volBar(b) {
      return { time: b.time, value: b.volume || 0, color: b.close >= b.open ? volColors.up : volColors.down };
    }

    function setInstrument(inst) {
      digits = inst.digits;
      var step = Math.pow(10, -digits);
      series.applyOptions({ priceFormat: { type: "price", precision: digits, minMove: step } });
      chart.applyOptions({ localization: { timeFormatter: timeLabel, priceFormatter: function (p) { return p.toFixed(digits); } } });
    }

    var times = [];      // candle start times on the chart, ascending
    var data = [];       // the candles themselves, same order
    var vols = [];       // tick volume per candle, same order
    var tfSeconds = 900;
    var lines = {};      // id -> {spec, line}
    var dragHandlers = [];

    function strip(b) { return { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }; }

    // ---- strength indicator: a pane under the chart ----
    //
    // A second chart holding exactly the main chart's candles (a value or a
    // gap for each), so the two scroll, zoom and point at the same moment.
    // Each candle arrives with `s`, its strength values in the order of
    // `indicatorKeys`, and `w`, matches won and played per currency.

    var IND_KEYS = options.indicatorKeys || [];
    var strengths = [];  // {s, w} or null per candle, same order as `data`
    var ind = null, indZero = null, indLines = [], indShown = false;
    function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
    function pct(v) { return (Math.abs(v) < 0.005 ? "" : v > 0 ? "+" : "−") + Math.abs(v).toFixed(2) + "%"; }
    if (options.indicatorEl) {
      ind = LWC.createChart(options.indicatorEl, {
        autoSize: true,
        layout: { background: { color: "transparent" }, textColor: t.muted, fontSize: 11 },
        grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
        rightPriceScale: { borderColor: t.grid, scaleMargins: { top: 0.2, bottom: 0.08 } },
        timeScale: { visible: false, rightOffset: 12 },
        crosshair: { mode: LWC.CrosshairMode.Normal },
        localization: { timeFormatter: timeLabel, priceFormatter: pct }
      });
      // Zero: the day's open. Always in view, and where the crosshair sits.
      indZero = ind.addLineSeries({ color: t.faint, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed,
        lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      indLines = IND_KEYS.map(function (k) {
        return ind.addLineSeries({ color: cssVar(k.color), lineWidth: 2, title: k.label, visible: !!k.on,
          lineStyle: k.dash ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid,
          priceLineVisible: false, crosshairMarkerVisible: false, priceFormat: { type: "custom", formatter: pct, minMove: 0.001 } });
      });
    }
    function strengthOf(b) { return b.s ? { s: b.s, w: b.w || [] } : null; }
    function indPoint(i, k) {
      var st = strengths[i], v = st ? st.s[k] : null;
      return v === null || v === undefined ? { time: times[i] } : { time: times[i], value: v };
    }
    function indSetAll() {
      if (!ind || !indShown) return;
      indZero.setData(times.map(function (tm) { return { time: tm, value: 0 }; }));
      indLines.forEach(function (line, k) { line.setData(times.map(function (_tm, i) { return indPoint(i, k); })); });
      var r = chart.timeScale().getVisibleLogicalRange();
      if (r) ind.timeScale().setVisibleLogicalRange(r);
      requestAnimationFrame(alignWidths);
    }
    function indUpdateLast() {
      if (!ind || !indShown || !times.length) return;
      var i = times.length - 1;
      indZero.update({ time: times[i], value: 0 });
      indLines.forEach(function (line, k) { line.update(indPoint(i, k)); });
    }
    // Both price scales as wide as the wider, so the candles line up.
    function alignWidths() {
      if (!ind || !indShown) return;
      var a = chart.priceScale("right").width(), b = ind.priceScale("right").width(), w = Math.max(a, b);
      if (w && a < w) chart.applyOptions({ rightPriceScale: { minimumWidth: w } });
      if (w && b < w) ind.applyOptions({ rightPriceScale: { minimumWidth: w } });
    }
    var syncing = false;
    function follow(target) {
      return function (r) {
        if (!r || syncing || !ind || !indShown) return;
        syncing = true;
        try { target.timeScale().setVisibleLogicalRange(r); } finally { syncing = false; }
        alignWidths();
      };
    }
    function indexOf(time) {
      var lo = 0, hi = times.length - 1;
      while (lo <= hi) { var mid = (lo + hi) >> 1; if (times[mid] < time) lo = mid + 1; else if (times[mid] > time) hi = mid - 1; else return mid; }
      return -1;
    }
    // The legend shows the candle under the crosshair, else the latest.
    var hoverAt = -1;
    function hovered(i) { hoverAt = i; legend(); }
    function legend() {
      if (options.onIndicatorHover) options.onIndicatorHover(hoverAt >= 0 ? strengths[hoverAt] : lastStrength(), hoverAt >= 0);
    }
    // The latest candle with a value: the forming one has none until a
    // minute of it has closed.
    function lastStrength() {
      for (var i = strengths.length - 1; i >= Math.max(0, strengths.length - 4); i--) if (strengths[i]) return strengths[i];
      return null;
    }
    // Values sent again for candles already on the chart (see send_strength).
    function setStrengths(bars) {
      var changed = false;
      bars.forEach(function (b) {
        var i = indexOf(b.time);
        if (i >= 0) { strengths[i] = strengthOf(b); changed = true; }
      });
      if (changed) { indSetAll(); if (hoverAt < 0) legend(); }
    }
    if (ind) {
      chart.timeScale().subscribeVisibleLogicalRangeChange(follow(ind));
      ind.timeScale().subscribeVisibleLogicalRangeChange(follow(chart));
      chart.subscribeCrosshairMove(function (p) {
        if (!indShown) return;
        if (p.time === undefined) { ind.clearCrosshairPosition(); hovered(-1); return; }
        ind.setCrosshairPosition(0, p.time, indZero);
        hovered(indexOf(p.time));
      });
      ind.subscribeCrosshairMove(function (p) { if (indShown && p.sourceEvent !== undefined) hovered(p.time === undefined ? -1 : indexOf(p.time)); });
    }
    function showIndicator(on) {
      indShown = !!(ind && on);
      if (indShown) requestAnimationFrame(function () { indSetAll(); hovered(-1); });
    }
    function setIndicatorKeys(on) {
      IND_KEYS.forEach(function (k, i) { k.on = !!on[k.key]; if (indLines[i]) indLines[i].applyOptions({ visible: k.on }); });
    }

    // ---- candles ----

    function setHistory(bars, seconds) {
      tfSeconds = seconds || tfSeconds;
      data = bars.map(strip);
      vols = bars.map(volBar);
      strengths = bars.map(strengthOf);
      times = data.map(function (b) { return b.time; });
      series.setData(data);
      volume.setData(vols);
      indSetAll();
      if (indShown) hovered(-1);
      requestOverlays();
    }

    function updateBars(bars) {
      for (var i = 0; i < bars.length; i++) {
        var b = bars[i];
        var last = times.length ? times[times.length - 1] : -Infinity;
        if (b.time < last) continue; // never rewrite the past
        var c = strip(b), v = volBar(b);
        series.update(c);
        volume.update(v);
        if (b.time > last) { times.push(b.time); data.push(c); vols.push(v); strengths.push(strengthOf(b)); }
        else { data[data.length - 1] = c; vols[vols.length - 1] = v; strengths[strengths.length - 1] = strengthOf(b); }
        indUpdateLast();
      }
      if (bars.length && indShown && hoverAt < 0) legend();
    }

    function prependBars(bars) {
      if (!bars.length) return 0;
      var first = times.length ? times[0] : Infinity;
      var older = bars.filter(function (b) { return b.time < first; });
      if (!older.length) return 0;
      var range = chart.timeScale().getVisibleLogicalRange();
      data = older.map(strip).concat(data);
      vols = older.map(volBar).concat(vols);
      strengths = older.map(strengthOf).concat(strengths);
      times = data.map(function (b) { return b.time; });
      series.setData(data);
      volume.setData(vols);
      if (range) chart.timeScale().setVisibleLogicalRange({ from: range.from + older.length, to: range.to + older.length });
      indSetAll();
      return older.length;
    }

    // ---- coordinates (drawings) ----

    function timeToLogical(ts) {
      var n = times.length;
      if (!n) return null;
      if (ts <= times[0]) return (ts - times[0]) / tfSeconds;
      if (ts >= times[n - 1]) return (n - 1) + (ts - times[n - 1]) / tfSeconds;
      var lo = 0, hi = n - 1;
      while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (times[mid] <= ts) lo = mid; else hi = mid; }
      // Inside a candle: proportional. Inside a gap (weekend): the next candle.
      return lo + Math.min((ts - times[lo]) / tfSeconds, 1);
    }

    function logicalToTime(l) {
      var n = times.length;
      if (!n) return null;
      if (l <= 0) return Math.round(times[0] + l * tfSeconds);
      if (l >= n - 1) return Math.round(times[n - 1] + (l - (n - 1)) * tfSeconds);
      var i = Math.floor(l), f = l - i;
      return Math.round(times[i] + f * Math.min(tfSeconds, times[i + 1] - times[i]));
    }

    function timeToX(ts) {
      var l = timeToLogical(ts);
      return l === null ? null : chart.timeScale().logicalToCoordinate(l);
    }
    function xToTime(x) {
      var l = chart.timeScale().coordinateToLogical(x);
      return l === null ? null : logicalToTime(l);
    }
    function priceToY(p) { return series.priceToCoordinate(p); }
    function yToPrice(y) { return series.coordinateToPrice(y); }
    function paneWidth() { return chart.timeScale().width(); }
    function paneHeight() { var h = el.clientHeight - chart.timeScale().height(); return h > 0 ? h : el.clientHeight; }

    // ---- quote: Bid and Ask ----
    //
    // Each has a line and a label on the price scale, always in the same
    // colours: Bid the SELL red and Ask the BUY blue, as on the order
    // buttons. The labels are placed here rather than by the library. It
    // spreads overlapping labels apart from the middle of the pane and breaks
    // ties by list order, so with Ask equal to Bid (a third of EUR/USD ticks
    // on a raw-spread account) the two swapped sides whenever the price
    // crossed the middle. Here each sits at its price, and when the two are
    // closer than a label's height they stack round the middle of the
    // spread: Ask above, Bid below, whatever the market does.

    var quote = null, bidLine = null, askLine = null;
    var quoteColors = { bid: t.down, ask: t.up };
    var LABEL_HEIGHT = 18;  // the price scale's labels at an 11 px font, and a little air

    function quoteLabels() {
      if (!quote) return null;
      var ya = series.priceToCoordinate(quote.ask), yb = series.priceToCoordinate(quote.bid);
      if (ya === null || yb === null) return null;
      var stacked = yb - ya < LABEL_HEIGHT;
      if (stacked) {
        var mid = (ya + yb) / 2;
        ya = mid - LABEL_HEIGHT / 2;
        yb = mid + LABEL_HEIGHT / 2;
      }
      return { ask: ya, bid: yb, stacked: stacked };
    }

    function quoteAxisView(side) {
      function y() { var at = quoteLabels(); return at ? at[side] : -1000; }
      return {
        coordinate: y,
        fixedCoordinate: y,
        text: function () { return quote ? quote[side].toFixed(digits) : ""; },
        textColor: function () { return "#ffffff"; },
        backColor: function () { return quoteColors[side]; },
        visible: function () { return quote !== null; },
        // The tick marks the exact price; a stacked label is beside it.
        tickVisible: function () { var at = quoteLabels(); return !!at && !at.stacked; }
      };
    }
    var quoteViews = [quoteAxisView("ask"), quoteAxisView("bid")];
    // "Ask" and "Bid" tags at the pane's right edge beside the labels, so the
    // two are named, not told apart by colour alone.
    var tagRenderer = {
      draw: function (target) {
        target.useMediaCoordinateSpace(function (scope) {
          var at = quoteLabels();
          if (!at) return;
          var ctx = scope.context, w = scope.mediaSize.width, h = scope.mediaSize.height;
          ctx.font = "600 10px -apple-system, 'Segoe UI', sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          [["ask", "Ask"], ["bid", "Bid"]].forEach(function (s) {
            var y = at[s[0]];
            if (y < -10 || y > h + 10) return;
            ctx.fillStyle = quoteColors[s[0]];
            ctx.beginPath();
            ctx.roundRect(w - 29, y - 8, 27, 16, 3);
            ctx.fill();
            ctx.fillStyle = "#ffffff";
            ctx.fillText(s[1], w - 15.5, y + 0.5);
          });
        });
      }
    };
    var tagView = { renderer: function () { return tagRenderer; }, zOrder: function () { return "top"; } };
    var quoteRedraw = function () {};
    series.attachPrimitive({
      attached: function (p) { quoteRedraw = p.requestUpdate; },
      detached: function () {},
      updateAllViews: function () {},
      priceAxisViews: function () { return quoteViews; },
      paneViews: function () { return [tagView]; }
    });

    function setQuote(q) {
      if (!q) return;
      quote = { bid: q.bid, ask: q.ask };
      var style = { lineWidth: 1, lineStyle: LWC.LineStyle.Dotted, axisLabelVisible: false };
      if (!bidLine) {
        bidLine = series.createPriceLine(Object.assign({ price: q.bid, color: quoteColors.bid, title: "Bid" }, style));
        askLine = series.createPriceLine(Object.assign({ price: q.ask, color: quoteColors.ask, title: "Ask" }, style));
      } else {
        bidLine.applyOptions({ price: q.bid });
        askLine.applyOptions({ price: q.ask });
      }
      quoteRedraw();
    }

    var STYLE = { solid: 0, dotted: 1, dashed: 2 };

    // lines: [{id, price, color, title, style, draggable, onDrop(price)}]
    function setLines(specs) {
      var keep = {};
      specs.forEach(function (s) {
        keep[s.id] = true;
        var opts = { price: s.price, color: s.color, lineWidth: s.width || 1,
          lineStyle: STYLE[s.style || "solid"], axisLabelVisible: true, title: s.title || "" };
        var cur = lines[s.id];
        if (cur && cur.dragging) { cur.spec = Object.assign({}, s, { price: cur.spec.price }); return; }
        if (cur) { cur.line.applyOptions(opts); cur.spec = s; }
        else lines[s.id] = { spec: s, line: series.createPriceLine(opts) };
      });
      Object.keys(lines).forEach(function (id) {
        if (!keep[id] && !lines[id].dragging) { series.removePriceLine(lines[id].line); delete lines[id]; }
      });
    }

    function setMarkers(markers) { series.setMarkers(markers || []); }

    // ---- pointer routing: order lines first, then drawings ----

    function local(e) {
      var r = el.getBoundingClientRect();
      var x = e.clientX - r.left, y = e.clientY - r.top;
      return { x: x, y: y, time: xToTime(x), price: yToPrice(y), inPane: x >= 0 && x <= paneWidth() && y >= 0 && y <= paneHeight() };
    }

    var lineHandler = {
      hit: function (pt) {
        var best = null, bestD = 6;
        Object.keys(lines).forEach(function (id) {
          var l = lines[id];
          if (!l.spec.draggable) return;
          var y = priceToY(l.spec.price);
          if (y === null) return;
          var d = Math.abs(y - pt.y);
          if (d < bestD) { bestD = d; best = l; }
        });
        return best;
      },
      hover: function (pt) { return this.hit(pt) ? "ns-resize" : null; },
      down: function (pt) {
        var l = this.hit(pt);
        if (!l) return false;
        l.dragging = true; this.active = l;
        return true;
      },
      move: function (pt) {
        var l = this.active;
        if (!l || pt.price === null) return;
        var p = Math.round(pt.price * 1e5) / 1e5;
        l.spec = Object.assign({}, l.spec, { price: p });
        l.line.applyOptions({ price: p });
      },
      up: function () {
        var l = this.active;
        this.active = null;
        if (!l) return;
        l.dragging = false;
        if (l.spec.onDrop) l.spec.onDrop(l.spec.price);
      }
    };
    dragHandlers.push(lineHandler);

    var captured = null;
    function setChartInteractive(on) { chart.applyOptions({ handleScroll: on, handleScale: on }); }

    // A handler that is mid-task (a drawing tool armed) is asked first, so
    // placing a point on top of an order line places the point.
    function ordered() {
      var busy = dragHandlers.filter(function (h) { return h.busy && h.busy(); });
      return busy.length ? busy.concat(dragHandlers.filter(function (h) { return busy.indexOf(h) < 0; })) : dragHandlers;
    }

    function onDown(e) {
      if (e.button !== undefined && e.button !== 0) return;
      var pt = local(e);
      if (!pt.inPane) return;
      var hs = ordered();
      for (var i = 0; i < hs.length; i++) {
        if (hs[i].down(pt, e)) {
          captured = hs[i];
          setChartInteractive(false);
          e.preventDefault(); e.stopPropagation();
          return;
        }
      }
    }
    function onMove(e) {
      var pt = local(e);
      if (captured) { captured.move(pt, e); e.preventDefault(); return; }
      var cursor = "";
      if (pt.inPane) {
        var hs = ordered();
        for (var i = 0; i < hs.length; i++) {
          var c = hs[i].hover && hs[i].hover(pt);
          if (c) { cursor = c; break; }
        }
      }
      el.style.cursor = cursor;
    }
    function onUp(e) {
      if (!captured) return;
      var h = captured; captured = null;
      h.up(local(e), e);
      setChartInteractive(true);
    }
    // Capture phase on the container: runs before the chart's own handlers,
    // so a drag on a line or drawing never also pans the chart.
    el.addEventListener("pointerdown", onDown, true);
    el.addEventListener("mousedown", function (e) { if (captured) { e.preventDefault(); e.stopPropagation(); } }, true);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);

    // `first`: asked before the others (the news dots, which are small
    // targets that should win over a drawing underneath them).
    function addPointerHandler(h, first) { if (first) dragHandlers.unshift(h); else dragHandlers.push(h); }

    // ---- overlay primitives (drawings, news) ----

    var overlays = [];
    function requestOverlays() { overlays.forEach(function (o) { o.request(); }); }
    function attachOverlay(drawFn) {
      var requestUpdate = function () {};
      var renderer = {
        draw: function (target) {
          target.useMediaCoordinateSpace(function (scope) { drawFn(scope.context, scope.mediaSize); });
        }
      };
      var view = { renderer: function () { return renderer; }, zOrder: function () { return "top"; } };
      series.attachPrimitive({
        attached: function (p) { requestUpdate = p.requestUpdate; },
        detached: function () {},
        updateAllViews: function () {},
        paneViews: function () { return [view]; }
      });
      var overlay = { request: function () { requestUpdate(); } };
      overlays.push(overlay);
      return overlay;
    }

    // ---- theme ----

    function retheme() {
      var n = tokens();
      chart.applyOptions({
        layout: { textColor: n.muted },
        grid: { vertLines: { color: n.grid }, horzLines: { color: n.grid } },
        rightPriceScale: { borderColor: n.grid },
        timeScale: { borderColor: n.grid }
      });
      series.applyOptions({ upColor: n.up, downColor: n.down, borderUpColor: n.up, borderDownColor: n.down,
        wickUpColor: n.up, wickDownColor: n.down });
      volColors = { up: hexA(n.up, 0.35), down: hexA(n.down, 0.35) };
      vols = data.map(function (c, i) { return { time: c.time, value: vols[i] ? vols[i].value : 0, color: c.close >= c.open ? volColors.up : volColors.down }; });
      volume.setData(vols);
      quoteColors = { bid: n.down, ask: n.up };
      if (ind) {
        ind.applyOptions({ layout: { textColor: n.muted }, grid: { vertLines: { color: n.grid }, horzLines: { color: n.grid } },
          rightPriceScale: { borderColor: n.grid } });
        indZero.applyOptions({ color: n.faint });
        indLines.forEach(function (line, k) { line.applyOptions({ color: cssVar(IND_KEYS[k].color) }); });
      }
      if (bidLine) { bidLine.applyOptions({ color: n.down }); askLine.applyOptions({ color: n.up }); }
      quoteRedraw();
      requestOverlays();
    }

    function setHideDates(on) {
      hideDates = !!on;
      chart.applyOptions({ localization: { timeFormatter: timeLabel }, timeScale: { tickMarkFormatter: tickLabel } });
    }

    return {
      chart: chart,
      series: series,
      volume: volume,
      setInstrument: setInstrument,
      requestOverlays: requestOverlays,
      timeToLogical: timeToLogical,
      setHistory: setHistory,
      updateBars: updateBars,
      prependBars: prependBars,
      setQuote: setQuote,
      quoteLabels: quoteLabels,
      showIndicator: showIndicator,
      setStrengths: setStrengths,
      setIndicatorKeys: setIndicatorKeys,
      strengthAt: function (i) { return strengths[i] || null; },
      lastStrength: lastStrength,
      setLines: setLines,
      setMarkers: setMarkers,
      attachOverlay: attachOverlay,
      addPointerHandler: addPointerHandler,
      handlers: function () { return dragHandlers; },
      timeToX: timeToX, xToTime: xToTime, priceToY: priceToY, yToPrice: yToPrice,
      paneWidth: paneWidth, paneHeight: paneHeight,
      firstTime: function () { return times.length ? times[0] : null; },
      lastTime: function () { return times.length ? times[times.length - 1] : null; },
      barCount: function () { return times.length; },
      onRangeChange: function (cb) { chart.timeScale().subscribeVisibleLogicalRangeChange(cb); },
      scrollToEnd: function () { chart.timeScale().scrollToRealTime(); },
      screenshot: function () { return chart.takeScreenshot().toDataURL("image/png"); },
      retheme: retheme,
      tokens: tokens,
      setHideDates: setHideDates,
      timeLabel: timeLabel
    };
  }

  window.SimChart = { create: createSimChart, tokens: tokens };
})();
