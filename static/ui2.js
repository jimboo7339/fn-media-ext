/* UI 2.0 动效与交互层。全部是渐进增强：关掉 JS 页面依然完整可用。
   主题偏好沿用经典版的 localStorage 键（fnxt.theme），两个 UI 之间切换不丢主题。 */
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  /* ---------------- 主题：auto / light / dark（与 1.0 共享偏好） ---------------- */
  var KEY = "fnxt.theme";
  var mq = window.matchMedia("(prefers-color-scheme: light)");

  function pref() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : "auto";
    } catch (e) {
      return "auto";
    }
  }
  function resolve(p) {
    return p === "auto" ? (mq.matches ? "light" : "dark") : p;
  }
  function paint(p) {
    var root = document.documentElement;
    root.setAttribute("data-theme", resolve(p));
    root.setAttribute("data-pref", p);
    each(document.querySelectorAll("[data-theme-set]"), function (b) {
      var on = b.getAttribute("data-theme-set") === p;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    var cycle = document.getElementById("theme-cycle");
    if (cycle) {
      var marks = { auto: "跟随系统", light: "浅色", dark: "深色" };
      cycle.setAttribute("aria-label", "切换主题，当前" + marks[p]);
    }
  }
  function save(p) {
    try {
      localStorage.setItem(KEY, p);
    } catch (e) {}
    paint(p);
  }
  var onChange = function () {
    if (pref() === "auto") paint("auto");
  };
  paint(pref());
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else if (mq.addListener) mq.addListener(onChange);
  each(document.querySelectorAll("[data-theme-set]"), function (b) {
    b.addEventListener("click", function () {
      save(b.getAttribute("data-theme-set"));
    });
  });
  var cycle = document.getElementById("theme-cycle");
  if (cycle) {
    var order = ["auto", "light", "dark"];
    cycle.addEventListener("click", function () {
      save(order[(order.indexOf(pref()) + 1) % order.length]);
    });
  }

  /* ---------------- 入场：stagger 自动编号 ---------------- */
  each(document.querySelectorAll("[data-stagger]"), function (box) {
    each(box.children, function (el, i) {
      if (!el.style.getPropertyValue("--i")) el.style.setProperty("--i", Math.min(i, 14));
    });
  });

  /* ---------------- KPI 数字滚动 ---------------- */
  function countUp(el) {
    var target = parseFloat(el.getAttribute("data-v"));
    if (isNaN(target) || reduced.matches) {
      el.textContent = el.getAttribute("data-v");
      return;
    }
    var dur = 850;
    var t0 = null;
    var step = function (ts) {
      if (t0 === null) t0 = ts;
      var k = Math.min(1, (ts - t0) / dur);
      var eased = 1 - Math.pow(1 - k, 3); // easeOutCubic
      el.textContent = Math.round(target * eased);
      if (k < 1) requestAnimationFrame(step);
    };
    el.textContent = "0";
    requestAnimationFrame(step);
  }
  function runCountUps(root) {
    each((root || document).querySelectorAll(".v[data-v]"), function (el) {
      if (el.getAttribute("data-done")) return;
      el.setAttribute("data-done", "1");
      countUp(el);
    });
  }
  runCountUps();

  /* ---------------- 进度条进场生长 ---------------- */
  function runBars(root) {
    each((root || document).querySelectorAll(".bar > i[data-w]"), function (el) {
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          el.style.width = el.getAttribute("data-w") + "%";
        });
      });
    });
  }
  runBars();

  /* ---------------- 实时页：10s 轮询 + 骨架屏 + 平滑换装 ---------------- */
  var live = document.getElementById("live");
  if (live) {
    var paused = false;
    var timer = null;
    var inflight = false;
    var btn = document.getElementById("live-pause");
    var dot = document.getElementById("live-dot");
    var label = document.getElementById("live-mode");

    var apply = function (html) {
      if (reduced.matches) {
        live.innerHTML = html;
      } else {
        live.classList.add("swapping");
        setTimeout(function () {
          live.innerHTML = html;
          live.classList.remove("swapping");
          runCountUps(live);
          runBars(live);
          each(live.querySelectorAll("[data-stagger]"), function (box) {
            each(box.children, function (el, i) {
              if (!el.style.getPropertyValue("--i")) el.style.setProperty("--i", Math.min(i, 14));
            });
          });
        }, 140);
      }
      var box = live.querySelector("[data-watching]");
      var n = box ? parseInt(box.getAttribute("data-watching"), 10) || 0 : 0;
      if (dot) dot.classList.toggle("idle", !n);
      if (label) label.textContent = n ? n + " 路正在观看" : "当前无人观看";
    };
    var tick = function () {
      if (paused || document.hidden || inflight) return;
      inflight = true;
      fetch("/partials/live", { headers: { Accept: "text/html" }, credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.text();
        })
        .then(apply)
        .catch(function () {
          if (dot) dot.classList.add("idle");
          if (label) label.textContent = "刷新失败，稍后重试";
        })
        .then(function () { inflight = false; });
    };
    var start = function () {
      if (!timer) timer = setInterval(tick, 10000);
    };
    var stop = function () {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
    if (btn) {
      btn.addEventListener("click", function () {
        paused = !paused;
        btn.textContent = paused ? "继续刷新" : "暂停刷新";
        if (dot) dot.classList.toggle("idle", paused);
        if (paused) stop();
        else {
          start();
          tick();
        }
      });
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) tick();
    });
    start();
  }

  /* ---------------- "/" 聚焦搜索 ---------------- */
  var box = document.getElementById("q2");
  if (box) {
    document.addEventListener("keydown", function (e) {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      var el = document.activeElement;
      if (el && /input|textarea|select/i.test(el.tagName)) return;
      e.preventDefault();
      box.focus();
      box.select();
    });
  }

  /* ---------------- 下拉菜单：点击别处自动收起 ---------------- */
  each(document.querySelectorAll("details.drop"), function (d) {
    document.addEventListener("click", function (e) {
      if (d.open && !d.contains(e.target)) d.open = false;
    });
    each(d.querySelectorAll(".menu a"), function (a) {
      a.addEventListener("click", function () { d.open = false; });
    });
  });

  /* ---------------- PWA ---------------- */
  if ("serviceWorker" in navigator && /^https?:$/.test(location.protocol)) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }
})();
