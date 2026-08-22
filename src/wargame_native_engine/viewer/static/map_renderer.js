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
  const DIRECTION_LABELS = ["E", "NE", "NW", "W", "SW", "SE"];
  const SELECTED_UNIT_SCREEN_RADIUS = 19;
  const MAX_STACK_SCREEN_SPREAD = 30;

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

  function pixelToHex(x, y, hexSize, bounds = null) {
    if (![x, y, hexSize].every(Number.isFinite) || !(hexSize > 0)) {
      throw new Error("pixel coordinates and hexSize must be finite");
    }
    const width = SQRT3 * hexSize;
    const rowGuess = Math.round((y - hexSize) / (hexSize * 1.5));
    const minRow = Number.isInteger(bounds?.min_row) ? bounds.min_row : 0;
    const maxRow = Number.isInteger(bounds?.max_row)
      ? bounds.max_row : rowGuess + 2;
    const minColumn = Number.isInteger(bounds?.min_column) ? bounds.min_column : 0;
    const maxColumn = Number.isInteger(bounds?.max_column)
      ? bounds.max_column : Number.MAX_SAFE_INTEGER;
    let best = null;
    let bestDistance = Infinity;
    for (let row = Math.max(minRow, rowGuess - 2); row <= Math.min(maxRow, rowGuess + 2); row += 1) {
      const columnGuess = Math.round((
        x - width / 2 - (row % 2 ? width / 2 : 0)
      ) / width);
      for (let column = Math.max(minColumn, columnGuess - 2);
        column <= Math.min(maxColumn, columnGuess + 2); column += 1) {
        const center = hexToPixel(row, column, hexSize);
        const distance = Math.hypot(x - center.x, y - center.y);
        if (distance < bestDistance) {
          bestDistance = distance;
          best = { row, column };
        }
      }
    }
    return bestDistance <= hexSize * 1.05 ? best : null;
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

  function edgeFacts(values) {
    if (!Array.isArray(values) || values.length !== 6) return [];
    return values.flatMap((value, direction) => (
      Number.isInteger(value) && value > 0
        ? [{ direction, label: DIRECTION_LABELS[direction], type: value }]
        : []
    ));
  }

  function formatEdgeFacts(values) {
    const facts = edgeFacts(values);
    return facts.length
      ? facts.map((item) => `${item.label}(d${item.direction})=type${item.type}`).join(",")
      : "none";
  }

  function summarizeMapLayers(mapData) {
    const cells = Array.isArray(mapData?.cells) ? mapData.cells : [];
    const terrainCounts = {};
    const roadTypes = new Set();
    const riverTypes = new Set();
    let elevationCount = 0;
    let roadEdgeCount = 0;
    let riverEdgeCount = 0;
    for (const cell of cells) {
      const semantic = typeof cell.terrain_semantic === "string"
        ? cell.terrain_semantic : "unknown";
      terrainCounts[semantic] = (terrainCounts[semantic] || 0) + 1;
      if (typeof cell.elevation === "number" && Number.isFinite(cell.elevation)) {
        elevationCount += 1;
      }
      for (const fact of edgeFacts(cell.roads)) {
        roadEdgeCount += 1;
        roadTypes.add(fact.type);
      }
      for (const fact of edgeFacts(cell.rivers)) {
        riverEdgeCount += 1;
        riverTypes.add(fact.type);
      }
    }
    return {
      configured_cell_count: cells.length,
      elevation_cell_count: elevationCount,
      terrain_counts: Object.fromEntries(Object.entries(terrainCounts).sort()),
      road_edge_count: roadEdgeCount,
      road_types: Array.from(roadTypes).sort((left, right) => left - right),
      river_edge_count: riverEdgeCount,
      river_types: Array.from(riverTypes).sort((left, right) => left - right),
    };
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

  function stackSpread(unitCount) {
    return unitCount > 1 ? 18 + Math.min(12, unitCount * 1.5) : 0;
  }

  function computeWorldBounds(mapData, units = []) {
    const hexSize = mapData?.hex_size;
    const bounds = mapData?.bounds;
    if (!(Number.isFinite(hexSize) && hexSize > 0) || !bounds) {
      return { minX: 0, minY: 0, maxX: 1, maxY: 1 };
    }

    let pointCount = 0;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    // Fit once while reserving the renderer's worst-case screen-space stack.
    // Later frames may add a larger boundary stack without resetting pan/zoom.
    let screenPadding = SELECTED_UNIT_SCREEN_RADIUS + MAX_STACK_SCREEN_SPREAD;
    const addPoint = (point) => {
      pointCount += 1;
      minX = Math.min(minX, point.x);
      maxX = Math.max(maxX, point.x);
      minY = Math.min(minY, point.y);
      maxY = Math.max(maxY, point.y);
    };
    const addHex = (row, column) => {
      if (!Number.isInteger(row) || !Number.isInteger(column) || row < 0 || column < 0) return;
      hexVertices(row, column, hexSize).forEach(addPoint);
    };
    const minRow = bounds.min_row;
    const maxRow = bounds.max_row;
    const minColumn = bounds.min_column;
    const maxColumn = bounds.max_column;
    if ([minRow, maxRow, minColumn, maxColumn].every((value) => (
      Number.isInteger(value) && value >= 0
    )) && minRow <= maxRow && minColumn <= maxColumn) {
      const representativeRows = new Set([minRow, maxRow]);
      for (const parity of [0, 1]) {
        const row = minRow + ((parity - minRow % 2 + 2) % 2);
        if (row <= maxRow) representativeRows.add(row);
      }
      for (const row of representativeRows) {
        addHex(row, minColumn);
        addHex(row, maxColumn);
      }
    }
    for (const cell of mapData.cells || []) addHex(cell.row, cell.column);

    for (const stack of stackUnits(units)) {
      const unit = stack[0];
      if (!Number.isInteger(unit.row) || !Number.isInteger(unit.column)
          || unit.row < 0 || unit.column < 0) continue;
      const center = hexToPixel(unit.row, unit.column, hexSize);
      addPoint(center);
    }

    if (!pointCount) hexVertices(0, 0, hexSize).forEach(addPoint);
    if (![minX, minY, maxX, maxY].every(Number.isFinite)) {
      return { minX: 0, minY: 0, maxX: 1, maxY: 1 };
    }
    if (maxX <= minX) { minX -= 0.5; maxX += 0.5; }
    if (maxY <= minY) { minY -= 0.5; maxY += 0.5; }
    return { minX, minY, maxX, maxY, screenPadding };
  }

  function unitSideColor(side) {
    if (side === "red") return "#e84545";
    if (side === "blue") return "#3388e8";
    return "#777777";
  }

  function elevationColor(elevation, mapData) {
    if (typeof elevation !== "number" || !Number.isFinite(elevation)) return "#4d555b";
    const minimum = mapData?.elevation?.minimum;
    const maximum = mapData?.elevation?.maximum;
    const levelCount = Number.isInteger(mapData?.elevation?.levels)
      ? Math.max(2, Math.min(TERRAIN_COLORS.length, mapData.elevation.levels))
      : 7;
    if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) return "#4d555b";
    const ratio = maximum <= minimum
      ? 0.5
      : Math.max(0, Math.min(1, (elevation - minimum) / (maximum - minimum)));
    const index = Math.min(levelCount - 1, Math.floor(ratio * levelCount));
    return TERRAIN_COLORS[Math.round(index * (TERRAIN_COLORS.length - 1) / (levelCount - 1))];
  }

  class MapRenderer {
    constructor(canvas, {
      onUnitSelected = null,
      onUnitActivated = null,
      onHexHovered = null,
      onHexSelected = null,
      onHexActivated = null,
    } = {}) {
      this.canvas = canvas;
      this.context = canvas.getContext("2d");
      this.onUnitSelected = onUnitSelected;
      this.onUnitActivated = onUnitActivated;
      this.onHexHovered = onHexHovered;
      this.onHexSelected = onHexSelected;
      this.onHexActivated = onHexActivated;
      this.map = null;
      this.frame = null;
      this.selectedUnitId = null;
      this.selectedUnitIds = new Set();
      this.scale = 1;
      this.offsetX = 0;
      this.offsetY = 0;
      this.hitTargets = [];
      this.drag = null;
      this.hasFitted = false;
      this.selectedHex = null;
      this.hoveredHexKey = null;
      this.cellByKey = new Map();
      this.actionTargets = {
        moves: [], shootTargetIds: new Set(),
        previewMovePosition: null, previewShootTargetId: null,
        plannedPath: [], plannedPaths: [],
      };
      this.tacticalOverlay = {
        side: null,
        visibilityHexIds: new Set(),
        weaponEnvelopeHexIds: new Set(),
        confirmedContactHexIds: new Set(),
      };
      this._bindEvents();
    }

    setMap(map) {
      this.map = map;
      this.cellByKey = new Map(
        (map?.cells || []).map((cell) => [`${cell.row}:${cell.column}`, cell]),
      );
      this.hasFitted = false;
      this.scale = 1;
      this.offsetX = 0;
      this.offsetY = 0;
      this.setTacticalOverlay();
    }

    setActionTargets({
      moves = [],
      shoot_target_ids: shootTargetIds = [],
      preview_move_position: previewMovePosition = null,
      preview_shoot_target_id: previewShootTargetId = null,
      planned_path: plannedPath = [],
      planned_paths: plannedPaths = [],
    } = {}) {
      this.actionTargets = {
        moves: Array.isArray(moves) ? moves.slice() : [],
        shootTargetIds: new Set(Array.isArray(shootTargetIds)
          ? shootTargetIds.map(String) : []),
        previewMovePosition: previewMovePosition === null
          ? null : String(previewMovePosition),
        previewShootTargetId: previewShootTargetId === null
          ? null : String(previewShootTargetId),
        plannedPath: Array.isArray(plannedPath)
          ? plannedPath.filter((value) => Number.isInteger(value) && value >= 0)
            .map((value) => Number(value))
          : [],
        plannedPaths: Array.isArray(plannedPaths)
          ? plannedPaths.map((item) => ({
            unitId: item?.unit_id === undefined ? null : String(item.unit_id),
            path: Array.isArray(item?.path)
              ? item.path.filter((value) => Number.isInteger(value) && value >= 0)
                .map(Number)
              : [],
          })).filter((item) => item.path.length > 1)
          : [],
      };
    }

    setTacticalOverlay({
      side = null,
      visibility_hex_ids: visibilityHexIds = [],
      weapon_envelope_hex_ids: weaponEnvelopeHexIds = [],
      confirmed_contact_hex_ids: confirmedContactHexIds = [],
    } = {}) {
      const normalized = (values) => new Set(
        (Array.isArray(values) ? values : [])
          .filter((value) => Number.isInteger(value) && value >= 0)
          .map(Number),
      );
      this.tacticalOverlay = {
        side: side === "red" || side === "blue" ? side : null,
        visibilityHexIds: normalized(visibilityHexIds),
        weaponEnvelopeHexIds: normalized(weaponEnvelopeHexIds),
        confirmedContactHexIds: normalized(confirmedContactHexIds),
      };
    }

    render(frame, selectedUnitId) {
      this.frame = frame;
      const selectedValues = Array.isArray(selectedUnitId)
        ? selectedUnitId : selectedUnitId === null || selectedUnitId === undefined
          ? [] : [selectedUnitId];
      this.selectedUnitIds = new Set(selectedValues.map(String));
      this.selectedUnitId = selectedValues.length === 1 ? selectedValues[0] : null;
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
      this._drawTacticalOverlays(context);
      this._drawScenarioFeatures(context, this.map?.scenario?.features || []);
      this._drawObjectives(context, frame?.cities || []);
      this._drawActionTargets(context);
      this._drawUnits(context, frame?.viewer_units || []);
      this._drawSelectedHex(context);
    }

    resetView() {
      if (!this.map) return;
      this._resizeCanvas();
      const bounds = this._worldBounds();
      const width = Math.max(1, this.canvas.clientWidth);
      const height = Math.max(1, this.canvas.clientHeight);
      const margin = 28 + Math.max(0, bounds.screenPadding || 0);
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
      if (this.frame) this.render(this.frame, Array.from(this.selectedUnitIds));
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
        this.render(this.frame, Array.from(this.selectedUnitIds));
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
        const point = this._eventPoint(event);
        if (!this.drag || this.drag.pointerId !== event.pointerId) {
          this._notifyHexHover(point);
          return;
        }
        const dx = point.x - this.drag.startX;
        const dy = point.y - this.drag.startY;
        if (Math.hypot(dx, dy) > 3) this.drag.moved = true;
        if (this.drag.moved) {
          this.offsetX = this.drag.offsetX + dx;
          this.offsetY = this.drag.offsetY + dy;
          this.render(this.frame, Array.from(this.selectedUnitIds));
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
          if (hit && this.onUnitSelected) {
            this.onUnitSelected(hit.operatorId, {
              ctrlKey: event.ctrlKey,
              metaKey: event.metaKey,
              shiftKey: event.shiftKey,
            });
          } else {
            const value = this._hexAtScreenPoint(point);
            if (value) {
              this.selectedHex = { row: value.row, column: value.column };
              if (this.onHexSelected) this.onHexSelected(value);
              this.render(this.frame, Array.from(this.selectedUnitIds));
            }
          }
        }
      });
      this.canvas.addEventListener("pointercancel", () => { this.drag = null; });
      this.canvas.addEventListener("pointerleave", () => {
        this.hoveredHexKey = null;
        if (this.onHexHovered) this.onHexHovered(null);
      });
      this.canvas.addEventListener("dblclick", (event) => {
        event.preventDefault();
        const point = this._eventPoint(event);
        const hit = this._unitAtScreenPoint(point);
        if (hit && this.onUnitActivated) {
          this.onUnitActivated(hit.operatorId);
          return;
        }
        const value = this._hexAtScreenPoint(point);
        if (value && this.onHexActivated) this.onHexActivated(value);
      });
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
      return computeWorldBounds(this.map, this.frame?.viewer_units || []);
    }

    _unitAtScreenPoint(point) {
      return [...this.hitTargets].reverse().find((target) => (
        Math.hypot(target.x - point.x, target.y - point.y) <= target.radius
      )) || null;
    }

    _hexAtScreenPoint(point) {
      if (!this.map || !(this.scale > 0)) return null;
      const worldX = (point.x - this.offsetX) / this.scale;
      const worldY = (point.y - this.offsetY) / this.scale;
      const coordinate = pixelToHex(
        worldX,
        worldY,
        this.map.hex_size,
        this.map.bounds,
      );
      if (!coordinate) return null;
      const key = `${coordinate.row}:${coordinate.column}`;
      const cell = this.cellByKey.get(key) || null;
      if ((this.map.cells || []).length && !cell) return null;
      const units = (this.frame?.viewer_units || []).filter((unit) => (
        unit.row === coordinate.row && unit.column === coordinate.column
      ));
      return {
        ...coordinate,
        position_id: cell?.position_id ?? String(coordinate.row * 100 + coordinate.column),
        terrain_semantic: cell?.terrain_semantic ?? "unknown",
        terrain_code: cell?.terrain_code ?? null,
        terrain_level: cell?.terrain_level ?? null,
        elevation: cell?.elevation ?? null,
        roads: cell?.roads ?? [0, 0, 0, 0, 0, 0],
        rivers: cell?.rivers ?? [0, 0, 0, 0, 0, 0],
        units,
      };
    }

    _notifyHexHover(point) {
      const value = this._hexAtScreenPoint(point);
      const key = value ? `${value.row}:${value.column}` : null;
      if (key === this.hoveredHexKey) return;
      this.hoveredHexKey = key;
      if (this.onHexHovered) this.onHexHovered(value);
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
      context.fillStyle = configured ? elevationColor(cell.elevation, this.map) : "#25313a";
      context.fill();
      context.strokeStyle = configured ? "#493b18aa" : "#53616a88";
      context.lineWidth = Math.max(0.45, Math.min(1.2, this.scale * 0.6));
      context.stroke();
      if (cell.terrain_semantic === "water") {
        context.fillStyle = "#397fc0dd"; context.fill();
      }
      if (configured && (typeof cell.elevation !== "number"
          || !Number.isFinite(cell.elevation))) {
        this._drawUnknownElevation(context, vertices);
      }
      this._drawTerrainSymbol(context, cell);
      (cell.roads || []).forEach((roadType, direction) => {
        if (roadType) this._drawRoad(context, cell, direction, roadType);
      });
      (cell.rivers || []).forEach((riverType, direction) => {
        if (riverType) this._drawRiver(context, cell, direction, riverType);
      });
      this._drawElevationLabel(context, cell);
    }

    _drawUnknownElevation(context, vertices) {
      context.save();
      context.beginPath();
      vertices.forEach((point, index) => index
        ? context.lineTo(point.x, point.y)
        : context.moveTo(point.x, point.y));
      context.closePath();
      context.clip();
      context.strokeStyle = "#b7c1c744";
      context.lineWidth = 1;
      const minX = Math.min(...vertices.map((point) => point.x));
      const maxX = Math.max(...vertices.map((point) => point.x));
      const minY = Math.min(...vertices.map((point) => point.y));
      const maxY = Math.max(...vertices.map((point) => point.y));
      for (let x = minX - (maxY - minY); x <= maxX; x += 8) {
        context.beginPath();
        context.moveTo(x, maxY);
        context.lineTo(x + (maxY - minY), minY);
        context.stroke();
      }
      context.restore();
    }

    _drawElevationLabel(context, cell) {
      if (typeof cell.elevation !== "number" || !Number.isFinite(cell.elevation)
          || this.map.hex_size * this.scale < 24) return;
      const center = this._screen(hexToPixel(cell.row, cell.column, this.map.hex_size));
      context.fillStyle = "#101010cc";
      context.font = "9px ui-monospace";
      context.textAlign = "center";
      context.fillText(String(cell.elevation), center.x, center.y + 3);
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

    _drawRiver(context, cell, direction, riverType) {
      const [start, end] = riverEdgeSegment(cell.row, cell.column, direction, this.map.hex_size);
      const a = this._screen(start), b = this._screen(end);
      context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y);
      context.strokeStyle = "#3e9be6";
      context.lineWidth = Math.max(
        1.8,
        this.map.hex_size * this.scale * (riverType === 2 ? 0.25 : 0.2),
      );
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

    _drawScenarioFeatures(context, features) {
      for (const feature of features) {
        if (!Number.isInteger(feature.row) || !Number.isInteger(feature.column)) continue;
        const center = this._screen(hexToPixel(
          feature.row, feature.column, this.map.hex_size,
        ));
        context.save();
        context.globalAlpha = 0.78;
        context.strokeStyle = feature.side === "red" ? "#ff5b62"
          : feature.side === "blue" ? "#45a8ff" : "#f1d271";
        context.fillStyle = "#161616bb";
        context.lineWidth = 1.5;
        if (feature.kind === "city") {
          context.beginPath(); context.arc(center.x, center.y, 8, 0, Math.PI * 2);
          context.fill(); context.stroke();
        } else if (feature.kind.toLowerCase().includes("mine")) {
          context.beginPath(); context.moveTo(center.x, center.y - 7);
          context.lineTo(center.x + 7, center.y + 6);
          context.lineTo(center.x - 7, center.y + 6); context.closePath();
          context.fill(); context.stroke();
        } else {
          context.fillRect(center.x - 6, center.y - 6, 12, 12);
          context.strokeRect(center.x - 6, center.y - 6, 12, 12);
          context.beginPath(); context.moveTo(center.x - 5, center.y - 5);
          context.lineTo(center.x + 5, center.y + 5);
          context.moveTo(center.x + 5, center.y - 5);
          context.lineTo(center.x - 5, center.y + 5); context.stroke();
        }
        context.restore();
      }
    }

    _drawTacticalOverlays(context) {
      const side = this.tacticalOverlay.side;
      if (!side) return;
      const colors = side === "red" ? {
        visibility: "rgba(255,120,120,0.18)",
        weapon: "rgba(255,80,80,0.12)",
      } : {
        visibility: "rgba(110,175,255,0.18)",
        weapon: "rgba(70,140,255,0.12)",
      };
      const draw = (hexIds, color) => {
        context.fillStyle = color;
        for (const hexId of hexIds) {
          const row = Math.floor(hexId / 100);
          const column = hexId % 100;
          if (this.cellByKey.size && !this.cellByKey.has(`${row}:${column}`)) continue;
          const vertices = hexVertices(row, column, this.map.hex_size)
            .map((point) => this._screen(point));
          context.beginPath();
          vertices.forEach((point, index) => index
            ? context.lineTo(point.x, point.y)
            : context.moveTo(point.x, point.y));
          context.closePath();
          context.fill();
        }
      };
      // Draw the broader, lighter weapon layer first so visibility remains
      // legible where the two read-only fields overlap.
      draw(this.tacticalOverlay.visibilityHexIds, colors.visibility);
      draw(this.tacticalOverlay.weaponEnvelopeHexIds, colors.weapon);
      context.save();
      context.strokeStyle = "#ffd84d";
      context.lineWidth = 3;
      for (const hexId of this.tacticalOverlay.confirmedContactHexIds) {
        const row = Math.floor(hexId / 100);
        const column = hexId % 100;
        if (this.cellByKey.size && !this.cellByKey.has(`${row}:${column}`)) continue;
        const vertices = hexVertices(row, column, this.map.hex_size)
          .map((point) => this._screen(point));
        context.beginPath();
        vertices.forEach((point, index) => index
          ? context.lineTo(point.x, point.y)
          : context.moveTo(point.x, point.y));
        context.closePath();
        context.stroke();
      }
      context.restore();
    }

    _drawSelectedHex(context) {
      if (!this.selectedHex) return;
      const vertices = hexVertices(
        this.selectedHex.row,
        this.selectedHex.column,
        this.map.hex_size,
      ).map((point) => this._screen(point));
      context.beginPath();
      vertices.forEach((point, index) => index
        ? context.lineTo(point.x, point.y)
        : context.moveTo(point.x, point.y));
      context.closePath();
      context.strokeStyle = "#ffe15a";
      context.lineWidth = 2.5;
      context.stroke();
    }

    _drawActionTargets(context) {
      const colors = ["#ffc857", "#68d391", "#63b3ed", "#f687b3", "#b794f4"];
      this.actionTargets.plannedPaths.forEach((planned, plannedIndex) => {
        const points = planned.path.map((position) => this._screen(
          hexToPixel(Math.floor(position / 100), position % 100, this.map.hex_size),
        ));
        context.beginPath();
        points.forEach((point, index) => index
          ? context.lineTo(point.x, point.y)
          : context.moveTo(point.x, point.y));
        context.strokeStyle = colors[plannedIndex % colors.length];
        context.lineWidth = 4;
        context.setLineDash([8, 4]);
        context.stroke();
        context.setLineDash([]);
      });
      if (this.actionTargets.plannedPath.length > 1) {
        const points = this.actionTargets.plannedPath.map((position) => this._screen(
          hexToPixel(Math.floor(position / 100), position % 100, this.map.hex_size),
        ));
        context.beginPath();
        points.forEach((point, index) => index
          ? context.lineTo(point.x, point.y)
          : context.moveTo(point.x, point.y));
        context.strokeStyle = "#ffc857";
        context.lineWidth = 4;
        context.setLineDash([8, 4]);
        context.stroke();
        context.setLineDash([]);
      }
      for (const target of this.actionTargets.moves) {
        if (!Number.isInteger(target.row) || !Number.isInteger(target.column)) continue;
        const vertices = hexVertices(
          target.row, target.column, this.map.hex_size,
        ).map((point) => this._screen(point));
        context.beginPath();
        vertices.forEach((point, index) => index
          ? context.lineTo(point.x, point.y)
          : context.moveTo(point.x, point.y));
        context.closePath();
        const preview = String(target.position_id)
          === this.actionTargets.previewMovePosition;
        context.fillStyle = preview ? "#ffc85755" : "#61d09533";
        context.fill();
        context.strokeStyle = preview ? "#ffc857" : "#61d095";
        context.lineWidth = preview ? 3 : 2;
        context.setLineDash(preview ? [] : [6, 4]);
        context.stroke();
        context.setLineDash([]);
      }
    }

    _drawUnits(context, units) {
      for (const stack of stackUnits(units)) {
        const center = this._screen(hexToPixel(stack[0].row, stack[0].column, this.map.hex_size));
        const spread = stackSpread(stack.length);
        stack.forEach((unit, index) => {
          const angle = stack.length > 1 ? Math.PI * 2 * index / stack.length - Math.PI / 2 : 0;
          const moving = unitDisplayPoint(unit, this.map.hex_size);
          const display = this._screen(moving);
          const x = display.x + Math.cos(angle) * spread;
          const y = display.y + Math.sin(angle) * spread;
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
      const selected = unit.operator_id === this.selectedUnitId
        || this.selectedUnitIds.has(String(unit.operator_id));
      const shootTarget = this.actionTargets.shootTargetIds.has(String(unit.operator_id));
      const previewShoot = String(unit.operator_id)
        === this.actionTargets.previewShootTargetId;
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
      if (shootTarget) {
        context.beginPath(); context.arc(0, 0, radius + 8, 0, Math.PI * 2);
        context.strokeStyle = previewShoot ? "#ffc857" : "#ff5b62";
        context.lineWidth = previewShoot ? 4 : 2;
        context.setLineDash(previewShoot ? [] : [4, 3]);
        context.stroke(); context.setLineDash([]);
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
      } else if (kind === "armored_vehicle") {
        context.moveTo(-radius, -radius * 0.55);
        context.lineTo(radius * 0.65, -radius * 0.55);
        context.lineTo(radius, 0);
        context.lineTo(radius * 0.65, radius * 0.55);
        context.lineTo(-radius, radius * 0.55);
        context.closePath();
      } else if (kind === "infantry") {
        context.moveTo(0, -radius); context.lineTo(radius, radius); context.lineTo(-radius, radius); context.closePath();
      } else if (kind === "drone" || kind === "helicopter") {
        context.arc(0, 0, radius * 0.45, 0, Math.PI * 2);
        context.moveTo(-radius, -radius); context.lineTo(radius, radius);
        context.moveTo(-radius, radius); context.lineTo(radius, -radius);
      } else if (kind === "artillery") {
        context.arc(0, 0, radius, 0, Math.PI * 2);
        context.moveTo(0, 0); context.lineTo(radius * 1.3, -radius * 0.5);
      } else if (kind === "unmanned_ground_vehicle") {
        context.moveTo(0, -radius); context.lineTo(radius, 0);
        context.lineTo(0, radius); context.lineTo(-radius, 0); context.closePath();
      } else if (kind === "loitering_munition") {
        context.moveTo(radius, 0); context.lineTo(-radius, -radius * 0.55);
        context.lineTo(-radius * 0.35, 0); context.lineTo(-radius, radius * 0.55);
        context.closePath();
      } else if (kind === "facility") {
        context.rect(-radius, -radius, radius * 2, radius * 2);
      } else {
        context.moveTo(0, -radius); context.lineTo(radius, 0); context.lineTo(0, radius); context.lineTo(-radius, 0); context.closePath();
      }
    }
  }

  function unitDisplayPoint(unit, hexSize) {
    const start = hexToPixel(unit.row, unit.column, hexSize);
    if (!Number.isInteger(unit.movement_target_hex)) return start;
    const progress = Number(unit.movement_progress);
    if (!Number.isFinite(progress) || progress <= 0) return start;
    const targetHex = Number(unit.movement_target_hex);
    const target = hexToPixel(
      Math.floor(targetHex / 100), targetHex % 100, hexSize,
    );
    const ratio = Math.max(0, Math.min(1, progress));
    return {
      x: start.x + (target.x - start.x) * ratio,
      y: start.y + (target.y - start.y) * ratio,
    };
  }

  const api = {
    MapRenderer,
    computeWorldBounds,
    directionalSegment,
    edgeFacts,
    formatEdgeFacts,
    hexToPixel,
    hexVertices,
    unitDisplayPoint,
    pixelToHex,
    riverEdgeSegment,
    summarizeMapLayers,
    stackSpread,
    stackUnits,
    unitSideColor,
  };
  globalObject.ReplayMapRenderer = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof window !== "undefined" ? window : globalThis));
