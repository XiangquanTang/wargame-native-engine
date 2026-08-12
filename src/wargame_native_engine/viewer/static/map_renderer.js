"use strict";

/*
 * Browser-native migration of the benchmark PyQt odd-r map presentation.
 * This module owns presentation geometry only: it never adjudicates movement,
 * visibility, terrain effects, actions, or replay progression.
 */
(function installReplayMapRenderer(globalObject) {
  const SQRT3 = Math.sqrt(3);
  const TERRAIN_COLORS = [
    "#f7e7b2", "#e7c96f", "#d1a64a", "#b78332",
    "#8d6124", "#6a431c", "#4a3217", "#332611",
  ];
  const DIRECTION_ANGLES = [0, -60, -120, 180, 120, 60];

  function hexToPixel(row, column, hexSize) {
    if (!Number.isInteger(row) || !Number.isInteger(column) || row < 0 || column < 0) {
      throw new Error("row and column must be non-negative integers");
    }
    if (!(hexSize > 0)) throw new Error("hexSize must be positive");
    const width = SQRT3 * hexSize;
    return {
      x: column * width + (row % 2 ? width / 2 : 0) + width / 2,
      y: row * hexSize * 1.5 + hexSize,
    };
  }

  function hexVertices(row, column, hexSize) {
    const center = hexToPixel(row, column, hexSize);
    return Array.from({ length: 6 }, (_, index) => {
      const angle = (60 * index - 90) * Math.PI / 180;
      return {
        x: center.x + hexSize * Math.cos(angle),
        y: center.y + hexSize * Math.sin(angle),
      };
    });
  }

  function directionalSegment(row, column, direction, hexSize, edgeFraction = 0.92) {
    if (!Number.isInteger(direction) || direction < 0 || direction > 5) {
      throw new Error("hex direction must be within [0, 5]");
    }
    const center = hexToPixel(row, column, hexSize);
    const angle = DIRECTION_ANGLES[direction] * Math.PI / 180;
    return [center, {
      x: center.x + Math.cos(angle) * hexSize * edgeFraction,
      y: center.y + Math.sin(angle) * hexSize * edgeFraction,
    }];
  }

  function riverEdgeSegment(row, column, direction, hexSize) {
    const [center, midpoint] = directionalSegment(
      row, column, direction, hexSize, SQRT3 / 2,
    );
    const dx = midpoint.x - center.x;
    const dy = midpoint.y - center.y;
    const length = Math.hypot(dx, dy) || 1;
    const tangent = { x: -dy / length, y: dx / length };
    const half = hexSize / 2;
    return [
      { x: midpoint.x - tangent.x * half, y: midpoint.y - tangent.y * half },
      { x: midpoint.x + tangent.x * half, y: midpoint.y + tangent.y * half },
    ];
  }

  function stackUnits(units) {
    const stacks = new Map();
    for (const unit of units || []) {
      const key = `${unit.row}:${unit.column}`;
      if (!stacks.has(key)) stacks.set(key, []);
      stacks.get(key).push(unit);
    }
    return Array.from(stacks.values())
      .map((values) => values.sort((left, right) => {
        const category = (left.category === "operator" ? 0 : 1)
          - (right.category === "operator" ? 0 : 1);
        return category || Number(left.operator_id) - Number(right.operator_id);
      }))
      .sort((left, right) => (
        left[0].row - right[0].row
        || left[0].column - right[0].column
      ));
  }

  function unitSideColor(side) {
    if (side === "red") return "#e84545";
    if (side === "blue") return "#3388e8";
    return "#777777";
  }

  function elevationColor(elevation) {
    if (typeof elevation !== "number" || !Number.isFinite(elevation)) return "#4d555b";
    const ratio = Math.max(0, Math.min(1, elevation / 70));
    const index = Math.min(6, Math.floor(ratio * 7));
    return TERRAIN_COLORS[Math.round(index * (TERRAIN_COLORS.length - 1) / 6)];
  }

  class MapRenderer {
    constructor(canvas, { onUnitSelected = null } = {}) {
      this.canvas = canvas;
      this.context = canvas.getContext("2d");
      this.onUnitSelected = onUnitSelected;
      this.map = null;
      this.frame = null;
      this.selectedUnitId = null;
      this.scale = 1;
      this.offsetX = 0;
      this.offsetY = 0;
      this.hitTargets = [];
      this.drag = null;
      this.hasFitted = false;
      this._bindEvents();
    }

    setMap(map) {
      this.map = map;
      this.hasFitted = false;
      this.resetView();
    }

    render(frame, selectedUnitId) {
      this.frame = frame;
      this.selectedUnitId = selectedUnitId;
      const resized = this._resizeCanvas();
      if (!this.hasFitted || resized && this.scale === 1) this.resetView();
      const context = this.context;
      const width = this.canvas.clientWidth;
      const height = this.canvas.clientHeight;
      context.clearRect(0, 0, width, height);
      context.fillStyle = "#171716";
      context.fillRect(0, 0, width, height);
      this.hitTargets = [];
      if (!this.map) return;
      this._drawMap(context);
      this._drawObjectives(context, frame?.cities || []);
      this._drawUnits(context, frame?.viewer_units || []);
    }

    resetView() {
      if (!this.map) return;
      this._resizeCanvas();
      const bounds = this._worldBounds();
      const width = Math.max(1, this.canvas.clientWidth);
      const height = Math.max(1, this.canvas.clientHeight);
      const margin = 28;
      const worldWidth = Math.max(1, bounds.maxX - bounds.minX);
      const worldHeight = Math.max(1, bounds.maxY - bounds.minY);
      this.scale = Math.min(
        (width - margin * 2) / worldWidth,
        (height - margin * 2) / worldHeight,
      );
      this.scale = Math.max(0.05, Math.min(20, this.scale));
      this.offsetX = (width - worldWidth * this.scale) / 2 - bounds.minX * this.scale;
      this.offsetY = (height - worldHeight * this.scale) / 2 - bounds.minY * this.scale;
      this.hasFitted = true;
      if (this.frame) this.render(this.frame, this.selectedUnitId);
    }

    _bindEvents() {
      this.canvas.style.touchAction = "none";
      this.canvas.addEventListener("wheel", (event) => {
        event.preventDefault();
        const point = this._eventPoint(event);
        const oldScale = this.scale;
        const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
        const nextScale = Math.max(0.05, Math.min(20, oldScale * factor));
        const worldX = (point.x - this.offsetX) / oldScale;
        const worldY = (point.y - this.offsetY) / oldScale;
        this.scale = nextScale;
        this.offsetX = point.x - worldX * nextScale;
        this.offsetY = point.y - worldY * nextScale;
        this.render(this.frame, this.selectedUnitId);
      }, { passive: false });
      this.canvas.addEventListener("pointerdown", (event) => {
        const point = this._eventPoint(event);
        this.canvas.setPointerCapture(event.pointerId);
        this.drag = {
          pointerId: event.pointerId,
          startX: point.x,
          startY: point.y,
          offsetX: this.offsetX,
          offsetY: this.offsetY,
          moved: false,
        };
      });
      this.canvas.addEventListener("pointermove", (event) => {
        if (!this.drag || this.drag.pointerId !== event.pointerId) return;
        const point = this._eventPoint(event);
        const dx = point.x - this.drag.startX;
        const dy = point.y - this.drag.startY;
        if (Math.hypot(dx, dy) > 3) this.drag.moved = true;
        if (this.drag.moved) {
          this.offsetX = this.drag.offsetX + dx;
          this.offsetY = this.drag.offsetY + dy;
          this.render(this.frame, this.selectedUnitId);
        }
      });
      this.canvas.addEventListener("pointerup", (event) => {
        if (!this.drag || this.drag.pointerId !== event.pointerId) return;
        const moved = this.drag.moved;
        this.drag = null;
        if (!moved) {
          const point = this._eventPoint(event);
          const hit = [...this.hitTargets].reverse().find((target) => (
            Math.hypot(target.x - point.x, target.y - point.y) <= target.radius
          ));
          if (hit && this.onUnitSelected) this.onUnitSelected(hit.operatorId);
        }
      });
      this.canvas.addEventListener("pointercancel", () => { this.drag = null; });
    }

    _resizeCanvas() {
      const ratio = globalObject.devicePixelRatio || 1;
      const rect = this.canvas.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width * ratio));
      const height = Math.max(1, Math.round(rect.height * ratio));
      const changed = this.canvas.width !== width || this.canvas.height !== height;
      if (changed) {
        this.canvas.width = width;
        this.canvas.height = height;
      }
      this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
      return changed;
    }

    _eventPoint(event) {
      const rect = this.canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    _worldBounds() {
      const size = this.map.hex_size;
      const bounds = this.map.bounds;
      const corners = [
        hexVertices(bounds.min_row, bounds.min_column, size),
        hexVertices(bounds.min_row, bounds.max_column, size),
        hexVertices(bounds.max_row, bounds.min_column, size),
        hexVertices(bounds.max_row, bounds.max_column, size),
      ].flat();
      return {
        minX: Math.min(...corners.map((point) => point.x)),
        maxX: Math.max(...corners.map((point) => point.x)),
        minY: Math.min(...corners.map((point) => point.y)),
        maxY: Math.max(...corners.map((point) => point.y)),
      };
    }

    _screen(point) {
      return {
        x: point.x * this.scale + this.offsetX,
        y: point.y * this.scale + this.offsetY,
      };
    }

    _drawMap(context) {
      const cells = this.map.cells || [];
      if (cells.length) {
        for (const cell of cells) this._drawCell(context, cell, true);
        return;
      }
      const bounds = this.map.bounds;
      const total = this.map.rows * this.map.columns;
      if (total > 20_000) return;
      for (let row = bounds.min_row; row <= bounds.max_row; row += 1) {
        for (let column = bounds.min_column; column <= bounds.max_column; column += 1) {
          this._drawCell(context, {
            row, column, elevation: null, terrain_semantic: "unknown",
            roads: [0, 0, 0, 0, 0, 0], rivers: [0, 0, 0, 0, 0, 0],
          }, false);
        }
      }
    }

    _drawCell(context, cell, configured) {
      const vertices = hexVertices(cell.row, cell.column, this.map.hex_size)
        .map((point) => this._screen(point));
      context.beginPath();
      vertices.forEach((point, index) => index
        ? context.lineTo(point.x, point.y)
        : context.moveTo(point.x, point.y));
      context.closePath();
      context.fillStyle = configured ? elevationColor(cell.elevation) : "#25313a";
      context.fill();
      context.strokeStyle = configured ? "#493b18aa" : "#53616a88";
      context.lineWidth = Math.max(0.45, Math.min(1.2, this.scale * 0.6));
      context.stroke();
      if (cell.terrain_semantic === "water") {
        context.fillStyle = "#397fc0dd"; context.fill();
      }
      this._drawTerrainSymbol(context, cell);
      (cell.roads || []).forEach((roadType, direction) => {
        if (roadType) this._drawRoad(context, cell, direction, roadType);
      });
      (cell.rivers || []).forEach((riverType, direction) => {
        if (riverType) this._drawRiver(context, cell, direction);
      });
    }

    _drawTerrainSymbol(context, cell) {
      const center = this._screen(hexToPixel(cell.row, cell.column, this.map.hex_size));
      const radius = Math.max(1.5, Math.min(5, this.map.hex_size * this.scale * 0.16));
      if (cell.terrain_semantic === "forest") {
        context.fillStyle = "#326b35";
        for (const [dx, dy] of [[-radius, 1], [radius, 2], [0, -radius]]) {
          context.beginPath(); context.arc(center.x + dx, center.y + dy, radius, 0, Math.PI * 2); context.fill();
        }
      } else if (cell.terrain_semantic === "settlement") {
        context.fillStyle = "#72564a";
        context.fillRect(center.x - radius * 1.3, center.y - radius, radius * 1.2, radius * 1.5);
        context.fillRect(center.x + radius * 0.2, center.y, radius * 1.2, radius * 1.5);
      } else if (cell.terrain_semantic === "soft_ground") {
        context.strokeStyle = "#665d3f";
        for (const offset of [-radius, 0, radius]) {
          context.beginPath(); context.moveTo(center.x - radius * 1.5, center.y + offset);
          context.lineTo(center.x + radius * 1.5, center.y + offset + radius * 0.45); context.stroke();
        }
      }
    }

    _drawRoad(context, cell, direction, roadType) {
      const [start, end] = directionalSegment(cell.row, cell.column, direction, this.map.hex_size);
      const a = this._screen(start), b = this._screen(end);
      context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y);
      context.strokeStyle = roadType === 2 ? "#5c5143" : "#c4a15e";
      context.lineWidth = Math.max(1.2, this.map.hex_size * this.scale * (roadType === 2 ? 0.16 : 0.12));
      context.setLineDash(roadType === 2 ? [5, 3] : []); context.stroke(); context.setLineDash([]);
    }

    _drawRiver(context, cell, direction) {
      const [start, end] = riverEdgeSegment(cell.row, cell.column, direction, this.map.hex_size);
      const a = this._screen(start), b = this._screen(end);
      context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y);
      context.strokeStyle = "#3e9be6";
      context.lineWidth = Math.max(1.8, this.map.hex_size * this.scale * 0.2);
      context.stroke();
    }

    _drawObjectives(context, cities) {
      for (const city of cities) {
        if (!Number.isInteger(city.coord) || city.coord < 0) continue;
        const center = this._screen(hexToPixel(
          Math.floor(city.coord / 100), city.coord % 100, this.map.hex_size,
        ));
        context.beginPath(); context.arc(center.x, center.y, 6, 0, Math.PI * 2);
        context.fillStyle = city.flag === 0 ? "#df3d45" : city.flag === 1 ? "#357edb" : "#d7c66b";
        context.fill(); context.strokeStyle = "#fff3ad"; context.lineWidth = 1.5; context.stroke();
      }
    }

    _drawUnits(context, units) {
      for (const stack of stackUnits(units)) {
        const center = this._screen(hexToPixel(stack[0].row, stack[0].column, this.map.hex_size));
        const spread = stack.length > 1 ? 18 + Math.min(12, stack.length * 1.5) : 0;
        stack.forEach((unit, index) => {
          const angle = stack.length > 1 ? Math.PI * 2 * index / stack.length - Math.PI / 2 : 0;
          const x = center.x + Math.cos(angle) * spread;
          const y = center.y + Math.sin(angle) * spread;
          this._drawUnit(context, unit, x, y);
        });
        if (stack.length > 1) {
          context.beginPath(); context.arc(center.x + 10, center.y - 12, 9, 0, Math.PI * 2);
          context.fillStyle = "#222"; context.fill(); context.strokeStyle = "#fff"; context.stroke();
          context.fillStyle = "#fff"; context.font = "11px ui-monospace";
          context.textAlign = "center"; context.fillText(String(stack.length), center.x + 10, center.y - 8);
        }
      }
    }

    _drawUnit(context, unit, x, y) {
      const selected = unit.operator_id === this.selectedUnitId;
      const radius = selected ? 12 : 9;
      const color = unit.destroyed ? "#777777" : unitSideColor(unit.side);
      context.save(); context.translate(x, y);
      context.fillStyle = color; context.strokeStyle = selected ? "#ffe15a" : "#ffffff";
      context.lineWidth = selected ? 3 : 1.5;
      this._unitShape(context, unit.icon_key, radius);
      context.fill(); context.stroke();
      if (unit.category === "passenger") {
        context.beginPath(); context.arc(0, 0, radius + 3, 0, Math.PI * 2);
        context.strokeStyle = "#ffc857"; context.lineWidth = 2.5; context.stroke();
      }
      if (unit.destroyed) {
        context.beginPath(); context.moveTo(-radius, -radius); context.lineTo(radius, radius);
        context.moveTo(radius, -radius); context.lineTo(-radius, radius);
        context.strokeStyle = "#dddddd"; context.lineWidth = 2; context.stroke();
      }
      if (unit.lose_control || unit.can_to_move === false) {
        context.beginPath(); context.arc(0, 0, radius + 5, Math.PI * 0.15, Math.PI * 0.85);
        context.strokeStyle = "#ffc857"; context.lineWidth = 2; context.stroke();
      }
      if (unit.stopped) {
        context.fillStyle = "#ffffff"; context.fillRect(-2, -2, 4, 4);
      }
      const health = typeof unit.health_ratio === "number" ? Math.max(0, Math.min(1, unit.health_ratio)) : null;
      if (health !== null) {
        context.fillStyle = "#222"; context.fillRect(-radius, radius + 3, radius * 2, 3);
        context.fillStyle = health >= 0.6 ? "#39d353" : health >= 0.3 ? "#e6a23c" : "#d9363e";
        context.fillRect(-radius, radius + 3, radius * 2 * health, 3);
      }
      context.fillStyle = "#f5f5f5"; context.font = "11px ui-monospace";
      context.textAlign = "left"; context.fillText(String(unit.operator_id), radius + 4, 4);
      context.restore();
      this.hitTargets.push({ x, y, radius: radius + 7, operatorId: unit.operator_id });
    }

    _unitShape(context, kind, radius) {
      context.beginPath();
      if (kind === "tank") {
        context.rect(-radius, -radius * 0.58, radius * 2, radius * 1.16);
      } else if (kind === "infantry") {
        context.moveTo(0, -radius); context.lineTo(radius, radius); context.lineTo(-radius, radius); context.closePath();
      } else if (kind === "drone" || kind === "helicopter") {
        context.arc(0, 0, radius * 0.45, 0, Math.PI * 2);
        context.moveTo(-radius, -radius); context.lineTo(radius, radius);
        context.moveTo(-radius, radius); context.lineTo(radius, -radius);
      } else if (kind === "artillery") {
        context.arc(0, 0, radius, 0, Math.PI * 2);
        context.moveTo(0, 0); context.lineTo(radius * 1.3, -radius * 0.5);
      } else if (kind === "facility") {
        context.rect(-radius, -radius, radius * 2, radius * 2);
      } else {
        context.moveTo(0, -radius); context.lineTo(radius, 0); context.lineTo(0, radius); context.lineTo(-radius, 0); context.closePath();
      }
    }
  }

  const api = {
    MapRenderer,
    directionalSegment,
    hexToPixel,
    hexVertices,
    riverEdgeSegment,
    stackUnits,
    unitSideColor,
  };
  globalObject.ReplayMapRenderer = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof window !== "undefined" ? window : globalThis));
