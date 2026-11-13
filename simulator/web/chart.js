/* Chart adapter (guide step 3.3).
 *
 * The only file that talks to TradingView Lightweight Charts. Everything else
 * calls these functions, so swapping to Advanced Charts later touches this
 * file alone:
 *
 *   setHistory(bars, tfSeconds)   replace all candles
 *   updateBars(bars)              apply forming / newly completed candles
 *   prependBars(bars)             older candles when scrolling back
 *   setQuote(quote)               the Ask line (the series shows the Bid)
 *   setLines(lines)               order / position lines, some draggable
 *   setMarkers(markers)           arrows for entries and exits
 *   attachOverlay(drawFn)         canvas callback used by drawings.js
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
      localization: { timeFormatter: timeLabel, priceFormatter: function (p) { return p.toFixed(5); } }
    });

    var series = chart.addCandlestickSeries({
      upColor: t.up, downColor: t.down, borderUpColor: t.up, borderDownColor: t.down,
      wickUpColor: t.up, wickDownColor: t.down,
      priceFormat: { type: "price", precision: 5, minMove: 0.00001 }
    });

    var times = [];      // candle start times on the chart, ascending
    var data = [];       // the candles themselves, same order
    var tfSeconds = 900;
    var askLine = null;
    var lines = {};      // id -> {spec, line}
    var dragHandlers = [];

    function strip(b) { return { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }; }

    // ---- candles ----

    function setHistory(bars, seconds) {
      tfSeconds = seconds || tfSeconds;
      data = bars.map(strip);
      times = data.map(function (b) { return b.time; });
      series.setData(data);
      if (overlay) overlay.request();
    }

    function updateBars(bars) {
      for (var i = 0; i < bars.length; i++) {
        var b = bars[i];
        var last = times.length ? times[times.length - 1] : -Infinity;
        if (b.time < last) continue; // never rewrite the past
        var c = strip(b);
        series.update(c);
        if (b.time > last) { times.push(b.time); data.push(c); } else data[data.length - 1] = c;
      }
    }

    function prependBars(bars) {
      if (!bars.length) return 0;
      var first = times.length ? times[0] : Infinity;
      var older = bars.filter(function (b) { return b.time < first; });
      if (!older.length) return 0;
      var range = chart.timeScale().getVisibleLogicalRange();
      data = older.map(strip).concat(data);
      times = data.map(function (b) { return b.time; });
      series.setData(data);
      if (range) chart.timeScale().setVisibleLogicalRange({ from: range.from + older.length, to: range.to + older.length });
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

    // ---- quote and lines ----

    function setQuote(q) {
      if (!q) return;
      if (!askLine) {
        askLine = series.createPriceLine({ price: q.ask, color: tokens().faint, lineWidth: 1,
          lineStyle: LWC.LineStyle.Dotted, axisLabelVisible: true, title: "Ask" });
      } else {
        askLine.applyOptions({ price: q.ask });
      }
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

    function addPointerHandler(h) { dragHandlers.push(h); }

    // ---- overlay primitive (drawings) ----

    var overlay = null;
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
      overlay = { request: function () { requestUpdate(); } };
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
      if (askLine) askLine.applyOptions({ color: n.faint });
      if (overlay) overlay.request();
    }

    function setHideDates(on) {
      hideDates = !!on;
      chart.applyOptions({ localization: { timeFormatter: timeLabel }, timeScale: { tickMarkFormatter: tickLabel } });
    }

    return {
      chart: chart,
      series: series,
      setHistory: setHistory,
      updateBars: updateBars,
      prependBars: prependBars,
      setQuote: setQuote,
      setLines: setLines,
      setMarkers: setMarkers,
      attachOverlay: attachOverlay,
      addPointerHandler: addPointerHandler,
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
