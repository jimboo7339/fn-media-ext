/* 观影时间线 service worker。
 *
 * 策略：静态资源缓存优先——base.html 里 app.css/app.js 的引用带 ?v=<版本号>，
 * 每次发版 URL 变化，旧缓存自然淘汰，不存在「改了样式不生效」的问题。
 * 页面与接口永远走网络：这个应用的全部价值在于数据新鲜度，不做离线页面。
 * 改缓存策略时同步升级 CACHE 版本号。
 */
var CACHE = "fnxt-static-v1";
var ASSETS = [
  "/static/app.css",
  "/static/app.js",
  "/static/favicon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png"
];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(ASSETS); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CACHE; })
          .map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (!url.pathname.startsWith("/static/")) return;
  e.respondWith(
    caches.match(e.request).then(function (hit) {
      if (hit) return hit;
      return fetch(e.request).then(function (resp) {
        if (resp.ok) {
          var copy = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
        }
        return resp;
      });
    })
  );
});
