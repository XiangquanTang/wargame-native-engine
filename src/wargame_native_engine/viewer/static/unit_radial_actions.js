"use strict";

// Layout only: controllers retain selection, legal actions and pending commands.
// CSS places circles in a compact rectangle, independent of unit/map coordinates.
(function installUnitRadialActions(root) {
  function layout(element, renderer, actorIds) {
    if (!element) return;
    const buttons = Array.from(element.querySelectorAll("button"));
    element.hidden = !(actorIds?.length && buttons.length);
  }
  root.UnitRadialActions = Object.freeze({ layout });
})(typeof window === "undefined" ? globalThis : window);
