// Small progressive enhancements; every page works without JS.
(function () {
  "use strict";

  /* ---------------- theme: auto / light / dark ---------------- */
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

  /* ---------------- live page: poll a fragment every 10s ---------------- */
  var live = document.getElementById("live");
  if (live) {
    var paused = false;
    var timer = null;
    var inflight = false;
    var btn = document.getElementById("live-pause");
    var dot = document.getElementById("live-dot");
    var label = document.getElementById("live-mode");

    var apply = function (html) {
      live.innerHTML = html;
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
        .then(function (html) {
          if (html) apply(html);
        })
        .catch(function () {
          // 失败不能静默：保留上一次的内容，把状态说出来
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

  /* ---------------- search: "/" jumps to the sidebar search box ---------------- */
  var box = document.getElementById("q");
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

  /* ---------------- mobile: drawer + bottom nav ---------------- */
  var side = document.getElementById("side");
  var scrim = document.getElementById("scrim");
  var menuBtn = document.getElementById("menu-btn");
  var closeBtn = document.getElementById("drawer-close");
  var moreBtn = document.getElementById("more-btn");
  var drawerReturnFocus = null;

  function drawerOpen() {
    return side.classList.contains("open");
  }
  function setDrawer(open) {
    if (!side) return;
    side.classList.toggle("open", open);
    if (scrim) scrim.classList.toggle("on", open);
    if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    side.setAttribute("aria-modal", open ? "true" : "false");
    document.documentElement.style.overflow = open ? "hidden" : "";
    if (open) {
      // 抽屉是临时模态：记住出发点，把焦点移进抽屉，Tab 循环限制在其中
      drawerReturnFocus = document.activeElement;
      var target = closeBtn || side.querySelector("a[href], button");
      if (target) target.focus();
    } else if (drawerReturnFocus && drawerReturnFocus.focus) {
      drawerReturnFocus.focus();
      drawerReturnFocus = null;
    }
  }
  if (side) {
    each([menuBtn, closeBtn, moreBtn], function (b) {
      if (b) b.addEventListener("click", function () { setDrawer(true); });
    });
    if (scrim) scrim.addEventListener("click", function () { setDrawer(false); });
    document.addEventListener("keydown", function (e) {
      if (!drawerOpen()) return;
      if (e.key === "Escape") {
        setDrawer(false);
        return;
      }
      if (e.key !== "Tab") return;
      var focusables = side.querySelectorAll("a[href], button:not([disabled]), input, select");
      if (!focusables.length) return;
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      if (e.shiftKey && (document.activeElement === first || !side.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !side.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    });
    // 点抽屉里的链接后自动收起
    each(side.querySelectorAll("a"), function (a) {
      a.addEventListener("click", function () { setDrawer(false); });
    });
    // 从窄屏拉宽到宽屏时复位，避免侧栏被锁住
    var wide = window.matchMedia("(min-width: 901px)");
    var onWide = function (e) { if (e.matches) setDrawer(false); };
    if (wide.addEventListener) wide.addEventListener("change", onWide);
    else if (wide.addListener) wide.addListener(onWide);
  }

  /* ---------------- theme: single-button cycle (mobile) ---------------- */
  var cycle = document.getElementById("theme-cycle");
  if (cycle) {
    var order = ["auto", "light", "dark"];
    var marks = { auto: "跟随系统", light: "浅色", dark: "深色" };
    function paintCycle() {
      var p = pref();
      cycle.setAttribute("title", "主题：" + marks[p]);
      cycle.setAttribute("aria-label", "切换主题，当前" + marks[p]);
    }
    cycle.addEventListener("click", function () {
      save(order[(order.indexOf(pref()) + 1) % order.length]);
      paintCycle();
    });
    paintCycle();
  }

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }
})();
