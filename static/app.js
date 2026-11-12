/* HOT DEAL LIST — клиентская логика: рендер карточек, графики, фильтры, подписка. */
(function () {
  "use strict";

  var SYM = { USD: "$", AMD: "֏", EUR: "€", RUB: "₽", GBP: "£" };
  var $ = function (sel) { return document.querySelector(sel); };
  var state = { subscribed: false, locked: false, total: 0 };

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtChart(v, currency) {
    if (v === null || v === undefined || isNaN(v)) return "";
    var s = String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    var sym = SYM[currency] || "";
    if (currency === "USD" || currency === "EUR" || currency === "GBP") return sym + s;
    return s + (sym ? " " + sym : "");
  }

  function drawSpark(canvas, history, currency) {
    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 600;
    var cssH = 56;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    var pts = (history || []).filter(function (p) {
      return p && p.length === 2 && p[1] !== null && p[1] !== undefined && !isNaN(p[1]);
    });
    if (pts.length < 2) {
      ctx.fillStyle = "#8b96ab";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("мало данных", cssW / 2, cssH / 2 + 4);
      return;
    }

    var values = pts.map(function (p) { return p[1]; });
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var pad = (max - min) > 0 ? (max - min) * 0.18 : Math.max(max * 0.05, 1);
    var lo = min - pad;
    var hi = max + pad;
    var span = hi - lo || 1;

    var padX = 4;
    var W = cssW - padX * 2;
    var H = cssH - 16;

    function x(i) { return padX + (W * i) / (pts.length - 1); }
    function y(v) { return H - ((v - lo) / span) * H; }

    ctx.beginPath();
    pts.forEach(function (p, i) {
      var px = x(i), py = y(p[1]);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = "#22c55e";
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.stroke();

    pts.forEach(function (p, i) {
      ctx.beginPath();
      ctx.arc(x(i), y(p[1]), 2.1, 0, Math.PI * 2);
      ctx.fillStyle = i === pts.length - 1 ? "#22c55e" : "#34d399";
      ctx.strokeStyle = "#141a28";
      ctx.lineWidth = 1;
      ctx.fill();
      ctx.stroke();
    });

    ctx.font = "10px sans-serif";
    ctx.fillStyle = "#8b96ab";
    ctx.textAlign = "left";
    ctx.fillText(fmtChart(pts[0][1], currency), 0, 9);
    ctx.textAlign = "right";
    ctx.fillStyle = "#22c55e";
    ctx.fillText(fmtChart(pts[pts.length - 1][1], currency), cssW, 9);
  }

  function createCard(d) {
    var art = document.createElement("article");
    art.className = "card";

    var media = d.image
      ? '<img class="card-img" src="' + esc(d.image) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
      : '<div class="card-media placeholder"></div>';

    var tags = "";
    if (d.category) tags += '<span class="tag">' + esc(d.category) + "</span>";
    if (d.region) tags += '<span class="tag region">' + esc(d.region) + "</span>";

    var first = (d.first_label && d.first_label !== "—")
      ? '<div class="first-price">' + esc(d.first_label) + "</div>" : "";

    var sqm = "";
    if (d.has_area && d.price_sqm) {
      sqm =
        '<div class="sqm-block">' +
          '<div class="sqm-drop">−' + esc(d.drop_sqm_abs || "—") +
            '<span class="sqm-unit">/м²</span></div>' +
          '<div class="sqm-meta">сейчас <b>' + esc(d.price_sqm) + "/м²</b>" +
            (d.first_sqm ? " · было <s>" + esc(d.first_sqm) + "/м²</s>" : "") +
            (d.drop_sqm_pct != null ? ' · <b class="sqm-pct">−' + d.drop_sqm_pct + "%</b>" : "") +
          "</div>" +
        "</div>";
    }

    art.innerHTML =
      '<div class="card-media"><span class="drop-badge">↓' + esc(d.badge) + '%</span>' + media + "</div>" +
      '<div class="card-body">' +
        '<div class="card-tags">' + tags + "</div>" +
        '<h3 class="card-title" title="' + esc(d.title) + '">' + esc(d.title) + "</h3>" +
        '<div class="prices"><div class="current-price">' + esc(d.current_label) + "</div>" + first + "</div>" +
        sqm +
        '<canvas class="spark"></canvas>' +
        '<div class="drop-stats">' +
          "<span>от перв. <b>−" + (d.drop_first_label != null ? d.drop_first_label : "—") + "%</b></span>" +
          "<span>от пред. <b>−" + (d.drop_prev_label != null ? d.drop_prev_label : "—") + "%</b></span>" +
        "</div>" +
        '<a class="view-link" href="' + esc(d.url) + '" target="_blank" rel="noopener">Посмотреть объявление →</a>' +
      "</div>";

    var canvas = art.querySelector(".spark");
    if (d.history && d.history.length >= 2) drawSpark(canvas, d.history, d.currency);
    else canvas.style.display = "none";

    return art;
  }

  function renderDeals(data) {
    state.subscribed = data.subscribed;
    state.locked = data.locked;
    state.total = data.total;

    var grid = $("#deals-grid");
    grid.innerHTML = "";
    var locked = $("#locked");
    locked.classList.add("hidden");
    locked.innerHTML = "";

    var countEl = $("#deals-count");
    countEl.textContent = data.total + " шт.";

    var list = data.locked ? data.preview : data.deals;
    (list || []).forEach(function (d) { grid.appendChild(createCard(d)); });

    if (data.locked && data.locked_count > 0) {
      var n = Math.min(data.locked_count, 9);
      for (var i = 0; i < n; i++) {
        var ph = document.createElement("div");
        ph.className = "card locked-card";
        ph.innerHTML =
          '<div class="card-media placeholder"></div>' +
          '<div class="card-body locked-lines"><div class="line"></div><div class="line short"></div><div class="line"></div><div class="line short"></div></div>';
        grid.appendChild(ph);
      }
      locked.classList.remove("hidden");
      locked.innerHTML =
        '<div class="locked-inner">' +
          '<div class="lock-icon">🔒</div>' +
          "<h3>Ещё " + data.locked_count + " предложений скрыто</h3>" +
          "<p>Подпишитесь, чтобы видеть все " + data.total + " горячих предложений с ценами и графиками.</p>" +
          '<button class="btn primary lg" id="locked-cta" type="button">Оформить подписку</button>' +
        "</div>";
      $("#locked-cta").addEventListener("click", openModal);
    }

    $("#deals-status").textContent =
      (data.total === 0 ? "По заданным фильтрам ничего не найдено." : "");
  }

  function currentFilters() {
    return {
      q: $("#f-q").value.trim(),
      category: $("#f-category").value,
      region: $("#f-region").value,
      currency: $("#f-currency").value,
      min_price: $("#f-min-price").value,
      max_price: $("#f-max-price").value,
      min_drop: $("#f-min-drop").value
    };
  }

  function loadDeals() {
    var qs = new URLSearchParams();
    var f = currentFilters();
    Object.keys(f).forEach(function (k) { if (f[k]) qs.set(k, f[k]); });

    $("#deals-status").textContent = "Загрузка…";
    fetch("/api/deals?" + qs.toString())
      .then(function (r) { return r.json(); })
      .then(renderDeals)
      .catch(function () { $("#deals-status").textContent = "Ошибка загрузки данных."; });
  }

  function fillFilters() {
    fetch("/api/filters").then(function (r) { return r.json(); }).then(function (f) {
      var cat = $("#f-category"), reg = $("#f-region"), cur = $("#f-currency");
      (f.categories || []).forEach(function (v) {
        var o = document.createElement("option"); o.value = v; o.textContent = v; cat.appendChild(o);
      });
      (f.regions || []).forEach(function (v) {
        var o = document.createElement("option"); o.value = v; o.textContent = v; reg.appendChild(o);
      });
      (f.currencies || []).forEach(function (v) {
        var o = document.createElement("option"); o.value = v; o.textContent = v; cur.appendChild(o);
      });
    });
  }

  /* ---------- subscribe ---------- */
  function openModal() { $("#modal").classList.remove("hidden"); }
  function closeModal() { $("#modal").classList.add("hidden"); }

  function submitSubscribe() {
    var plan = document.querySelector('input[name="plan"]:checked').value;
    var email = $("#modal-email").value.trim();
    var btn = $("#modal-submit");
    btn.disabled = true; btn.textContent = "Обработка…";
    fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: plan, email: email })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res.ok) window.location.reload();
      else { btn.disabled = false; btn.textContent = "Оплатить и открыть доступ"; }
    }).catch(function () { btn.disabled = false; btn.textContent = "Оплатить и открыть доступ"; });
  }

  /* ---------- refresh ---------- */
  function pollUntilDone() {
    var timer = setInterval(function () {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
        $("#deals-status").textContent =
          "Обновление данных… собрано " + s.collected_count + " объявлений";
        if (!s.collecting) { clearInterval(timer); window.location.reload(); }
      }).catch(function () {});
    }, 2000);
  }

  function init() {
    var init = window.__INIT__ || {};
    if (init.error) $("#deals-status").textContent = init.error;

    fillFilters();
    loadDeals();

    var debounce;
    ["f-q", "f-category", "f-region", "f-currency", "f-min-price", "f-max-price", "f-min-drop"]
      .forEach(function (id) {
        var el = $("#" + id);
        if (!el) return;
        el.addEventListener(el.tagName === "INPUT" && el.type !== "number" ? "input" : "change", function () {
          clearTimeout(debounce);
          debounce = setTimeout(loadDeals, 250);
        });
      });

    $("#f-reset").addEventListener("click", function () {
      ["f-q", "f-category", "f-region", "f-currency", "f-min-price", "f-max-price", "f-min-drop"]
        .forEach(function (id) { var el = $("#" + id); if (el) el.value = ""; });
      loadDeals();
    });

    var subBtn = $("#subscribe-btn");
    if (subBtn) subBtn.addEventListener("click", openModal);
    document.querySelectorAll(".plan-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        var plan = b.getAttribute("data-plan");
        var radio = document.querySelector('input[name="plan"][value="' + plan + '"]');
        if (radio) radio.checked = true;
        openModal();
      });
    });

    var cancelBtn = $("#cancel-btn");
    if (cancelBtn) cancelBtn.addEventListener("click", function () {
      fetch("/api/cancel", { method: "POST" }).then(function () { window.location.reload(); });
    });

    $("#modal-close").addEventListener("click", closeModal);
    $("#modal").addEventListener("click", function (e) { if (e.target === this) closeModal(); });
    $("#modal-submit").addEventListener("click", submitSubscribe);

    $("#refresh-btn").addEventListener("click", function () {
      var btn = $("#refresh-btn");
      btn.disabled = true; btn.textContent = "Обновляем…";
      fetch("/refresh", { method: "POST" }).then(function (r) { return r.json(); })
        .then(function (res) { if (res.collecting) pollUntilDone(); else { btn.disabled = false; btn.textContent = "⟳ Обновить данные"; } })
        .catch(function () { btn.disabled = false; btn.textContent = "⟳ Обновить данные"; });
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
