const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const LAYERS = ['body', 'head'];

function el(tag, className, parent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (parent) parent.appendChild(node);
  return node;
}

export class Rig {
  constructor(container, { editable = false, onSelect, onLayerChange, onImageSize } = {}) {
    this.container = container;
    this.editable = editable;
    this.onSelect = onSelect;
    this.onLayerChange = onLayerChange;
    this.onImageSize = onImageSize;
    this.config = null;
    this.selected = 'body';
    this.talk = 0;
    this.lastStateAt = -Infinity;
    this.swingAngle = 0;
    this.lastTime = 0;
    this.transformCache = new Map();
    this.drag = null;
    this.build();
    this.frame = requestAnimationFrame((time) => this.animate(time));
  }

  build() {
    this.viewport = el('div', 'rig-viewport', this.container);
    this.stage = el('div', 'rig-stage', this.viewport);
    this.stage.style.width = '1920px';
    this.stage.style.height = '1080px';
    this.root = el('div', 'rig-root', this.stage);
    this.anchors = {};
    this.images = {};
    this.motions = {};

    for (const kind of LAYERS) {
      const anchor = el('div', `rig-anchor rig-${kind}-anchor`, this.root);
      const motion = kind === 'head' ? el('div', 'rig-head-motion', anchor) : anchor;
      const img = el('img', `rig-img rig-${kind}-img`, motion);
      img.alt = `${kind} layer`;
      img.draggable = false;
      img.dataset.kind = kind;
      img.addEventListener('load', () => this.handleImageLoad(kind));
      img.addEventListener('dragstart', (event) => event.preventDefault());
      this.anchors[kind] = anchor;
      this.images[kind] = img;
      this.motions[kind] = motion;
    }

    this.pivot = el('span', 'rig-pivot', this.motions.head);
    this.stage.classList.toggle('is-editing', this.editable);
    this.stage.addEventListener('pointerdown', (event) => this.onPointerDown(event));
    this.stage.addEventListener('pointermove', (event) => this.onPointerMove(event));
    this.stage.addEventListener('pointerup', (event) => this.onPointerUp(event));
    this.stage.addEventListener('pointercancel', (event) => this.onPointerUp(event));
    this.stage.addEventListener('wheel', (event) => this.onWheel(event), { passive: false });
    this.stage.addEventListener('contextmenu', (event) => {
      if (this.editable && event.target.closest('.rig-img')) event.preventDefault();
    });
    this.resizeObserver = new ResizeObserver(() => this.fit());
    this.resizeObserver.observe(this.container);
    this.fit();
  }

  destroy() {
    cancelAnimationFrame(this.frame);
    this.resizeObserver?.disconnect();
    this.container.replaceChildren();
  }

  setConfig(config) {
    if (!config) return;
    this.config = config;
    for (const kind of LAYERS) {
      const layer = config.assets[kind];
      const img = this.images[kind];
      if (!layer?.src) {
        img.removeAttribute('src');
        img.dataset.src = '';
        img.style.display = 'none';
        continue;
      }
      if (img.dataset.src !== layer.src) {
        img.dataset.src = layer.src;
        img.src = layer.src;
      }
      const width = layer.naturalWidth || img.naturalWidth || 500;
      const height = layer.naturalHeight || img.naturalHeight || 500;
      img.style.width = `${width}px`;
      img.style.height = `${height}px`;
      img.style.opacity = String(layer.opacity);
      img.style.display = layer.visible ? 'block' : 'none';
      img.style.left = kind === 'body' ? `${-width / 2}px` : '0px';
      img.style.top = `${-height}px`;
      img.style.transformOrigin = kind === 'body' ? '50% 100%' : '0px 100%';
      this.setTransform(img, `rotate(${layer.rotation}deg) scale(${layer.scale})`);
      this.setTransform(this.anchors[kind], `translate3d(${layer.x}px, ${layer.y}px, 0)`);
    }
    this.pivot.hidden = !config.assets.head?.src || !this.editable || this.selected !== 'head';
    const color = { transparent: 'transparent', black: '#000', white: '#fff', green: '#00b140' }[
      config.overlay?.background || 'transparent'
    ];
    this.stage.style.background = color;
    this.viewport.style.background = color;
    this.stage.classList.toggle('has-bg', config.overlay?.background !== 'transparent');
    this.updateSelection();
  }

  setSelected(kind) {
    this.selected = kind;
    this.updateSelection();
  }

