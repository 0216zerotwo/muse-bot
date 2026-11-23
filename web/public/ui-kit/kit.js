/* The companion's UI kit for generated answers: the bridge to the app plus small helpers, so a
   generated page needs little code and looks like the rest of the app.

   ui.send(text)            send a message as the user (only on an explicit tap, never on load/input)
   data-send="…"            any element: tapping it sends that text
   ui.chart(el, spec)       Chart.js with the theme's colors
   ui.map(el, spec)         Leaflet map with numbered pins (高德 base map)
   ui.fmt.money/num/pct     number formatting
   ui.on(selector, render)  re-run render() whenever those inputs change (and once now)
   .seg / .chips            segmented controls and chips work by themselves (aria-pressed)
*/
(function () {
  "use strict";
  var post = function (message) {
    try { parent.postMessage(message, "*"); } catch (e) { /* not framed */ }
  };

  var ui = (window.ui = window.ui || {});

  ui.send = function (text) {
    text = String(text == null ? "" : text).trim();
    if (text) post({ type: "companion:send", text: text.slice(0, 2000) });
  };
  // Older pages used window.companion.send.
  window.companion = { send: ui.send };

  /* ---------- theme ---------- */

  ui.css = function (name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback || "";
  };
  var themed = [];
  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type !== "companion:theme" || !data.tokens) return;
    for (var name in data.tokens) if (data.tokens[name]) document.documentElement.style.setProperty(name, data.tokens[name]);
    themed.forEach(function (fn) { try { fn(); } catch (e) { /* keep others */ } });
  });

  /* ---------- size ---------- */

  var lastHeight = 0;
  var report = function () {
    if (typeof document === "undefined" || !document.documentElement) return;
    var height = Math.ceil(document.documentElement.scrollHeight);
    if (Math.abs(height - lastHeight) > 2) {
      lastHeight = height;
      post({ type: "companion:height", height: height });
    }
  };
  window.addEventListener("load", function () {
    report();
    if (window.ResizeObserver) new ResizeObserver(report).observe(document.body);
  });
  setTimeout(report, 50);

  /* ---------- controls ---------- */

  document.addEventListener("click", function (event) {
    var target = event.target.closest ? event.target.closest("button, [data-send]") : null;
    if (!target) return;
    var group = target.parentElement;
    if (group && group.classList.contains("seg")) {
      Array.prototype.forEach.call(group.children, function (b) { b.setAttribute("aria-pressed", String(b === target)); });
      group.dataset.value = target.dataset.value || target.textContent.trim();
      group.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (group && group.classList.contains("chips")) {
      if (group.hasAttribute("data-multi")) {
        target.setAttribute("aria-pressed", String(target.getAttribute("aria-pressed") !== "true"));
      } else {
        Array.prototype.forEach.call(group.children, function (b) { b.setAttribute("aria-pressed", String(b === target)); });
      }
      group.dataset.value = Array.prototype.filter
        .call(group.children, function (b) { return b.getAttribute("aria-pressed") === "true"; })
        .map(function (b) { return b.dataset.value || b.textContent.trim(); })
        .join(",");
      group.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (target.hasAttribute("data-send")) {
      event.preventDefault();
      ui.send(target.getAttribute("data-send") || target.textContent);
    }
  });

  /** The value of an input, a .seg or a .chips group, by selector or element. */
  ui.val = function (target) {
    var el = typeof target === "string" ? document.querySelector(target) : target;
    if (!el) return undefined;
    if (el.classList && (el.classList.contains("seg") || el.classList.contains("chips"))) {
      if (!el.dataset.value) {
        var on = Array.prototype.filter.call(el.children, function (b) { return b.getAttribute("aria-pressed") === "true"; });
        el.dataset.value = on.map(function (b) { return b.dataset.value || b.textContent.trim(); }).join(",");
      }
      return el.dataset.value;
    }
    if (el.type === "range" || el.type === "number") return Number(el.value);
    return el.value;
  };

  var escHtml = function (value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  };
  var findOutput = function (id) {
    if (!id) return null;
    var list = document.querySelectorAll("output[for]");
    for (var i = 0; i < list.length; i++) {
      if (list[i].getAttribute("for") === id) return list[i];
    }
    return null;
  };

  /** Run render() now and whenever any matching control changes. */
  ui.on = function (selector, render) {
    var run = function () {
      try { render(); } catch (e) { console.error(e); }
      // Range inputs show their value in a sibling <output> when there is one.
      document.querySelectorAll("input[type=range]").forEach(function (input) {
        var out = findOutput(input.id);
        if (out && !out.dataset.manual) out.textContent = input.value + (out.dataset.unit || "");
      });
      report();
    };
    document.querySelectorAll(selector).forEach(function (el) {
      el.addEventListener("input", run);
      el.addEventListener("change", run);
    });
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
    else run();
    return run;
  };

  /* ---------- numbers ---------- */

  var finite = function (n) { return typeof n === "number" && isFinite(n); };
  ui.fmt = {
    num: function (n, digits) {
      if (!finite(n)) return "—";
      return n.toLocaleString("zh-CN", { maximumFractionDigits: digits == null ? 0 : digits, minimumFractionDigits: digits || 0 });
    },
    money: function (n, symbol, digits) {
      if (!finite(n)) return "—";
      return (symbol == null ? "¥" : symbol) + ui.fmt.num(n, digits == null ? 0 : digits);
    },
    pct: function (n, digits) { return finite(n) ? ui.fmt.num(n * 100, digits == null ? 1 : digits) + "%" : "—"; },
    wan: function (n, digits) { return finite(n) ? ui.fmt.num(n / 10000, digits == null ? 1 : digits) + " 万" : "—"; },
  };

  /* ---------- charts ---------- */

  var palette = function () {
    // The companion's color first, then hues far from it: its accent is the same hue, so two series
    // in primary and accent read as one color.
    return [ui.css("--c-primary", "#bd5a02"), "#4f8a8b", "#8a6fbf", "#c9a227", "#6b7f99", "#b5565c", ui.css("--c-accent", "#e67f3b")];
  };
  var alpha = function (color, a) {
    var m = /^#([0-9a-f]{6})$/i.exec(color);
    if (!m) return color;
    var v = parseInt(m[1], 16);
    return "rgba(" + (v >> 16) + "," + ((v >> 8) & 255) + "," + (v & 255) + "," + a + ")";
  };

  /**
   * ui.chart(el, {type: "bar"|"line"|"pie"|"doughnut", labels: [...], datasets: [{label, data}],
   *               unit: "元", stacked: false, horizontal: false, height: 200})
   * Returns an object with update(spec) for live recalculation.
   */
  ui.chart = function (target, spec) {
    var el = typeof target === "string" ? document.querySelector(target) : target;
    if (!el || !window.Chart) return null;
    var box = el;
    if (el.tagName !== "CANVAS") {
      el.classList.add("chart");
      if (spec.height) el.style.height = spec.height + "px";
      box = document.createElement("canvas");
      el.innerHTML = "";
      el.appendChild(box);
    }
    var config = function (s) {
      var colors = palette();
      var round = s.type === "pie" || s.type === "doughnut";
      var text = ui.css("--c-text-muted", "#6e6058");
      var grid = ui.css("--c-border", "rgba(0,0,0,0.08)");
      return {
        type: s.type || "bar",
        data: {
          labels: s.labels || [],
          datasets: (s.datasets || []).map(function (d, i) {
            var color = d.color || colors[i % colors.length];
            return Object.assign({
              borderColor: round ? ui.css("--c-surface", "#fff") : color,
              backgroundColor: round ? (s.labels || []).map(function (_, j) { return colors[j % colors.length]; })
                : s.type === "line" ? alpha(color, 0.15) : color,
              borderWidth: round ? 2 : s.type === "line" ? 2.5 : 0,
              borderRadius: s.type === "bar" || !s.type ? 6 : 0,
              tension: 0.35, pointRadius: s.type === "line" ? 0 : undefined, fill: s.type === "line" && (s.datasets || []).length === 1,
            }, d);
          }),
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
          indexAxis: s.horizontal ? "y" : "x",
          plugins: {
            legend: { display: round || (s.datasets || []).length > 1, position: "bottom", labels: { color: text, boxWidth: 10, boxHeight: 10, usePointStyle: true } },
            tooltip: { callbacks: { label: function (c) { var v = c.parsed && typeof c.parsed === "object" ? (s.horizontal ? c.parsed.x : c.parsed.y) : c.parsed; return (c.dataset.label ? c.dataset.label + "：" : "") + ui.fmt.num(v, 2) + (s.unit || ""); } } },
          },
          scales: round ? {} : {
            x: { stacked: !!s.stacked, grid: { display: false }, ticks: { color: text, maxRotation: 0, autoSkip: true } },
            y: { stacked: !!s.stacked, grid: { color: grid }, border: { display: false }, ticks: { color: text, callback: function (v) { return ui.fmt.num(v) + (s.unit || ""); } } },
          },
        },
      };
    };
    var chart = new window.Chart(box, config(spec));
    var current = spec;
    themed.push(function () { var c = config(current); chart.data = c.data; chart.options = c.options; chart.update("none"); });
    return {
      chart: chart,
      update: function (next) { current = Object.assign({}, current, next); var c = config(current); chart.data = c.data; chart.update(); },
    };
  };

  /* ---------- maps ---------- */

  /**
   * ui.map(el, {pins: [{name, lat, lng, note}], route: true, height: 240, zoom})
   * Numbered pins in order; with route, a dashed line joins them (illustrative, not directions).
   * Tapping a pin highlights the element with data-stop="<index>" and vice versa.
   */
  ui.map = function (target, spec) {
    var el = typeof target === "string" ? document.querySelector(target) : target;
    if (!el || !window.L) return null;
    el.classList.add("map");
    if (spec.height) el.style.height = spec.height + "px";
    var L = window.L;
    var map = L.map(el, { zoomControl: false, attributionControl: true, scrollWheelZoom: false });
    // 高德 base map: reachable in mainland China and Taiwan; coordinates are GCJ-02 in mainland China.
    L.tileLayer("https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}", {
      subdomains: "1234", maxZoom: 18, attribution: "© 高德地图",
    }).addTo(map);
    var pins = (spec.pins || []).filter(function (p) { return finite(Number(p.lat)) && finite(Number(p.lng)); });
    var markers = pins.map(function (p, i) {
      var icon = L.divIcon({ className: "", html: '<div class="map-pin" data-pin="' + i + '">' + (i + 1) + "</div>", iconSize: [24, 24], iconAnchor: [12, 12] });
      var marker = L.marker([Number(p.lat), Number(p.lng)], { icon: icon }).addTo(map);
      if (p.name) marker.bindTooltip(escHtml(p.name) + (p.note ? " · " + escHtml(p.note) : ""), { direction: "top", offset: [0, -12] });
      marker.on("click", function () { ui.focusStop(i); });
      return marker;
    });
    if (spec.route && pins.length > 1) {
      L.polyline(pins.map(function (p) { return [Number(p.lat), Number(p.lng)]; }), {
        color: ui.css("--c-primary", "#bd5a02"), weight: 3, opacity: 0.7, dashArray: "6 8",
      }).addTo(map);
    }
    if (pins.length === 1) map.setView([Number(pins[0].lat), Number(pins[0].lng)], spec.zoom || 14);
    else if (pins.length) map.fitBounds(L.latLngBounds(pins.map(function (p) { return [Number(p.lat), Number(p.lng)]; })), { padding: [28, 28] });
    else map.setView([25.04, 121.56], 11);
    ui.focusStop = function (i) {
      document.querySelectorAll("[data-pin]").forEach(function (pin) { pin.classList.toggle("active", Number(pin.dataset.pin) === i); });
      document.querySelectorAll("[data-stop]").forEach(function (stop) { stop.classList.toggle("active", Number(stop.dataset.stop) === i); });
      if (markers[i]) { map.panTo(markers[i].getLatLng()); markers[i].openTooltip(); }
    };
    document.addEventListener("click", function (event) {
      var stop = event.target.closest ? event.target.closest("[data-stop]") : null;
      if (stop && !event.target.closest("[data-send]")) ui.focusStop(Number(stop.dataset.stop));
    });
    setTimeout(function () { map.invalidateSize(); }, 60);
    return map;
  };

  /* ---------- errors ---------- */

  // A broken page should say so quietly rather than show a blank box.
  window.addEventListener("error", function (event) {
    if (document.body && !document.querySelector(".ui-error")) {
      var note = document.createElement("p");
      note.className = "note ui-error";
      note.textContent = "这里有一部分没显示出来。";
      document.body.appendChild(note);
      report();
    }
    console.error(event.message);
  });
})();
