/* PicNamer Service Worker（阶段三）
 * 策略（ERR-010 后改为「网络优先」）：
 *   - 所有静态资源（/、CSS、JS、manifest、图标）：网络优先，成功即更新缓存，
 *     离线时回落缓存——本应用高频迭代，新鲜度必须优先；
 *     （v1 曾用缓存优先，导致改版后旧前端顶住新版本、"修复无效"的假象）
 *   - /api/*：一律不缓存（数据与缩略图必须实时，AI 推理永远需要后端在线）。
 * 前端发布仍建议把 CACHE 版本号 +1，确保已打开的页面尽快换新。
 */
"use strict";

const CACHE = "picnamer-v2";
const SHELL = [
  "/",
  "/css/style.css",
  "/js/app.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-512-maskable.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;

  e.respondWith(
    fetch(e.request).then((resp) => {
      if (resp.ok && url.origin === location.origin) {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return resp;
    }).catch(() =>
      caches.match(e.request).then((hit) => {
        if (hit) return hit;
        if (e.request.mode === "navigate") return caches.match("/");
        return undefined;
      })
    )
  );
});
