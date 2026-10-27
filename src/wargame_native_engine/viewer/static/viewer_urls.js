/* Same-origin routes shared by standalone and prefix-stripping notebook proxies. */
(() => {
  "use strict";
  const base = new URL(document.baseURI);
  if (base.origin !== window.location.origin || !base.pathname.endsWith("/")) {
    throw new Error("Invalid viewer mount: expected a same-origin directory");
  }

  function resolve(path) {
    // Accept application paths only. Never let a token-bearing request escape
    // the viewer mount through an absolute URL or a dot-segment traversal.
    if (typeof path !== "string" || !/^\/(api|static)\//.test(path) || path.includes("\\")) {
      throw new Error("Invalid viewer resource path");
    }
    const decodedPath = decodeURIComponent(path.split(/[?#]/, 1)[0]);
    if (decodedPath.includes("\\") || decodedPath.split("/").some(part => part === "." || part === "..")) {
      throw new Error("Viewer resource traversal is not allowed");
    }
    const url = new URL(path.slice(1), base);
    if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) {
      throw new Error("Viewer resource is outside its mount");
    }
    return url.pathname + url.search;
  }

  globalThis.ViewerUrls = Object.freeze({ resolve });
})();
