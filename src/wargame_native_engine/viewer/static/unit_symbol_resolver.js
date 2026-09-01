"use strict";

// Presentation-only resolver. The tracked JSON registry is installed by the
// hosting Viewer; this module never guesses identity from a unit name/icon_key.
(function installUnitSymbolResolver(root) {
  const REGISTRY_SCHEMA = "operator-registry/v1";
  const SYMBOL_SCHEMA = "unit-symbol/v1";
  const LAYERS_SCHEMA = "unit-symbol-layers/v1";
  const DEFAULT_REGISTRY_URL = "/static/operator_registry_v1.json";
  const SAFE_LAYER_NAME = /^[a-z0-9_]+$/;
  // A side projection may disclose the broad Source domain while withholding
  // (or lacking) the fine subtype.  These coarse symbols reveal no more than
  // that already-present type code and keep the descriptor UNKNOWN, so no
  // subtype, modifier, or weapon metadata is inferred.
  const SAFE_UNKNOWN_BASES = new Set([
    "unknown",
    "unknown_vehicle",
    "infantry",
  ]);

  function exactInteger(value) {
    return Number.isInteger(value) && typeof value === "number";
  }

  function unknownDescriptor(typeCode, subtypeCode) {
    const key = exactInteger(typeCode) && exactInteger(subtypeCode)
      ? `${typeCode}_${subtypeCode}`
      : "UNKNOWN";
    const baseSymbol = typeCode === 1
      ? "infantry"
      : (typeCode === 2 ? "unknown_vehicle" : "unknown");
    const displayName = typeCode === 1
      ? "人员算子（未细分）"
      : (typeCode === 2 ? "车辆算子（未细分）" : "未知算子");
    return Object.freeze({
      schema: SYMBOL_SCHEMA,
      key,
      display_name: displayName,
      base_symbol: baseSymbol,
      modifiers: Object.freeze([]),
      status: "UNKNOWN",
    });
  }

  function resolveSvgLayers(descriptor) {
    const value = descriptor && typeof descriptor === "object"
      ? descriptor : {};
    const requestedBase = typeof value.base_symbol === "string"
      && SAFE_LAYER_NAME.test(value.base_symbol)
      ? value.base_symbol : "unknown";
    const requestedModifiers = Array.isArray(value.modifiers)
      && value.modifiers.every((item) => (
        typeof item === "string" && SAFE_LAYER_NAME.test(item)
      ))
      ? value.modifiers : [];
    const base = value.status === "UNKNOWN"
      && !SAFE_UNKNOWN_BASES.has(requestedBase)
      ? "unknown" : requestedBase;
    const modifiers = value.status === "UNKNOWN" ? [] : requestedModifiers;
    return Object.freeze({
      schema: LAYERS_SCHEMA,
      layers: Object.freeze([
        `assets/unit_symbols/base/${base}.svg`,
        ...modifiers.map((modifier) => (
          `assets/unit_symbols/modifiers/${modifier}.svg`
        )),
      ]),
    });
  }

  function validateRegistry(document) {
    if (!document || typeof document !== "object" || Array.isArray(document)) {
      throw new TypeError("operator registry must be an object");
    }
    if (document.schema !== REGISTRY_SCHEMA) {
      throw new Error(`operator registry schema must be ${REGISTRY_SCHEMA}`);
    }
    if (!document.operators || typeof document.operators !== "object"
        || Array.isArray(document.operators)) {
      throw new TypeError("operator registry operators must be an object");
    }
    const entries = new Map();
    Object.entries(document.operators).forEach(([key, value]) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError(`operator registry entry ${key} must be an object`);
      }
      const identity = value.identity;
      const symbol = value.symbol;
      if (!identity || !exactInteger(identity.type_code)
          || !exactInteger(identity.subtype_code)
          || `${identity.type_code}_${identity.subtype_code}` !== key) {
        throw new Error(`operator registry entry ${key} has invalid identity`);
      }
      if (!symbol || typeof symbol.base !== "string"
          || !Array.isArray(symbol.modifiers)
          || !symbol.modifiers.every((item) => typeof item === "string")) {
        throw new Error(`operator registry entry ${key} has invalid symbol`);
      }
      entries.set(key, Object.freeze({
        display_name: String(value.display_name),
        base_symbol: symbol.base,
        modifiers: Object.freeze([...symbol.modifiers]),
      }));
    });
    return entries;
  }

  function create(document) {
    const entries = validateRegistry(document);

    function resolveOperator(viewerUnit) {
      if (!viewerUnit || typeof viewerUnit !== "object"
          || Array.isArray(viewerUnit)) {
        throw new TypeError("viewer_unit must be an object");
      }
      const typeCode = exactInteger(viewerUnit.type_code)
        ? viewerUnit.type_code
        : viewerUnit.type;
      const subtypeCode = exactInteger(viewerUnit.subtype_code)
        ? viewerUnit.subtype_code
        : (exactInteger(viewerUnit.sub_type_code)
          ? viewerUnit.sub_type_code
          : viewerUnit.sub_type);
      if (!exactInteger(typeCode) || !exactInteger(subtypeCode)) {
        return unknownDescriptor(typeCode, subtypeCode);
      }
      const key = `${typeCode}_${subtypeCode}`;
      const mapped = entries.get(key);
      if (!mapped) {
        return unknownDescriptor(typeCode, subtypeCode);
      }
      return Object.freeze({
        schema: SYMBOL_SCHEMA,
        key,
        display_name: mapped.display_name,
        base_symbol: mapped.base_symbol,
        modifiers: Object.freeze([...mapped.modifiers]),
        status: "MAPPED",
      });
    }

    return Object.freeze({
      schema: SYMBOL_SCHEMA,
      resolveOperator,
      resolveUnitSymbol: resolveOperator,
      resolveSvgLayers,
    });
  }

  let activeResolver = null;
  let registryPromise = null;
  function resolveActive(viewerUnit) {
    if (activeResolver === null) {
      const typeCode = viewerUnit?.type_code ?? viewerUnit?.type;
      const subtypeCode = viewerUnit?.subtype_code
        ?? viewerUnit?.sub_type_code
        ?? viewerUnit?.sub_type;
      return unknownDescriptor(typeCode, subtypeCode);
    }
    return activeResolver.resolveOperator(viewerUnit);
  }

  const publicApi = Object.freeze({
    schema: SYMBOL_SCHEMA,
    registry_schema: REGISTRY_SCHEMA,
    create,
    installRegistry(document) {
      activeResolver = create(document);
      return activeResolver;
    },
    loadRegistry(url = DEFAULT_REGISTRY_URL) {
      if (activeResolver !== null) return Promise.resolve(activeResolver);
      if (registryPromise !== null) return registryPromise;
      if (typeof root.fetch !== "function") {
        return Promise.reject(new Error("fetch is unavailable for operator registry"));
      }
      registryPromise = root.fetch(url, { cache: "no-store" })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`operator registry request failed: ${response.status}`);
          }
          return response.json();
        })
        .then((document) => publicApi.installRegistry(document))
        .catch((error) => {
          registryPromise = null;
          throw error;
        });
      return registryPromise;
    },
    resolveOperator: resolveActive,
    resolveUnitSymbol: resolveActive,
    resolveSvgLayers,
  });

  root.UnitSymbolResolver = publicApi;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = publicApi;
  }
}(typeof globalThis !== "undefined" ? globalThis : window));
