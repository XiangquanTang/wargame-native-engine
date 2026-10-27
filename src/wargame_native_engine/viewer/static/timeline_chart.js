"use strict";

// Read-only presentation: no analysis values or gameplay rules are recomputed.
(function (root) {
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value) => finite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 4}) : "UNKNOWN";

  function buildModel(data) {
    const indices = data.frame_indices || [];
    const red = data.red_effective_power || [];
    const blue = data.blue_effective_power || [];
    const advantage = data.normalized_advantages || [];
    let maximum = 0;
    for (const values of [red, blue]) for (const value of values) if (finite(value)) maximum = Math.max(maximum, value);
    return {indices, red, blue, advantage, powerMaximum: maximum > 0 ? maximum * 1.05 : 1,
      hasPower: red.some(finite) || blue.some(finite), hasAdvantage: advantage.some(finite)};
  }

  // Keep extrema and endpoints within each screen column, never bridge UNKNOWN.
  function segments(values, indices, pixelWidth) {
    const result = [];
    let segment = [], bucket = [], lastColumn = null;
    const start = indices[0] || 0, span = Math.max(1, (indices[indices.length - 1] || 0) - start);
    function flushBucket() {
      if (!bucket.length) return;
      let minimum = bucket[0], maximum = bucket[0];
      for (const point of bucket) {
        if (point[1] < minimum[1]) minimum = point;
        if (point[1] > maximum[1]) maximum = point;
      }
      const selected = new Map([bucket[0], minimum, maximum, bucket[bucket.length - 1]].map((point) => [point[0], point]));
      segment.push(...Array.from(selected.values()).sort((a, b) => a[0] - b[0]));
      bucket = [];
    }
    for (let index = 0; index < indices.length; index++) {
      const value = values[index];
      if (!finite(value)) {
        flushBucket(); if (segment.length) result.push(segment);
        segment = []; lastColumn = null; continue;
      }
      const column = Math.floor((indices[index] - start) / span * Math.max(1, pixelWidth));
      if (column !== lastColumn) flushBucket();
      bucket.push([indices[index], value]); lastColumn = column;
    }
    flushBucket(); if (segment.length) result.push(segment);
    return result;
  }

  function nearestIndex(indices, frame) {
    if (!indices.length) return -1;
    let low = 0, high = indices.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (indices[middle] < frame) low = middle + 1; else high = middle;
    }
    return low > 0 && Math.abs(indices[low - 1] - frame) <= Math.abs(indices[low] - frame) ? low - 1 : low;
  }

  class TimelineChart {
    constructor(canvas, tooltip, onSeek) {
      this.canvas = canvas; this.tooltip = tooltip; this.onSeek = onSeek;
      this.background = document.createElement("canvas");
      this.model = null; this.frame = 0; this.requestedFrame = 0; this.hover = null; this.dirty = true;
      this.visible = {red: true, blue: true, advantage: true};
      canvas.addEventListener("mousemove", (event) => {
        if (!this.model) return;
        this.hover = this.indexAt(event.clientX);
        this.describe(this.hover); this.draw();
      });
      canvas.addEventListener("mouseleave", () => { this.hover = null; this.describe(nearestIndex(this.model?.indices || [], this.frame)); this.draw(); });
      canvas.addEventListener("click", (event) => { const index = this.indexAt(event.clientX); if (index >= 0) this.onSeek(this.model.indices[index]); });
      canvas.addEventListener("keydown", (event) => {
        if (!this.model || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let index = nearestIndex(this.model.indices, this.requestedFrame);
        if (event.key === "Home") index = 0;
        else if (event.key === "End") index = this.model.indices.length - 1;
        else index += event.key === "ArrowRight" ? 1 : -1;
        index = Math.max(0, Math.min(this.model.indices.length - 1, index));
        if (index >= 0) this.onSeek(this.model.indices[index]);
      });
    }
    setData(data) { this.model = buildModel(data); this.dirty = true; this.draw(); this.describe(nearestIndex(this.model.indices, this.frame)); }
    setRequestedFrame(frame) { this.requestedFrame = frame; }
    setFrame(frame) { this.frame = frame; this.requestedFrame = frame; this.draw(); if (this.hover === null) this.describe(nearestIndex(this.model?.indices || [], frame)); }
    setVisible(name, visible) { this.visible[name] = visible; this.dirty = true; this.draw(); }
    indexAt(clientX) {
      if (!this.model) return -1;
      const rect = this.canvas.getBoundingClientRect(), plotWidth = Math.max(1, rect.width - 90);
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left - 66) / plotWidth));
      const indices = this.model.indices;
      return nearestIndex(indices, (indices[0] || 0) + ratio * ((indices[indices.length - 1] || 0) - (indices[0] || 0)));
    }
    describe(index) {
      if (!this.model || index < 0) return;
      const m = this.model;
      this.tooltip.textContent = `Frame ${m.indices[index]} · 红方力量 ${number(m.red[index])} · 蓝方力量 ${number(m.blue[index])} · 相对优势 ${number(m.advantage[index])}`;
    }
    draw() {
      if (!this.model) return;
      const rect = this.canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
      const width = Math.max(1, Math.round(rect.width * ratio)), height = Math.max(1, Math.round(rect.height * ratio));
      if (this.canvas.width !== width || this.canvas.height !== height) {
        this.canvas.width = width; this.canvas.height = height;
        this.background.width = width; this.background.height = height; this.dirty = true;
      }
      const w = width / ratio, h = height / ratio;
      const top = {top: 24, bottom: h * 0.51}, bottom = {top: h * 0.64, bottom: h - 28};
      const left = 66, right = w - 24, m = this.model;
      const first = m.indices[0] || 0, last = m.indices[m.indices.length - 1] || first;
      const x = (frame) => left + (frame - first) / Math.max(1, last - first) * (right - left);
      const powerY = (value) => top.bottom - value / m.powerMaximum * (top.bottom - top.top);
      const advantageY = (value) => bottom.bottom - (value + 1) / 2 * (bottom.bottom - bottom.top);
      if (this.dirty) {
        const ctx = this.background.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, w, h);
        ctx.font = "11px system-ui, sans-serif"; ctx.fillStyle = "#8fa6b5"; ctx.textAlign = "left";
        ctx.fillText("有效力量", left, 13); ctx.fillText("相对优势", left, bottom.top - 12);
        const horizontal = (value, y, label, zero) => {
          ctx.strokeStyle = zero ? "#78909c" : "#294050"; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
          ctx.fillStyle = "#8fa6b5"; ctx.textAlign = "right"; ctx.fillText(label, left - 8, y + 4);
        };
        for (const fraction of [0, 0.5, 1]) horizontal(fraction, powerY(fraction * m.powerMaximum), number(fraction * m.powerMaximum), false);
        for (const value of [-1, 0, 1]) horizontal(value, advantageY(value), String(value), value === 0);
        const series = (values, y, color) => {
          ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.7;
          for (const points of segments(values, m.indices, right - left)) {
            if (points.length === 1) { ctx.beginPath(); ctx.arc(x(points[0][0]), y(points[0][1]), 2, 0, Math.PI * 2); ctx.fill(); continue; }
            ctx.beginPath(); ctx.moveTo(x(points[0][0]), y(points[0][1]));
            for (let i = 1; i < points.length; i++) ctx.lineTo(x(points[i][0]), y(points[i][1]));
            ctx.stroke();
          }
        };
        if (this.visible.red) series(m.red, powerY, "#ff5b62");
        if (this.visible.blue) series(m.blue, powerY, "#45a8ff");
        if (this.visible.advantage) series(m.advantage, advantageY, "#ffc857");
        ctx.fillStyle = "#8fa6b5"; ctx.textAlign = "center";
        if (!m.hasPower) ctx.fillText("力量数据：UNKNOWN", (left + right) / 2, (top.top + top.bottom) / 2);
        if (!m.hasAdvantage) ctx.fillText("相对优势：UNKNOWN", (left + right) / 2, (bottom.top + bottom.bottom) / 2);
        for (let step = 0; step <= 4; step++) {
          const frame = Math.round(first + (last - first) * step / 4);
          ctx.fillText(String(frame), x(frame), h - 8);
        }
        this.dirty = false;
      }
      const ctx = this.canvas.getContext("2d"); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, width, height); ctx.drawImage(this.background, 0, 0);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const cursor = (frame, color) => { ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x(frame), top.top); ctx.lineTo(x(frame), bottom.bottom); ctx.stroke(); };
      cursor(this.frame, "#ffffffaa");
      if (this.hover !== null) cursor(m.indices[this.hover], "#ffc85799");
    }
  }
  const api = {buildModel, segments, nearestIndex, TimelineChart};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ReplayTimelineChart = api;
})(typeof window !== "undefined" ? window : globalThis);
