"use strict";

// SVG-to-Canvas presentation adapter. It owns no unit semantics and never
// inspects weapons, actions, adjudication state, or hidden observations.
(function installUnitSymbolRenderer(root) {
  const CACHE_SCHEMA = "unit-symbol-cache/v1";
  const SAFE_LAYER_PATH = /^assets\/unit_symbols\/(base|modifiers)\/[a-z0-9_]+\.svg$/;
  const SIDE_COLORS = Object.freeze({
    RED: "#d33",
    BLUE: "#36f",
    UNKNOWN: "#777777",
    DESTROYED: "#777777",
  });

  function normalizeSide(value) {
    const side = typeof value === "string" ? value.toUpperCase() : "UNKNOWN";
    return side === "RED" || side === "BLUE" ? side : "UNKNOWN";
  }

  function renderPalette(descriptor) {
    if (descriptor?.status === "destroyed") return "DESTROYED";
    return normalizeSide(descriptor?.side);
  }

  function svgLayers(descriptor) {
    const resolver = root.UnitSymbolResolver;
    if (!resolver || typeof resolver.resolveSvgLayers !== "function") return [];
    const layers = resolver.resolveSvgLayers(descriptor).layers;
    return layers.every((path) => SAFE_LAYER_PATH.test(path)) ? layers : [];
  }

  class UnitSymbolCache {
    constructor({
      fetchImpl = typeof root.fetch === "function" ? root.fetch.bind(root) : null,
      ImageClass = root.Image,
      BlobClass = root.Blob,
      URLApi = root.URL,
      onReady = null,
    } = {}) {
      this.fetchImpl = fetchImpl;
      this.ImageClass = ImageClass;
      this.BlobClass = BlobClass;
      this.URLApi = URLApi;
      this.onReady = typeof onReady === "function" ? onReady : null;
      this.symbols = new Map();
      this.layerImages = new Map();
    }

    symbolKey(descriptor, layers = svgLayers(descriptor)) {
      const palette = renderPalette(descriptor);
      const names = layers.map((path) => path.split("/").pop().replace(/\.svg$/, ""));
      return `${palette}:${names.join(":")}`;
    }

    get(descriptor) {
      const layers = svgLayers(descriptor);
      if (!layers.length) return Object.freeze({ status: "FAILED", images: [] });
      const key = this.symbolKey(descriptor, layers);
      let entry = this.symbols.get(key);
      if (entry) return entry;
      entry = { status: "PENDING", images: [], error: null };
      this.symbols.set(key, entry);
      const palette = renderPalette(descriptor);
      const color = SIDE_COLORS[palette];
      Promise.all(layers.map((path) => this._loadLayer(path, color)))
        .then((images) => {
          entry.status = "READY";
          entry.images = images;
          if (this.onReady) this.onReady(key, "READY");
        })
        .catch((error) => {
          entry.status = "FAILED";
          entry.error = error;
          if (this.onReady) this.onReady(key, "FAILED");
        });
      return entry;
    }

    _loadLayer(path, color) {
      const key = `${color}:${path}`;
      if (this.layerImages.has(key)) return this.layerImages.get(key);
      const promise = this._fetchSvgImage(path, color);
      this.layerImages.set(key, promise);
      return promise;
    }

    _fetchSvgImage(path, color) {
      if (!this.fetchImpl || typeof this.ImageClass !== "function"
          || typeof this.BlobClass !== "function"
          || !this.URLApi || typeof this.URLApi.createObjectURL !== "function") {
        return Promise.reject(new Error("SVG image loading is unavailable"));
      }
      return this.fetchImpl(`/static/${path}`, { cache: "force-cache" })
        .then((response) => {
          if (!response.ok) throw new Error(`SVG request failed: ${response.status}`);
          return response.text();
        })
        .then((source) => new Promise((resolve, reject) => {
          const colored = source.replace(/currentColor/g, color);
          const blob = new this.BlobClass([colored], {
            type: "image/svg+xml;charset=utf-8",
          });
          const objectUrl = this.URLApi.createObjectURL(blob);
          const image = new this.ImageClass();
          image.onload = () => {
            this.URLApi.revokeObjectURL(objectUrl);
            resolve(image);
          };
          image.onerror = () => {
            this.URLApi.revokeObjectURL(objectUrl);
            reject(new Error(`invalid SVG image: ${path}`));
          };
          image.src = objectUrl;
        }));
    }
  }

  class UnitSymbolRenderer {
    constructor({ cache = null, onReady = null } = {}) {
      this.cache = cache || new UnitSymbolCache({ onReady });
    }

    drawUnitSymbol(context, x, y, descriptor, size = 24) {
      if (!context || typeof context.drawImage !== "function"
          || !Number.isFinite(x) || !Number.isFinite(y)
          || !Number.isFinite(size) || size <= 0) {
        return false;
      }
      const entry = this.cache.get(descriptor);
      if (entry.status !== "READY" || !entry.images.length) return false;
      const origin = -size / 2;
      context.save();
      context.translate(x, y);
      for (const image of entry.images) {
        context.drawImage(image, origin, origin, size, size);
      }
      context.restore();
      return true;
    }
  }

  const api = Object.freeze({
    schema: CACHE_SCHEMA,
    SIDE_COLORS,
    UnitSymbolCache,
    UnitSymbolRenderer,
    create(options = {}) {
      return new UnitSymbolRenderer(options);
    },
  });
  root.UnitSymbolRenderer = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof globalThis !== "undefined" ? globalThis : window));
