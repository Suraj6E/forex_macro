/* Economic calendar on the chart.
 *
 * A dot per impact level per candle, along the bottom of the price pane:
 * red high, yellow medium, near-white low (the calendar's own rating). A
 * filled dot has been released at the replay's clock; a ring is scheduled
 * and still to come, drawn in the space right of the last candle. Clicking a
 * dot opens every event of that candle with its details.
 *
 * The server decides what is known at the clock: the schedule, forecast and
 * previous ahead of time, the actual only once released. This file only
 * draws what it is sent, and asks again when the clock passes an event.
 */
(function () {
  "use strict";

  var IMPACT = { 3: "High", 2: "Medium", 1: "Low" };
  var R = 4.5;          // dot radius
  var STEP = 11;        // vertical spacing of stacked dots
  var LS_KEY = "sim-news-impacts";

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function colors(theme) {
    var light = theme === "light";
    return {
      3: { fill: "#e5484d", stroke: light ? "#b4232a" : "#ff8a8d" },
      2: { fill: "#f2b705", stroke: light ? "#a87d00" : "#ffd75e" },
      1: { fill: light ? "#ffffff" : "#ececec", stroke: light ? "#8a8a8a" : "#ffffff" },
      ring: light ? "#fbfbfb" : "#101010"
    };
  }

  function create(C, opts) {
    var events = [];          // [{id, ms, ccy, impact, name, released}]
    var range = null;         // {start, end} ms fetched
    var nextPending = null;   // ms of the first unreleased event
    var fetchedAt = 0;        // wall clock of the last fetch
    var AHEAD = 7 * 86400000; // the schedule shown ahead of the clock (the server's limit)
    var STALE = 120000;       // fetched again this often: the calendar is updated in the background
    var spots = [];           // drawn dots, for hit testing: {x, y, group}
    var shown = loadImpacts();
    var loading = false, queued = false;
    var popup = document.getElementById("newsPop");
    var tip = document.getElementById("newsTip");

    function loadImpacts() {
      try {
        var v = JSON.parse(localStorage.getItem(LS_KEY) || "null");
        if (v && typeof v === "object") return { 1: v[1] !== false, 2: v[2] !== false, 3: v[3] !== false };
      } catch (e) { /* storage unavailable */ }
      return { 1: true, 2: true, 3: true };
    }
    function saveImpacts() { try { localStorage.setItem(LS_KEY, JSON.stringify(shown)); } catch (e) { /* ignore */ } }

    var overlay = C.attachOverlay(draw);

    // ---- data ----

    function reload() {
      var s = opts.getSession();
      var first = C.firstTime();
      if (!s || first === null) return;
      if (loading) { queued = true; return; }
      loading = true;
      var clock = opts.getClockMs() || Date.now();
      var start = first * 1000, end = clock + AHEAD;
      fetchedAt = Date.now();
      fetch("/api/calendar?session=" + encodeURIComponent(s.id) + "&start=" + start + "&end=" + end)
        .then(function (r) { return r.ok ? r.json() : { events: [] }; })
        .then(function (d) {
          events = d.events || [];
          range = { start: start, end: end, clock: d.clock_ms };
          nextPending = null;
          for (var i = 0; i < events.length; i++) if (!events[i].released) { nextPending = events[i].ms; break; }
          overlay.request();
          if (opts.onStatus) opts.onStatus(d.status, events);
        })
        .catch(function () { /* server restarting */ })
        .finally(function () {
          loading = false;
          if (queued) { queued = false; reload(); }
        });
    }

    // Called with every clock update: refetch when an event is released or
    // the clock nears the end of what was fetched.
    function onClock(ms) {
      if (!range) return;
      if ((nextPending !== null && ms >= nextPending) || ms > range.end - 86400000 || ms < (range.clock || 0) - 60000 ||
          Date.now() - fetchedAt > STALE) reload();
    }

    function clear() { events = []; range = null; nextPending = null; overlay.request(); hidePopup(); }

    // ---- drawing ----

    function groups() {
      var tf = opts.getTfSeconds();
      var by = {}, order = [];
      events.forEach(function (e) {
        if (!shown[e.impact]) return;
        var bar = Math.floor(e.ms / 1000 / tf) * tf;
        var g = by[bar];
        if (!g) { g = by[bar] = { bar: bar, events: [], impacts: {} }; order.push(g); }
        g.events.push(e);
        var k = e.impact;
        // A level counts as released only when every event of it is.
        g.impacts[k] = g.impacts[k] === undefined ? e.released : g.impacts[k] && e.released;
      });
      return order;
    }

    // Where every dot is right now. Worked out again for each draw and for
    // each pointer event, never cached between them: the price axis changes
    // width when the crosshair appears, which moves every candle sideways.
    function layout() {
      var out = [];
      if (!events.length) return out;
      var width = C.paneWidth(), base = C.paneHeight() - 9;
      groups().forEach(function (g) {
        var x = C.timeToX(g.bar);
        if (x === null || x < -R || x > width + R) return;
        var k = 0;
        [3, 2, 1].forEach(function (imp) {
          if (g.impacts[imp] === undefined) return;
          out.push({ x: x, y: base - k * STEP, impact: imp, released: g.impacts[imp], group: g });
          k++;
        });
      });
      return out;
    }

    function draw(ctx, size) {
      spots = layout();
      if (!spots.length) return;
      var col = colors(document.documentElement.dataset.theme);
      spots.forEach(function (s) {
        var c = col[s.impact];
        ctx.beginPath();
        ctx.arc(s.x, s.y, R, 0, Math.PI * 2);
        ctx.lineWidth = 1.5;
        if (s.released) { ctx.fillStyle = c.fill; ctx.fill(); ctx.strokeStyle = c.stroke; ctx.stroke(); }
        else { ctx.fillStyle = col.ring; ctx.fill(); ctx.strokeStyle = c.fill; ctx.lineWidth = 2; ctx.stroke(); }
      });
    }

    function hit(pt) {
      var best = null, bestD = R + 4;
      layout().forEach(function (s) {
        var d = Math.hypot(s.x - pt.x, s.y - pt.y);
        if (d < bestD) { bestD = d; best = s; }
      });
      return best;
    }

    // ---- pointer ----

    C.addPointerHandler({
      hover: function (pt) {
        var s = hit(pt);
        if (!s) { tip.classList.add("hidden"); return null; }
        var evs = sorted(s.group.events);
        tip.innerHTML = evs.slice(0, 4).map(function (e) {
          return "<div><span class='imp imp" + e.impact + "'></span><b>" + esc(e.ccy) + "</b> " + esc(e.name) +
            (e.released ? "" : " <span class='faint'>· " + esc(opts.fmtTime(e.ms)) + "</span>") + "</div>";
        }).join("") + (evs.length > 4 ? "<div class='faint'>+" + (evs.length - 4) + " more</div>" : "") +
          "<div class='faint tiny'>Click for details</div>";
        placeNear(tip, pt.x, pt.y);
        tip.classList.remove("hidden");
        return "pointer";
      },
      down: function (pt) {
        var s = hit(pt);
        if (!s) return false;
        this.active = s;
        return true;
      },
      move: function () {},
      up: function (pt) {
        var s = this.active;
        this.active = null;
        tip.classList.add("hidden");
        if (s) open(sorted(s.group.events), pt.x, pt.y);
      }
    }, true);

    function sorted(evs) {
      return evs.slice().sort(function (a, b) { return b.impact - a.impact || a.ms - b.ms || a.ccy.localeCompare(b.ccy); });
    }

    function placeNear(el, x, y) {
      var wrap = document.getElementById("chart").getBoundingClientRect();
      el.style.left = "0px"; el.style.top = "0px";
      var w = el.offsetWidth, h = el.offsetHeight;
      var left = Math.min(Math.max(x + 14, 6), wrap.width - w - 70);
      var top = y - h - 12;
      if (top < 6) top = Math.min(y + 14, wrap.height - h - 6);
      el.style.left = left + "px";
      el.style.top = top + "px";
    }

    // ---- popup ----

    var current = null;   // {list, index}

    function open(list, x, y) {
      current = { list: list, index: 0 };
      popup.classList.remove("hidden");
      render(null);
      placeNear(popup, x, y);
      load(0, x, y);
    }

    function load(i, x, y) {
      current.index = i;
      var e = current.list[i];
      var s = opts.getSession();
      render(null);
      fetch("/api/calendar/event/" + e.id + "?session=" + encodeURIComponent(s.id))
        .then(function (r) { if (!r.ok) throw new Error("not available"); return r.json(); })
        .then(function (d) { if (current && current.list[current.index].id === e.id) { render(d); if (x !== undefined) placeNear(popup, x, y); } })
        .catch(function (err) { popup.querySelector(".nbody").innerHTML = "<p class='faint'>Details are not available: " + esc(err.message) + "</p>"; });
    }

    function render(d) {
      var list = current.list, i = current.index;
      var tabs = list.length > 1 ? "<div class='ntabs'>" + list.map(function (e, k) {
        return "<button type='button' data-ni='" + k + "' class='" + (k === i ? "on" : "") + "'><span class='imp imp" + e.impact + "'></span>" +
          esc(e.ccy) + " " + esc(e.name) + "</button>";
      }).join("") + "</div>" : "";
      var body;
      if (!d) body = "<p class='faint'>Loading…</p>";
      else body = detailHtml(d);
      popup.innerHTML = "<button type='button' class='nclose ghost' aria-label='Close'>×</button>" + tabs + "<div class='nbody'>" + body + "</div>";
    }

    function cell(label, value, cls) {
      return "<div class='ncell'><div class='nk'>" + label + "</div><div class='nv mono " + (cls || "") + "'>" + (value == null ? "<span class='faint'>—</span>" : esc(value)) + "</div></div>";
    }

    function untilText(ms) {
      var clock = opts.getClockMs();
      if (!clock) return "";
      var m = Math.round((ms - clock) / 60000);
      if (m <= 0) return "";
      if (m < 60) return "in " + m + " min";
      if (m < 2880) return "in " + Math.floor(m / 60) + " h " + (m % 60) + " min";
      return "in " + Math.round(m / 1440) + " days";
    }

    function detailHtml(d) {
      var head = "<div class='nhead'><span class='nccy mono'>" + esc(d.currency) + "</span><h4>" + esc(d.name) + "</h4>" +
        "<span class='nbadge imp-b" + d.impact + "'>" + esc(IMPACT[d.impact] || d.impact_label) + " impact</span></div>";
      var when = "<p class='nwhen'>" + esc(opts.fmtTime(d.ms)) + (opts.hideDates() ? "" : " UTC") +
        (d.period ? " · for <span class='mono'>" + esc(d.period) + "</span>" : "") +
        (d.released ? " · released" : " · <b>" + esc(untilText(d.ms) || "due") + "</b>") + "</p>";
      var actual = d.released ? d.actual : null;
      // Just released and not collected yet: the calendar is updated a few
      // minutes after each medium or high impact release.
      var clock = opts.getClockMs();
      if (d.released && actual == null && d.forecast != null && clock && clock - d.ms < 1800000) actual = "coming in";
      var surprise = d.surprise ? "<span class='nsurp'>" + esc(d.surprise) + "</span>" : "";
      var values = "<div class='ngrid'>" +
        cell("Actual", d.released ? actual : "not out yet", d.released && actual !== "coming in" ? "" : "faint") +
        cell("Forecast", d.forecast) +
        cell("Previous", d.previous + (d.revised_previous && d.revised_previous !== d.previous ? " → " + d.revised_previous : "")) +
        "</div>" + surprise;
      var same = d.same_time.length ? "<div class='nsec'><div class='nk'>Released at the same time</div><ul class='nlist'>" +
        d.same_time.map(function (x) { return "<li><span class='imp imp" + x.impact + "'></span><span class='mono'>" + esc(x.currency) + "</span> " + esc(x.name) + "</li>"; }).join("") +
        "</ul><p class='tiny faint'>Released together, their effects on price cannot be told apart.</p></div>" : "";
      var hist = d.history.length ? "<div class='nsec'><div class='nk'>Earlier releases</div><table class='t ntable'><thead><tr><th>Released</th>" +
        "<th class='n'>Actual</th><th class='n'>Forecast</th><th class='n'>Previous</th></tr></thead><tbody>" +
        d.history.map(function (h) {
          return "<tr><td class='mono'>" + esc(opts.fmtTime(h.ms)) + "</td><td class='n'>" + esc(h.actual || "—") + "</td><td class='n'>" +
            esc(h.forecast || "—") + "</td><td class='n'>" + esc(h.previous || "—") + "</td></tr>";
        }).join("") + "</tbody></table></div>" : "<div class='nsec tiny faint'>No earlier release of this indicator before the replay's clock.</div>";
      var facts = [];
      if (d.concept) facts.push(["Kind", d.concept.replace("_", " ")]);
      if (d.frequency) facts.push(["Frequency", d.frequency]);
      if (d.group) facts.push(["Release group", d.group]);
      facts.push(["Time stamp", d.timestamp === "minute" ? "to the minute" : d.timestamp]);
      if (d.source) facts.push(["Source", d.source]);
      if (d.confounded) facts.push(["Note", "another event within 15 minutes"]);
      var meta = "<dl class='kv nmeta'>" + facts.map(function (f) { return "<dt>" + esc(f[0]) + "</dt><dd>" + esc(f[1]) + "</dd>"; }).join("") + "</dl>";
      var expl = d.explainer ? "<div class='nsec'><div class='nk'>What it is</div><p class='tiny'>" + esc(d.explainer) + "</p></div>" : "";
      return head + when + values + same + hist + expl + meta +
        "<p class='tiny faint nfoot'>From the fxmacro calendar. The actual is the first print; later revisions are not shown, " +
        "because at this point in the replay they have not happened.</p>";
    }

    function hidePopup() { popup.classList.add("hidden"); current = null; }

    popup.addEventListener("click", function (e) {
      if (e.target.closest(".nclose")) { hidePopup(); return; }
      var b = e.target.closest("[data-ni]");
      if (b && current) load(+b.dataset.ni);
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && current) hidePopup(); });
    document.addEventListener("pointerdown", function (e) {
      // A click on a dot never gets here: the chart captures it first.
      if (current && !popup.contains(e.target)) hidePopup();
    });

    // ---- impact filter (chips in the chart corner) ----

    function renderFilter(el) {
      el.innerHTML = "<span class='faint'>News</span>" + [3, 2, 1].map(function (k) {
        return "<button type='button' data-imp='" + k + "' class='" + (shown[k] ? "on" : "") + "' title='" + IMPACT[k] + " impact events'>" +
          "<span class='imp imp" + k + "'></span>" + IMPACT[k] + "</button>";
      }).join("");
    }
    var filterEl = document.getElementById("newsFilter");
    if (filterEl) {
      renderFilter(filterEl);
      filterEl.addEventListener("click", function (e) {
        var b = e.target.closest("[data-imp]");
        if (!b) return;
        shown[b.dataset.imp] = !shown[b.dataset.imp];
        saveImpacts();
        renderFilter(filterEl);
        overlay.request();
      });
    }

    return { reload: reload, onClock: onClock, clear: clear, redraw: function () { overlay.request(); }, hide: hidePopup,
             spots: function () { return spots; },
             count: function () { return events.length; } };
  }

  window.SimNews = { create: create };
})();
