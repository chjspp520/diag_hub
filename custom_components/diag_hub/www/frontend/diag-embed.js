/*!
 * Diag Hub —— Home Assistant 前端悬浮窗模块
 *
 * 通过 homeassistant.components.frontend.add_extra_js_url 注入。
 * 设备页「访问」链接（configuration_url → /diag_hub/page）被点击时不再整页跳转，
 * 而是阻止默认行为，在 HA 界面内弹出一个可拖动 / 可缩放的悬浮窗，窗内 iframe
 * 加载诊断页面。这样既不用往侧边栏塞仪表盘条目，也不离开当前页面。
 *
 * 纯原生 JS + Shadow DOM，不依赖 HA 前端内部实现。
 * 没有拦截成功时（例如中键、Ctrl+点击、或模块没注入）链接就是普通新标签页。
 */
(() => {
  "use strict";

  // 只拦这一个页面（页面内 fetch 的 /api/... 一律放行）
  const PAGE_PATH = "/diag_hub/page";
  const HOST_ID = "diag-hub-float-host";
  const WINDOW_KIND = "diag";
  const TITLE = "集成诊断";
  const SIZE = { width: "min(1180px, 94vw)", height: "min(840px, 88vh)" };

  const CSS = `
    .dh-window {
      position: fixed;
      z-index: 1;
      display: none;
      flex-direction: column;
      min-width: 340px;
      min-height: 260px;
      background: #11151c;
      border: 1px solid #2f3c4e;
      border-radius: 12px;
      box-shadow: 0 14px 48px rgba(0, 0, 0, .55);
      overflow: hidden;
      resize: both;
      pointer-events: auto;
    }
    .dh-window.show { display: flex; }
    .dh-window.dh-flash { animation: dh-pop .32s ease; }
    @keyframes dh-pop {
      0% { transform: scale(.97); }
      60% { transform: scale(1.012); }
      100% { transform: scale(1); }
    }
    .dh-head {
      display: flex;
      align-items: center;
      gap: 8px;
      height: 42px;
      padding: 0 10px;
      background: #1b222d;
      border-bottom: 1px solid #2f3c4e;
      cursor: move;
      touch-action: none;
      color: #e7edf5;
      flex: 0 0 auto;
      font-family: -apple-system, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
    }
    .dh-grip { color: #6b7787; font-size: 14px; letter-spacing: -2px; }
    .dh-title { font-size: 13.5px; font-weight: 600; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; }
    .dh-spacer { flex: 1; }
    .dh-btn {
      appearance: none;
      border: 1px solid #2f3c4e;
      background: #202a38;
      color: #e7edf5;
      border-radius: 7px;
      width: 30px;
      height: 28px;
      font-size: 15px;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      padding: 0;
    }
    .dh-btn:hover { background: #2a3646; border-color: #2f9bff; }
    .dh-x:hover { background: #5a2327; border-color: #f0564a; }
    .dh-body { flex: 1 1 auto; position: relative; background: #11151c; }
    .dh-body iframe { position: absolute; inset: 0; width: 100%; height: 100%;
      border: 0; background: #11151c; }
  `;

  // 设备页「访问」是 <ha-button href> ，真正的 <a> 在其 Shadow DOM 里，
  // 普通 closest("a") 穿不过 shadow 边界，必须用 composedPath()。
  function linkFromEvent(e) {
    let path = null;
    try { path = typeof e.composedPath === "function" ? e.composedPath() : null; }
    catch (_) { path = null; }
    if (!path || !path.length) {
      path = [];
      let node = e.target;
      while (node) {
        path.push(node);
        const root = node.getRootNode ? node.getRootNode() : null;
        node = node.parentNode || (root && root.host) || null;
      }
    }
    for (const node of path) {
      if (!node || node.nodeType !== 1) { continue; }
      let href = null;
      if (node.tagName === "A") { href = node.href; }
      if (!href && typeof node.getAttribute === "function") {
        href = node.getAttribute("href");
      }
      if (!href) { continue; }
      let url;
      try { url = new URL(href, location.href); } catch (_) { continue; }
      const clean = url.pathname.replace(/\/+$/, "");
      if (clean === PAGE_PATH) {
        // 主机一律用浏览器当前地址栏（设备链接里可能是 HA 探测到的别的地址）
        try { url = new URL(url.pathname + url.search, window.location.origin); }
        catch (_) { /* 解析失败就用原链接 */ }
        return { url, node };
      }
    }
    return null;
  }

  // 不拦截时（Ctrl/中键开新标签）也把 link 改写成地址栏主机，避免打不开
  function rewriteLink(node, url) {
    try {
      if (node.getAttribute && node.getAttribute("href")) {
        node.setAttribute("href", url.href);
      }
    } catch (_) { /* 改不动就静默 */ }
  }

  function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (host) { return host; }
    host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;inset:0;z-index:999999;pointer-events:none";
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);
    document.body.appendChild(host);
    return host;
  }

  function centerWindow(win) {
    const w = win.offsetWidth || Math.min(1180, window.innerWidth * 0.94);
    const h = win.offsetHeight || Math.min(840, window.innerHeight * 0.88);
    win.style.left = Math.max(8, (window.innerWidth - w) / 2) + "px";
    win.style.top = Math.max(8, (window.innerHeight - h) / 2) + "px";
  }

  function enableDrag(win, head) {
    head.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".dh-btn")) { return; }
      e.preventDefault();
      const offX = e.clientX - win.offsetLeft;
      const offY = e.clientY - win.offsetTop;
      const move = (ev) => {
        const x = Math.max(-win.offsetWidth + 120,
          Math.min(window.innerWidth - 60, ev.clientX - offX));
        const y = Math.max(0, Math.min(window.innerHeight - 40, ev.clientY - offY));
        win.style.left = x + "px";
        win.style.top = y + "px";
      };
      const up = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    });
  }

  function buildWindow(root) {
    const win = document.createElement("div");
    win.className = "dh-window";
    win.dataset.kind = WINDOW_KIND;
    win.style.width = SIZE.width;
    win.style.height = SIZE.height;
    win.innerHTML = `
      <div class="dh-head">
        <span class="dh-grip">⠿</span>
        <span class="dh-title">${TITLE}</span>
        <span class="dh-spacer"></span>
        <button type="button" class="dh-btn" data-act="reload" title="重新加载">↻</button>
        <a class="dh-btn dh-link" target="_blank" rel="noopener" title="在新标签页打开">↗</a>
        <button type="button" class="dh-btn dh-x" data-act="close" title="关闭">✕</button>
      </div>
      <div class="dh-body"><iframe allowfullscreen></iframe></div>`;
    root.appendChild(win);

    const frame = win.querySelector("iframe");
    centerWindow(win);
    enableDrag(win, win.querySelector(".dh-head"));

    win.querySelector('[data-act="reload"]').addEventListener("click", () => {
      if (frame.dataset.url) { frame.src = frame.dataset.url; }
    });
    win.querySelector('[data-act="close"]').addEventListener("click", () => {
      frame.src = "about:blank";
      frame.dataset.url = "";
      win.classList.remove("show");
    });
    return win;
  }

  function openFloat(url) {
    const host = ensureHost();
    const root = host.shadowRoot;
    let win = root.querySelector(`.dh-window[data-kind="${WINDOW_KIND}"]`);
    if (!win) { win = buildWindow(root); }
    const frame = win.querySelector("iframe");
    if (frame.dataset.url !== url.href) {
      frame.dataset.url = url.href;
      frame.src = url.href;
      win.querySelector(".dh-link").href = url.href;
    }
    win.classList.add("show");
    win.classList.remove("dh-flash");
    void win.offsetWidth;  // 强制 reflow 以重放动画
    win.classList.add("dh-flash");
  }

  function onClick(e) {
    const match = linkFromEvent(e);
    if (!match) { return; }
    rewriteLink(match.node, match.url);
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) {
      return;  // 修饰键 / 中键：交给浏览器原生新标签
    }
    e.preventDefault();
    e.stopPropagation();
    openFloat(match.url);
  }

  document.addEventListener("click", onClick, true);

  // F12 里输入 __diagHubFloat 可确认模块是否生效
  window.__diagHubFloat = "1.0.0";
})();