  updateSelection() {
    for (const kind of LAYERS) {
      this.images[kind].classList.toggle('is-selected', this.editable && this.selected === kind);
    }
    this.pivot.hidden = !this.config?.assets.head?.src || !this.editable || this.selected !== 'head';
  }

  handleImageLoad(kind) {
    const layer = this.config?.assets?.[kind];
    const img = this.images[kind];
    if (!layer || !img.naturalWidth) return;
    if (!layer.naturalWidth || !layer.naturalHeight) {
      layer.naturalWidth = img.naturalWidth;
      layer.naturalHeight = img.naturalHeight;
      this.onImageSize?.(kind, img.naturalWidth, img.naturalHeight);
    }
    this.setConfig(this.config);
  }

  updateTalk(state) {
    if (!state) return;
    this.talk = clamp(Number(state.talk) || 0, 0, 1);
    this.lastStateAt = performance.now();
  }

  fit() {
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    if (!width || !height) return;
    const scale = Math.min(width / 1920, height / 1080);
    this.stage.style.transform = `translate(-50%, -50%) scale(${scale})`;
  }

  stagePoint(event) {
    const rect = this.stage.getBoundingClientRect();
    return {
      x: ((event.clientX - rect.left) / rect.width) * 1920,
      y: ((event.clientY - rect.top) / rect.height) * 1080
    };
  }

  onPointerDown(event) {
    if (!this.editable || event.button !== 0) return;
    const img = event.target.closest('.rig-img');
    if (!img || img.style.display === 'none') return;
    const kind = img.dataset.kind;
    if (!this.config?.assets[kind]?.src) return;
    event.preventDefault();
    this.setSelected(kind);
    this.onSelect?.(kind);
    img.setPointerCapture?.(event.pointerId);
    const point = this.stagePoint(event);
    const layer = this.config.assets[kind];
    this.drag = { pointerId: event.pointerId, kind, startX: point.x, startY: point.y, x: layer.x, y: layer.y };
    this.container.classList.add('is-dragging');
  }

  onPointerMove(event) {
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    const point = this.stagePoint(event);
    const layer = this.config.assets[this.drag.kind];
    layer.x = Math.round(this.drag.x + point.x - this.drag.startX);
    layer.y = Math.round(this.drag.y + point.y - this.drag.startY);
    this.setConfig(this.config);
    this.onLayerChange?.(this.drag.kind, 'drag');
  }

  onPointerUp(event) {
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    const kind = this.drag.kind;
    this.drag = null;
    this.container.classList.remove('is-dragging');
    this.onLayerChange?.(kind, 'end');
  }

  onWheel(event) {
    if (!this.editable || !this.selected) return;
    const layer = this.config?.assets[this.selected];
    if (!layer?.src) return;
    event.preventDefault();
    if (event.altKey) {
      layer.rotation = clamp(Number((layer.rotation + event.deltaY * 0.05).toFixed(2)), -360, 360);
    } else {
      layer.scale = clamp(Number((layer.scale * Math.exp(-event.deltaY * 0.0014)).toFixed(4)), 0.01, 30);
    }
    this.setConfig(this.config);
    this.onLayerChange?.(this.selected, 'wheel');
  }

  setTransform(element, value) {
    if (this.transformCache.get(element) === value) return;
    this.transformCache.set(element, value);
    element.style.transform = value;
  }

  animate(time) {
    this.frame = requestAnimationFrame((next) => this.animate(next));
    const dt = this.lastTime ? clamp((time - this.lastTime) / 1000, 1 / 120, 1 / 20) : 1 / 60;
    this.lastTime = time;
    const talk = time - this.lastStateAt > 2200 || !this.config?.talk.enabled ? 0 : this.talk;
    const weight = Math.pow(talk, 0.72);
    const jitter = talk * (this.config?.talk.jitter || 0) * (
      Math.sin(time * 0.031) * 3.2 +
      Math.sin(time * 0.047 + 1.7) * 1.8 +
      Math.sin(time * 0.018 + 0.4) * 1.2
    );
    const target = -weight * (this.config?.talk.swing || 0) + jitter;
    const rate = target < this.swingAngle ? 58 : 28;
    this.swingAngle = target + (this.swingAngle - target) * Math.exp(-rate * dt);
    this.motions.head.style.transform = `rotate(${this.swingAngle.toFixed(2)}deg)`;
  }
}
