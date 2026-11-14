/* Drawing tools (guide step 6).
 *
 * Every drawing is data, not pixels: a kind, a list of (time, price) points,
 * a style and the timeframe it was drawn on. Pixels change with zoom; time
 * and price do not, so a drawing stays on the same candle and level across
 * zoom, timeframe switches and page reloads.
 *
 * Kinds: hline (1 point), trend (2), rect (2), position (entry, stop,
 * target; a long if the stop is below the entry). All share one interaction
 * layer: pick a tool, click to place points, drag a handle to edit, drag the
 * body to move, Delete to remove, Esc to cancel.
 */
(function () {
  "use strict";

  var HANDLE_R = 5;
  var HIT = 6;

  function uid() { return "d" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }
  var DIGITS = 5;
  function round5(p) { var f = Math.pow(10, DIGITS); return Math.round(p * f) / f; }
  function clone(d) { return JSON.parse(JSON.stringify(d)); }

  function distToSegment(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay;
    var len2 = dx * dx + dy * dy;
    var t = len2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0;
    var x = ax + t * dx, y = ay + t * dy;
    return Math.hypot(px - x, py - y);
  }

  function hexA(hex, a) {
    var h = (hex || "#888").replace("#", "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    return "rgba(" + parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4, 6), 16) + "," + a + ")";
  }

  function create(C, opts) {
    var drawings = [];
    var tool = null;          // null = cursor
    var creating = null;      // drawing being placed
    var selectedId = null;
    var drag = null;          // {id, handle, start, orig}
    var pip = 0.0001;

    var overlay = C.attachOverlay(draw);
    function redraw() { overlay.request(); }

    function byId(id) { for (var i = 0; i < drawings.length; i++) if (drawings[i].id === id) return drawings[i]; return null; }
    function selected() { return byId(selectedId); }

    function select(id) {
      selectedId = id;
      if (opts.onSelect) opts.onSelect(selected());
      redraw();
    }

    // ---- geometry ----

    function xy(p) { return { x: C.timeToX(p.time), y: C.priceToY(p.price) }; }

    function positionGeom(d) {
      var entry = d.points[0], stop = d.points[1], target = d.points[2];
      var x0 = C.timeToX(entry.time), x1 = C.timeToX(stop.time);
      return {
        x0: Math.min(x0, x1), x1: Math.max(x0, x1),
        ye: C.priceToY(entry.price), ys: C.priceToY(stop.price), yt: C.priceToY(target.price),
        long: stop.price < entry.price
      };
    }

    function handles(d) {
      if (d.kind === "hline") return [];
      if (d.kind === "position") {
        var g = positionGeom(d);
        return [{ x: g.x0, y: g.ye, i: 0 }, { x: g.x1, y: g.ys, i: 1 }, { x: g.x1, y: g.yt, i: 2 }];
      }
      return d.points.map(function (p, i) { var q = xy(p); return { x: q.x, y: q.y, i: i }; });
    }

    function hitTest(pt) {
      var order = drawings.slice().reverse();
      var sel = selected();
      if (sel) {
        var hs = handles(sel);
        for (var k = 0; k < hs.length; k++) {
          if (hs[k].x !== null && hs[k].y !== null && Math.hypot(hs[k].x - pt.x, hs[k].y - pt.y) <= HANDLE_R + 3) return { d: sel, handle: hs[k].i };
        }
      }
      for (var i = 0; i < order.length; i++) {
        var d = order[i];
        if (d.kind === "hline") {
          var y = C.priceToY(d.points[0].price);
          if (y !== null && Math.abs(y - pt.y) <= HIT) return { d: d, handle: "body" };
        } else if (d.kind === "trend") {
          var a = xy(d.points[0]), b = xy(d.points[1]);
          if (a.x !== null && b.x !== null && distToSegment(pt.x, pt.y, a.x, a.y, b.x, b.y) <= HIT) return { d: d, handle: "body" };
        } else if (d.kind === "rect") {
          var p = xy(d.points[0]), q = xy(d.points[1]);
          if (p.x === null || q.x === null) continue;
          if (pt.x >= Math.min(p.x, q.x) - HIT && pt.x <= Math.max(p.x, q.x) + HIT &&
              pt.y >= Math.min(p.y, q.y) - HIT && pt.y <= Math.max(p.y, q.y) + HIT) return { d: d, handle: "body" };
        } else if (d.kind === "position") {
          var g = positionGeom(d);
          var top = Math.min(g.ys, g.yt), bot = Math.max(g.ys, g.yt);
          if (pt.x >= g.x0 && pt.x <= g.x1 && pt.y >= top && pt.y <= bot) return { d: d, handle: "body" };
        }
      }
      return null;
    }

    // ---- drawing ----

    function label(ctx, text, x, y, color, align) {
      ctx.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
      var w = ctx.measureText(text).width + 8;
      var bx = align === "right" ? x - w : x;
      ctx.fillStyle = hexA(C.tokens().surface || "#101010", 0.85);
      ctx.fillRect(bx, y - 8, w, 16);
      ctx.fillStyle = color;
      ctx.textBaseline = "middle";
      ctx.fillText(text, bx + 4, y);
    }

    var paintQueued = false;
    function afterPaint() {
      if (paintQueued || !opts.onPaint) return;
      paintQueued = true;
      requestAnimationFrame(function () { paintQueued = false; opts.onPaint(selected()); });
    }

    function draw(ctx, size) {
      afterPaint();
      var T = C.tokens();
      var all = creating ? drawings.concat([creating]) : drawings;
      all.forEach(function (d) {
        var color = (d.style && d.style.color) || T.draw;
        var isSel = d.id === selectedId || d === creating;
        ctx.lineWidth = isSel ? 2 : 1.5;
        ctx.strokeStyle = color;
        ctx.setLineDash([]);
        if (d.kind === "hline") {
          var y = C.priceToY(d.points[0].price);
          if (y === null) return;
          ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size.width, y); ctx.stroke();
          label(ctx, d.points[0].price.toFixed(DIGITS), 6, y - 10, color);
        } else if (d.kind === "trend") {
          if (d.points.length < 2) return;
          var a = xy(d.points[0]), b = xy(d.points[1]);
          if (a.x === null || b.x === null || a.y === null || b.y === null) return;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        } else if (d.kind === "rect") {
          if (d.points.length < 2) return;
          var p = xy(d.points[0]), q = xy(d.points[1]);
          if (p.x === null || q.x === null || p.y === null || q.y === null) return;
          ctx.fillStyle = hexA(color, 0.12);
          ctx.fillRect(Math.min(p.x, q.x), Math.min(p.y, q.y), Math.abs(q.x - p.x), Math.abs(q.y - p.y));
          ctx.strokeRect(Math.min(p.x, q.x), Math.min(p.y, q.y), Math.abs(q.x - p.x), Math.abs(q.y - p.y));
        } else if (d.kind === "position") {
          if (d.points.length < 3) return;
          var g = positionGeom(d);
          if (g.x0 === null || g.x1 === null || g.ye === null) return;
          var w = Math.max(g.x1 - g.x0, 2);
          ctx.fillStyle = hexA(T.up, 0.18);
          ctx.fillRect(g.x0, Math.min(g.ye, g.yt), w, Math.abs(g.yt - g.ye));
          ctx.fillStyle = hexA(T.down, 0.18);
          ctx.fillRect(g.x0, Math.min(g.ye, g.ys), w, Math.abs(g.ys - g.ye));
          ctx.strokeStyle = T.muted; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(g.x0, g.ye); ctx.lineTo(g.x1, g.ye); ctx.stroke();
          var e = d.points[0].price, s = d.points[1].price, t = d.points[2].price;
          var riskPips = Math.abs(e - s) / pip, rewardPips = Math.abs(t - e) / pip;
          var rr = riskPips ? rewardPips / riskPips : 0;
          label(ctx, "Target " + t.toFixed(DIGITS) + "  +" + rewardPips.toFixed(1) + " pips", g.x0 + 2, g.yt + (g.long ? -10 : 10), T.up);
          label(ctx, "Stop " + s.toFixed(DIGITS) + "  -" + riskPips.toFixed(1) + " pips", g.x0 + 2, g.ys + (g.long ? 10 : -10), T.down);
          label(ctx, (g.long ? "Long" : "Short") + "  R:R " + rr.toFixed(2), g.x0 + 2, g.ye + (g.long ? 10 : -10), T.text);
        }
        if (isSel) {
          handles(d).forEach(function (h) {
            if (h.x === null || h.y === null) return;
            ctx.beginPath(); ctx.arc(h.x, h.y, HANDLE_R, 0, Math.PI * 2);
            ctx.fillStyle = T.surface || "#101010"; ctx.fill();
            ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
          });
        }
      });
    }

    // ---- interaction ----

    function newDrawing(kind, pt) {
      var p = { time: pt.time, price: round5(pt.price) };
      var d = { id: uid(), kind: kind, timeframe: opts.getTimeframe(), points: [p], style: {} };
      if (kind === "trend" || kind === "rect") d.points.push({ time: p.time, price: p.price });
      if (kind === "position") d.points.push({ time: p.time, price: p.price }, { time: p.time, price: p.price });
      return d;
    }

    function shapePosition(d, pt) {
      // While placing: the second click sets the stop and the right edge; the
      // target starts at 2R and is dragged afterwards.
      var entry = d.points[0];
      var tf = opts.getTimeframeSeconds();
      var right = pt.time > entry.time ? pt.time : entry.time + 20 * tf;
      var stop = round5(pt.price);
      d.points[1] = { time: right, price: stop };
      d.points[2] = { time: right, price: round5(entry.price + 2 * (entry.price - stop)) };
    }

    function finish() {
      var d = creating;
      creating = null;
      if (d.kind === "position" && d.points[1].price === d.points[0].price) { redraw(); return; }
      drawings.push(d);
      setTool(null);
      select(d.id);
      opts.onSave(d);
    }

    var handler = {
      busy: function () { return !!tool; },
      hover: function (pt) {
        if (tool) {
          if (creating && pt.time !== null && pt.price !== null) {
            if (creating.kind === "position") shapePosition(creating, pt);
            else creating.points[creating.points.length - 1] = { time: pt.time, price: round5(pt.price) };
            redraw();
          }
          return "crosshair";
        }
        var h = hitTest(pt);
        if (!h) return null;
        return h.handle === "body" ? (h.d.kind === "hline" ? "ns-resize" : "move") : "grab";
      },
      down: function (pt) {
        if (pt.time === null || pt.price === null) return false;
        if (tool) {
          if (!creating) {
            creating = newDrawing(tool, pt);
            if (tool === "hline") finish();
          } else {
            if (creating.kind === "position") shapePosition(creating, pt);
            else creating.points[creating.points.length - 1] = { time: pt.time, price: round5(pt.price) };
            finish();
          }
          redraw();
          return true;
        }
        var h = hitTest(pt);
        if (!h) { if (selectedId) select(null); return false; }
        if (h.d.id !== selectedId) select(h.d.id);
        drag = { id: h.d.id, handle: h.handle, start: pt, orig: clone(h.d) };
        return true;
      },
      move: function (pt) {
        if (!drag || pt.time === null || pt.price === null) return;
        var d = byId(drag.id);
        if (!d) return;
        var dt = pt.time - drag.start.time, dp = pt.price - drag.start.price;
        if (drag.handle === "body") {
          d.points = drag.orig.points.map(function (p) {
            return { time: d.kind === "hline" ? p.time : p.time + dt, price: round5(p.price + dp) };
          });
        } else if (d.kind === "position") {
          var o = drag.orig.points;
          if (drag.handle === 0) {
            d.points[0] = { time: o[0].time + dt, price: round5(o[0].price + dp) };
          } else {
            var right = Math.max(pt.time, d.points[0].time);
            d.points[drag.handle] = { time: right, price: round5(pt.price) };
            var other = drag.handle === 1 ? 2 : 1;
            d.points[other] = { time: right, price: d.points[other].price };
          }
        } else {
          d.points[drag.handle] = { time: pt.time, price: round5(pt.price) };
        }
        redraw();
        if (opts.onSelect) opts.onSelect(d);
      },
      up: function () {
        if (!drag) return;
        var d = byId(drag.id);
        var moved = d && JSON.stringify(d.points) !== JSON.stringify(drag.orig.points);
        drag = null;
        if (moved) opts.onSave(d);
      }
    };
    C.addPointerHandler(handler);

    function setTool(kind) {
      tool = kind || null;
      creating = null;
      if (opts.onTool) opts.onTool(tool);
      redraw();
    }

    function deleteSelected() {
      var d = selected();
      if (!d) return;
      drawings = drawings.filter(function (x) { return x.id !== d.id; });
      select(null);
      opts.onDelete(d.id);
    }

    function setAll(list) {
      drawings = (list || []).map(function (d) {
        return { id: d.id, kind: d.kind, timeframe: d.timeframe, points: d.points, style: d.style || {} };
      });
      if (selectedId && !byId(selectedId)) select(null);
      redraw();
    }

    document.addEventListener("keydown", function (e) {
      var tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.key === "Escape") { if (tool || creating) setTool(null); else select(null); }
      if ((e.key === "Delete" || e.key === "Backspace") && selectedId) { e.preventDefault(); deleteSelected(); }
    });

    // Prices round to the instrument's digits; position boxes count its pips.
    function setInstrument(inst) { DIGITS = inst.digits; pip = inst.pip; redraw(); }

    return {
      setInstrument: setInstrument,
      setTool: setTool,
      tool: function () { return tool; },
      setAll: setAll,
      selected: selected,
      select: select,
      deleteSelected: deleteSelected,
      redraw: redraw,
      screenPoint: function (d) {
        var hs = handles(d);
        if (d.kind === "hline") return { x: 60, y: C.priceToY(d.points[0].price) };
        if (!hs.length) return null;
        var top = hs.reduce(function (a, h) { return h.y !== null && (a === null || h.y < a.y) ? h : a; }, null);
        var right = hs.reduce(function (a, h) { return h.x !== null && (a === null || h.x > a.x) ? h : a; }, null);
        return top && right ? { x: right.x, y: top.y } : null;
      }
    };
  }

  window.SimDrawings = { create: create };
})();
