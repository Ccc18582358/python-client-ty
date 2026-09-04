const fs = require("fs");
const path = require("path");
const vm = require("vm");
const childProcess = require("child_process");

// ── 浏览器指纹探针：记录 SDK 对 navigator/screen/document/performance/location 的每次属性访问 ──
let fpAccessLog = [];
function resetFpAccessLog() { fpAccessLog = []; }
function makeFpLogger(label) {
  return (target) => new Proxy(target, {
    get(obj, prop, receiver) {
      const key = label + "." + String(prop);
      if (!fpAccessLog.includes(key)) fpAccessLog.push(key);
      const value = Reflect.get(obj, prop, receiver);
      if (typeof value === "function") {
        return function (...args) {
          const callKey = key + "(" + args.length + ")";
          if (!fpAccessLog.includes(callKey)) fpAccessLog.push(callKey);
          return Reflect.apply(value, obj, args);
        };
      }
      return value;
    },
    set(obj, prop, value) {
      const key = label + "." + String(prop) + "=<set>";
      if (!fpAccessLog.includes(key)) fpAccessLog.push(key);
      return Reflect.set(obj, prop, value);
    },
  });
}

function fetchExternalScriptSync(fullUrl) {
  const fetcher = `
    const https = require("https");
    const zlib = require("zlib");
    const url = process.argv[1];
    https.get(url, { headers: { "accept-encoding": "gzip, deflate, br" } }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const buffer = Buffer.concat(chunks);
        const encoding = String(response.headers["content-encoding"] || "").toLowerCase();
        const done = (error, output) => {
          if (error) {
            console.error(error && error.stack ? error.stack : String(error));
            process.exit(1);
          }
          process.stdout.write(output);
        };
        if (encoding.includes("br")) zlib.brotliDecompress(buffer, done);
        else if (encoding.includes("gzip")) zlib.gunzip(buffer, done);
        else if (encoding.includes("deflate")) zlib.inflate(buffer, done);
        else process.stdout.write(buffer);
      });
    }).on("error", (error) => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
  `;
  return childProcess.execFileSync(process.execPath, ["-e", fetcher, fullUrl], {
    encoding: "utf8",
    timeout: 15000,
    maxBuffer: 2 * 1024 * 1024,
  });
}

function createStorage(initial = {}) {
  const data = new Map(Object.entries(initial).map(([k, v]) => [String(k), String(v)]));
  return {
    getItem(key) {
      key = String(key);
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      data.set(String(key), String(value));
    },
    removeItem(key) {
      data.delete(String(key));
    },
    dump() {
      return Object.fromEntries(data.entries());
    },
  };
}

function makeDateCtor(nowMs) {
  if (!nowMs) {
    return Date;
  }
  const fixed = Number(nowMs);
  function FixedDate(...args) {
    if (this instanceof FixedDate) {
      return args.length ? new Date(...args) : new Date(fixed);
    }
    return args.length ? Date(...args) : new Date(fixed).toString();
  }
  FixedDate.now = () => fixed;
  FixedDate.parse = Date.parse;
  FixedDate.UTC = Date.UTC;
  FixedDate.prototype = Date.prototype;
  return FixedDate;
}

function makeScriptNode(source) {
  const textNode = {
    nodeType: 3,
    nodeValue: String(source || ""),
    textContent: String(source || ""),
    data: String(source || ""),
    length: String(source || "").length,
    appendChild() { return null; },
    removeChild() { return null; },
  };
  const base = {
    nodeType: 1,
    tagName: "SCRIPT",
    nodeName: "SCRIPT",
    firstChild: textNode,
    lastChild: textNode,
    childNodes: [textNode],
    parentNode: null,
    innerHTML: String(source || ""),
    textContent: String(source || ""),
    src: "",
    id: "",
    type: "text/javascript",
    className: "",
    style: {},
    children: [],
  };
  textNode.parentNode = base;
  return new Proxy(base, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (typeof prop === "symbol") return undefined;
      // Any unknown method/property → a tolerant no-op so DOM calls never throw.
      return function() { return null; };
    },
  });
}

function createContext(options = {}) {
  const localStorage = createStorage(options.localStorageData || {});
  const sessionStorage = createStorage(options.sessionStorageData || {});
  const browserFingerprint = options.browserFingerprint || {};
  const navigatorData = browserFingerprint.navigator || {};
  const screenData = browserFingerprint.screen || {};
  const webglData = browserFingerprint.webgl || {};
  const performanceMemory = browserFingerprint.performanceMemory || {};
  const href = options.pageUrl || "https://www.pxb7.com/buy/10032/1";
  const html = options.html || "";
  const textareas = {};
  String(html).replace(/<textarea\s+id="([^"]+)"[^>]*>([\s\S]*?)<\/textarea>/g, (_, id, value) => {
    textareas[id] = String(value)
      .replace(/&quot;/g, '"')
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>');
    return "";
  });
  const DateCtor = makeDateCtor(options.nowMs);
  const location = new URL(href);
  location.replace = (next) => {
    const updated = new URL(String(next), location.href);
    Object.assign(location, updated);
  };
  const cookieJar = new Map();
  for (const part of String(options.cookie || "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0) cookieJar.set(part.slice(0, index).trim(), part.slice(index + 1).trim());
  }
  const document = {
    currentScript: null,
    head: { innerHTML: html, appendChild() {}, removeChild() {}, firstChild: null, lastChild: null },
    body: { innerHTML: html, appendChild() {}, removeChild() {}, firstChild: null, lastChild: null, insertBefore() { return null; } },
    documentElement: { innerHTML: html, appendChild() {}, removeChild() {}, firstChild: null, lastChild: null },
    getElementById(id) {
      id = String(id);
      if (Object.prototype.hasOwnProperty.call(textareas, id)) {
        return {
          id,
          value: textareas[id],
          innerHTML: textareas[id],
          textContent: textareas[id],
        };
      }
      return null;
    },
    createElement(tag) {
      const makeAnchor = () => {
        const anchor = {};
        Object.defineProperty(anchor, "href", {
          get() {
            return this._href || "";
          },
          set(value) {
            const parsed = new URL(String(value), href);
            this._href = parsed.href;
            this.protocol = parsed.protocol;
            this.host = parsed.host;
            this.hostname = parsed.hostname;
            this.port = parsed.port;
            this.pathname = parsed.pathname;
            this.search = parsed.search;
            this.hash = parsed.hash;
          },
        });
        return anchor;
      };
      if (String(tag).toLowerCase() === "a") {
        return makeAnchor();
      }
      const element = {
        style: {},
        children: [],
        firstChild: null,
        lastChild: null,
        parentNode: null,
        tagName: String(tag).toUpperCase(),
        className: "",
        id: "",
        setAttribute() {},
        getAttribute() { return null; },
        removeAttribute() {},
        insertBefore(newNode) { this.children.push(newNode); return newNode; },
        appendChild(node) { this.children.push(node); return node; },
        removeChild(node) { return node; },
        replaceChild(node) { return node; },
        addEventListener() {},
        removeEventListener() {},
      };
      Object.defineProperty(element, "innerHTML", {
        get() {
          return this._innerHTML || "";
        },
        set(value) {
          this._innerHTML = String(value);
          if (this._innerHTML.includes("<a") && !this.firstChild) {
            this.firstChild = makeAnchor();
          }
        },
      });
      return element;
    },
    getElementsByTagName(tag) {
      const name = String(tag || "").toLowerCase();
      if (name === "script" && document.currentScript) return [document.currentScript];
      return [];
    },
    getElementsByName() {
      return [];
    },
    getElementsByClassName() {
      return [];
    },
    querySelector(selector) {
      const idMatch = String(selector || "").match(/^#(.+)$/);
      if (idMatch) return this.getElementById(idMatch[1]);
      return null;
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
    removeEventListener() {},
  };
  Object.defineProperty(document, "cookie", {
    configurable: true,
    get() {
      return Array.from(cookieJar.entries()).map(([k, v]) => `${k}=${v}`).join("; ");
    },
    set(value) {
      const first = String(value || "").split(";", 1)[0];
      const index = first.indexOf("=");
      if (index > 0) cookieJar.set(first.slice(0, index).trim(), first.slice(index + 1).trim());
    },
  });
  function Headers(init = {}) {
    this._data = {};
    if (init && typeof init.forEach === "function") {
      init.forEach((value, key) => this.append(key, value));
    } else if (Array.isArray(init)) {
      for (const [key, value] of init) this.append(key, value);
    } else if (init && typeof init === "object") {
      for (const [key, value] of Object.entries(init)) this.append(key, value);
    }
  }
  Headers.prototype.append = function append(key, value) {
    this._data[String(key).toLowerCase()] = String(value);
  };
  Headers.prototype.set = Headers.prototype.append;
  Headers.prototype.get = function get(key) {
    return this._data[String(key).toLowerCase()] || null;
  };
  Headers.prototype.has = function has(key) {
    return Object.prototype.hasOwnProperty.call(this._data, String(key).toLowerCase());
  };
  Headers.prototype.delete = function remove(key) {
    delete this._data[String(key).toLowerCase()];
  };
  Headers.prototype.forEach = function forEach(callback) {
    for (const [key, value] of Object.entries(this._data)) callback(value, key, this);
  };
  function Request(input, init = {}) {
    this.url = input && input.url ? String(input.url) : String(input || "");
    this.method = String(init.method || (input && input.method) || "GET").toUpperCase();
    this.headers = new Headers(init.headers || (input && input.headers) || {});
    this.body = init.body || null;
  }
  function Response(body = "", init = {}) {
    this._body = body;
    this.status = init.status || 200;
    this.statusText = init.statusText || "OK";
    this.headers = new Headers(init.headers || {});
    this.ok = this.status >= 200 && this.status < 300;
  }
  Response.prototype.text = function text() { return Promise.resolve(String(this._body || "")); };
  Response.prototype.json = function json() { return this.text().then((value) => JSON.parse(value)); };
  Response.prototype.clone = function clone() {
    return new Response(this._body, { status: this.status, statusText: this.statusText, headers: this.headers });
  };
  function XMLHttpRequest() {
    this.readyState = XMLHttpRequest.UNSENT;
    this.status = 0;
    this.statusText = "";
    this.responseText = "";
    this.response = "";
    this.responseXML = null;
    this.responseURL = "";
    this.responseType = "";
    this.timeout = 0;
    this.withCredentials = false;
    this.upload = { onprogress: null, onload: null, onerror: null, addEventListener() {}, removeEventListener() {} };
    this.onreadystatechange = null;
    this.onload = null;
    this.onerror = null;
    this.onabort = null;
    this.onloadstart = null;
    this.onloadend = null;
    this.onprogress = null;
    this.ontimeout = null;
    this._headers = {};
    this._url = "";
    this._method = "GET";
    this._async = true;
    this._sent = false;
  }
  XMLHttpRequest.UNSENT = 0;
  XMLHttpRequest.OPENED = 1;
  XMLHttpRequest.HEADERS_RECEIVED = 2;
  XMLHttpRequest.LOADING = 3;
  XMLHttpRequest.DONE = 4;
  Object.defineProperty(XMLHttpRequest.prototype, "open", {
    writable: true,
    configurable: true,
    enumerable: false,
    value: function open(method, url, async = true, user, password) {
      this._method = String(method || "GET").toUpperCase();
      this._url = String(url || "");
      this._async = async !== false;
      this.readyState = XMLHttpRequest.OPENED;
      this._sent = false;
    },
  });
  XMLHttpRequest.prototype.open.toString = function toString() {
    return "function open() { [native code] }";
  };
  XMLHttpRequest.prototype.setRequestHeader = function setRequestHeader(key, value) {
    this._headers[String(key).toLowerCase()] = String(value);
  };
  XMLHttpRequest.prototype.setRequestHeader.toString = function toString() {
    return "function setRequestHeader() { [native code] }";
  };
  XMLHttpRequest.prototype.getResponseHeader = function getResponseHeader(name) {
    if (name && this._responseHeaders) {
      const key = String(name).toLowerCase();
      return this._responseHeaders[key] || null;
    }
    return null;
  };
  XMLHttpRequest.prototype.getAllResponseHeaders = function getAllResponseHeaders() {
    if (!this._responseHeaders) return "";
    return Object.entries(this._responseHeaders)
      .map(([k, v]) => `${k}: ${v}`)
      .join("\r\n");
  };
  XMLHttpRequest.prototype.addEventListener = function addEventListener(type, callback) {
    this["on" + type] = callback;
  };
  XMLHttpRequest.prototype.removeEventListener = function removeEventListener() {};

  const makeRealSend = (cookieJar) => function send(body) {
    const xhr = this;
    const url = xhr._url;
    if (!url || !enableRealNetwork) {
      // Mock mode: just set readyState to DONE
      xhr._sent = true;
      xhr.readyState = XMLHttpRequest.DONE;
      xhr.status = 200;
      xhr.statusText = "OK";
      xhr.responseText = "";
      xhr.response = "";
      xhr._responseHeaders = {};
      if (typeof xhr.onreadystatechange === "function") {
        try { xhr.onreadystatechange(); } catch (_) {}
      }
      if (typeof xhr.onload === "function") {
        try { xhr.onload(); } catch (_) {}
      }
      return;
    }

    // Real network mode: make actual HTTPS request
    const https = require("https");
    const parsedUrl = new URL(url);
    const method = xhr._method || "GET";
    const headers = { ...xhr._headers };

    if (!options.quiet) {
      console.error("[XHR]", method, url.substring(0, 150));
    }

    // Add cookie from cookie jar
    const cookieStr = Array.from(cookieJar.entries())
      .map(([k, v]) => `${k}=${v}`)
      .join("; ");
    if (cookieStr) headers["Cookie"] = cookieStr;
    if (body && !headers["content-type"] && !headers["Content-Type"]) {
      headers["Content-Type"] = "text/plain;charset=UTF-8";
    }

    const requestBody = body ? (
      typeof body === "string" ? body : Buffer.from(body).toString()
    ) : undefined;

    const options = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || 443,
      path: parsedUrl.pathname + parsedUrl.search,
      method: method,
      headers: headers,
      rejectUnauthorized: false,
      timeout: 10000,
    };

    try {
      const req = https.request(options, (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          xhr.responseText = Buffer.concat(chunks).toString("utf8");
          xhr.response = xhr.responseText;
          xhr.status = res.statusCode;
          xhr.statusText = res.statusMessage || "";
          xhr.readyState = XMLHttpRequest.DONE;
          xhr._sent = true;

          // Parse response headers
          xhr._responseHeaders = {};
          for (const [k, v] of Object.entries(res.headers)) {
            xhr._responseHeaders[String(k).toLowerCase()] = String(v);
          }

          // Update cookie jar from Set-Cookie
          const setCookie = res.headers["set-cookie"];
          if (setCookie) {
            const values = Array.isArray(setCookie) ? setCookie : [setCookie];
            for (const sc of values) {
              const first = String(sc).split(";", 1)[0];
              const idx = first.indexOf("=");
              if (idx > 0) {
                cookieJar.set(first.slice(0, idx).trim(), first.slice(idx + 1).trim());
              }
            }
          }

          if (typeof xhr.onreadystatechange === "function") {
            try { xhr.onreadystatechange(); } catch (_) {}
          }
          if (typeof xhr.onload === "function") {
            try { xhr.onload(); } catch (_) {}
          }
        });
      });

      req.on("error", (err) => {
        xhr.readyState = XMLHttpRequest.DONE;
        xhr.status = 0;
        xhr.statusText = String(err.message);
        xhr._sent = true;
        xhr._responseHeaders = {};
        if (typeof xhr.onerror === "function") {
          try { xhr.onerror(err); } catch (_) {}
        }
        if (typeof xhr.onreadystatechange === "function") {
          try { xhr.onreadystatechange(); } catch (_) {}
        }
      });

      req.setTimeout(10000, () => {
        req.destroy();
        xhr.readyState = XMLHttpRequest.DONE;
        xhr.status = 0;
        xhr.statusText = "timeout";
        xhr._sent = true;
        xhr._responseHeaders = {};
        if (typeof xhr.onreadystatechange === "function") {
          try { xhr.onreadystatechange(); } catch (_) {}
        }
      });

      if (requestBody) {
        req.write(requestBody);
      }
      req.end();
    } catch (err) {
      xhr.readyState = XMLHttpRequest.DONE;
      xhr.status = 0;
      xhr.statusText = String(err.message);
      xhr._sent = true;
    }
  };

  Object.defineProperty(XMLHttpRequest.prototype, "send", {
    writable: true,
    configurable: true,
    enumerable: false,
    value: function send(body) { return makeRealSend(cookieJar).call(this, body); },
  });
  XMLHttpRequest.prototype.send.toString = function toString() {
    return "function send() { [native code] }";
  };
  XMLHttpRequest.prototype.abort = function abort() {};
  XMLHttpRequest.prototype.abort.toString = function toString() {
    return "function abort() { [native code] }";
  };

  // Module-level state for challenge network mode
  let enableRealNetwork = false;
  let envFetch = function() { return Promise.resolve(new Response("")); };
  const navigator = {
    userAgent: navigatorData.userAgent || options.userAgent || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    platform: navigatorData.platform || "Win32",
    vendor: navigatorData.vendor || "Google Inc.",
    webdriver: navigatorData.webdriver === undefined ? false : navigatorData.webdriver,
    language: navigatorData.language || "zh-CN",
    languages: Array.isArray(navigatorData.languages) && navigatorData.languages.length ? navigatorData.languages : ["zh-CN", "zh"],
    hardwareConcurrency: navigatorData.hardwareConcurrency || 16,
    deviceMemory: navigatorData.deviceMemory || 8,
    maxTouchPoints: navigatorData.maxTouchPoints === undefined ? 10 : navigatorData.maxTouchPoints,
    cookieEnabled: navigatorData.cookieEnabled === undefined ? true : navigatorData.cookieEnabled,
    doNotTrack: null,
    appVersion: String(navigatorData.userAgent || options.userAgent || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36").replace(/^Mozilla\//, ""),
    productSub: navigatorData.productSub || "20030107",
    vendorSub: "",
    plugins: (() => {
      const names = Array.isArray(navigatorData.plugins) && navigatorData.plugins.length ? navigatorData.plugins : ["Chrome PDF Plugin", "Chrome PDF Viewer", "Native Client"];
      const plugins = { length: names.length, item(index) { return this[index] || null; }, namedItem(name) { return names.includes(name) ? { name } : null; }, refresh() {} };
      names.forEach((name, index) => { plugins[index] = { name }; });
      return plugins;
    })(),
    mimeTypes: (() => {
      const types = Array.isArray(navigatorData.mimeTypes) && navigatorData.mimeTypes.length ? navigatorData.mimeTypes : ["application/pdf", "text/pdf"];
      const mimeTypes = { length: types.length, item(index) { return this[index] || null; }, namedItem(type) { return types.includes(type) ? { type } : null; } };
      types.forEach((type, index) => { mimeTypes[index] = { type }; });
      return mimeTypes;
    })(),
    getBattery() { return Promise.resolve({ charging: true, level: 1 }); },
    getGamepads() { return []; },
    mediaDevices: { enumerateDevices() { return Promise.resolve([]); } },
    serviceWorker: { register() { return Promise.reject(); } },
    storage: { estimate() { return Promise.resolve({ quota: 0, usage: 0 }); } },
    bluetooth: { getAvailability() { return Promise.resolve(false); } },
    permissions: { query() { return Promise.resolve({ state: "prompt" }); } },
    geolocation: {},
  };
  const sandbox = {
    console: options.quiet ? { log() {}, warn() {}, error() {}, debug() {} } : console,
    window: null,
    self: null,
    globalThis: null,
    document,
    navigator,
    location,
    localStorage,
    sessionStorage,
    Math,
    Date: DateCtor,
    JSON,
    String,
    Number,
    Boolean,
    Array,
    Object,
    Function,
    RegExp,
    Error,
    URL,
    Uint8Array,
    encodeURIComponent,
    decodeURIComponent,
    encodeURI,
    decodeURI,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
    escape,
    unescape,
    setTimeout(callback, ...args) {
      if (typeof callback === "function") {
        try {
          callback(...args);
        } catch (_) {}
      }
      return 1;
    },
    setInterval(callback, ...args) {
      return 1;
    },
    clearTimeout() {},
    clearInterval() {},
    XMLHttpRequest,
    fetch: (url, init) => {
      sandbox.__lastFetchUrl = String(url || "");
      return envFetch(url, init);
    },
    Request,
    Response,
    Headers,
    Blob: function Blob(parts = [], opts = {}) {
      this.parts = parts;
      this.type = opts.type || "";
      this.arrayBuffer = () => Promise.resolve(Buffer.from(parts.map((item) => String(item)).join("")));
      this.text = () => Promise.resolve(parts.map((item) => String(item)).join(""));
    },
    // Chrome object (WAF checks this)
    chrome: {
      loadTimes() { return {}; },
      csi() { return {}; },
      app: {
        isInstalled: false,
        InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" },
        RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" },
      },
    },
    // Screen properties
    screen: {
      width: screenData.width || 1440,
      height: screenData.height || 900,
      availWidth: screenData.availWidth || screenData.width || 1440,
      availHeight: screenData.availHeight || 852,
      colorDepth: screenData.colorDepth || 24,
      pixelDepth: screenData.pixelDepth || 24,
    },
    devicePixelRatio: screenData.devicePixelRatio || 2,
    outerWidth: screenData.outerWidth || screenData.width || 1440,
    outerHeight: screenData.outerHeight || screenData.height || 900,
    innerWidth: screenData.innerWidth || screenData.width || 1440,
    innerHeight: screenData.innerHeight || screenData.availHeight || 852,
    screenX: screenData.screenX || 0,
    screenY: screenData.screenY || 0,
    // Performance
    performance: {
      now: () => Date.now(),
      timing: { navigationStart: Date.now() - 20000 },
      memory: {
        usedJSHeapSize: performanceMemory.usedJSHeapSize || 90000000,
        totalJSHeapSize: performanceMemory.totalJSHeapSize || 100000000,
        jsHeapSizeLimit: performanceMemory.jsHeapSizeLimit || 4000000000,
      },
    },
    // IndexedDB
    indexedDB: {},
    // Notification
    Notification: { permission: "default", requestPermission() { return Promise.resolve("default"); } },
    // Worker
    Worker: function Worker() {},
    // Permissions
    permissions: navigator.permissions,
    // Battery
    getBattery: navigator.getBattery,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = function addEventListener() {};
  sandbox.removeEventListener = function removeEventListener() {};
  sandbox.dispatchEvent = function dispatchEvent() { return true; };
  sandbox.HTMLCanvasElement = function HTMLCanvasElement() {};
  const makeWebglContext = () => ({
    VENDOR: 0x1F00,
    RENDERER: 0x1F01,
    VERSION: 0x1F02,
    SHADING_LANGUAGE_VERSION: 0x8B8C,
    MAX_TEXTURE_SIZE: 0x0D33,
    getExtension(name) {
      if (String(name) === "WEBGL_debug_renderer_info") {
        return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
      }
      return null;
    },
    getParameter(param) {
      if (param === 0x9245 || param === this.VENDOR) return webglData.vendor || "Google Inc.";
      if (param === 0x9246 || param === this.RENDERER) return webglData.renderer || "";
      if (param === this.VERSION) return webglData.version || "WebGL 1.0 (OpenGL ES 2.0 Chromium)";
      if (param === this.SHADING_LANGUAGE_VERSION) return webglData.shadingLanguageVersion || "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)";
      if (param === this.MAX_TEXTURE_SIZE) return webglData.maxTextureSize || 16384;
      return 0;
    },
  });
  const originalCreateElement = document.createElement.bind(document);
  document.createElement = function patchedCreateElement(tag) {
    const element = originalCreateElement(tag);
    if (String(tag).toLowerCase() === "canvas") {
      element.getContext = function getContext(type) {
        if (String(type).includes("webgl")) {
          const gl = makeWebglContext();
          return options.debugFingerprint ? makeFpLogger("webgl")(gl) : gl;
        }
        const ctx2d = {
          textBaseline: "",
          font: "",
          fillStyle: "",
          fillRect() {},
          fillText() {},
          getImageData() { return { data: [] }; },
        };
        return options.debugFingerprint ? makeFpLogger("canvas2d")(ctx2d) : ctx2d;
      };
      element.toDataURL = function toDataURL() {
        return `data:image/png;base64,${browserFingerprint.canvasHash || "canvas"}`;
      };
      if (options.debugFingerprint) return makeFpLogger("canvas")(element);
    }
    return element;
  };
  const globalStorageKeys = new Set([
    "_waf_bd8ce2ce37",
    "_waf_a86dfdc5f2",
    "__00b204e9800998__",
    "api-pc.pxb7.com_dySig",
    "www.pxb7.com_dySig",
  ]);
  for (const [key, value] of Object.entries(options.localStorageData || {})) {
    if (!globalStorageKeys.has(String(key))) continue;
    const first = String(value).split("||", 1)[0];
    try {
      sandbox[key] = decodeURIComponent(first);
    } catch (_) {
      sandbox[key] = first;
    }
  }
  if (options.debugFingerprint) {
    resetFpAccessLog();
    const wrapNav = makeFpLogger("navigator");
    const wrapScr = makeFpLogger("screen");
    const wrapDoc = makeFpLogger("document");
    const wrapPerf = makeFpLogger("performance");
    const wrapLoc = makeFpLogger("location");
    sandbox.navigator = wrapNav(sandbox.navigator);
    sandbox.screen = wrapScr(sandbox.screen);
    sandbox.document = wrapDoc(sandbox.document);
    sandbox.performance = wrapPerf(sandbox.performance);
    sandbox.location = wrapLoc(sandbox.location);
    // window/self/globalThis 都指向 sandbox 自身，保持对新包装对象的引用
    sandbox.window = sandbox;
    sandbox.self = sandbox;
    sandbox.globalThis = sandbox;
  }
  return { context: vm.createContext(sandbox), localStorage, sessionStorage, cookieJar };
}

function stripExpirySuffix(source) {
  return String(source || "").replace(/\|\|\d+\s*$/, "");
}

function injectExports(source, options = {}) {
  source = stripExpirySuffix(source);
  source = source.replace("var go;function gI", "var go=790;function gI");
  source = source.replace("typeof gC===[]+[][[]]?'/LN+23PNHvJGRW':gC()", "'/LN+23PNHvJGRW'");
  source = source.replace("var tk;function tp", "var tk=276;function tp");
  source = source.replace("typeof tl===[]+[][[]]?'jjeZpKPaN++g88':tl()", "'jjeZpKPaN++g88'");
  const forceEnv = options.forceEnv || "";
  const oldMarker = "}finally{v.pop();}}var a6=";
  if (source.includes(oldMarker)) {
    const envPatch = forceEnv ? `;w=function(){return ${JSON.stringify(String(forceEnv))};}` : "";
    const exportCode = `}finally{v.pop();}}${envPatch};N['r'].__pz={a0:a0,a1:a1,a2:a2,a3:a3,a4:a4,a5:a5,O:O,w:w,M:M,gA:gA,gE:gE,az:az,Y:Y,decodeNames:function(a8){try{v.push(175);var a9={};for(var aa in a8){try{a9[aa]=az(a8[aa]);}catch(aA){a9[aa]='';}}return a9;}finally{v.pop();}}};var a6=`;
    let patched = source.replace(oldMarker, exportCode);
    if (process.env.PZ_SKIP_DYNAMIC_INIT === "1") {
      patched = patched.replace(
        ";a7[az(gN.gb)]();}finally{v.pop();}}()));}());",
        ";}finally{v.pop();}}()));}());"
      );
    }
    return { source: patched, injected: true, mode: "legacy" };
  }
  const newMarker = "}finally{t.pop();}}var Lt=";
  if (source.includes(newMarker)) {
    const envPatch = forceEnv ? `;W=function(){return ${JSON.stringify(String(forceEnv))};}` : "";
    const logPatch = options.debugLog
      ? `;Z['n'].__pzDbg={calls:[],alpha:''};try{var __d_u=u,__d_LS=LS,__d_Li=Li,__d_L3=L3,__d_I=I,__d_Ln=Ln,__d_q=q,__d_N=N,__d_O=O;try{Z['n'].__pzDbg.alpha=String(z);Z['n'].__pzDbg.L6=String(L6);}catch(__za){}u=function(a){Z['n'].__pzDbg.calls.push(['u',String(a)]);return __d_u(a);};LS=function(a){var r=__d_LS(a);Z['n'].__pzDbg.calls.push(['LS',String(a),'->',String(r)]);return r;};Li=function(a){var r=__d_Li(a);Z['n'].__pzDbg.calls.push(['Li',String(a),'->',String(r)]);return r;};L3=function(){var r=__d_L3();Z['n'].__pzDbg.calls.push(['L3',String(r)]);return r;};I=function(){var r=__d_I();Z['n'].__pzDbg.calls.push(['I',String(r)]);return r;};Ln=function(a){var r=__d_Ln(a);Z['n'].__pzDbg.calls.push(['Ln',String(a),'->',String(r)]);return r;};q=function(a){var r=__d_q(a);Z['n'].__pzDbg.calls.push(['q',String(a),'->',String(r)]);return r;};N=function(a,b,c){var r=__d_N(a,b,c);Z['n'].__pzDbg.calls.push(['N',String(a),String(b),String(c),'->',String(r)]);return r;};O=function(a,b){var r=__d_O(a,b);Z['n'].__pzDbg.calls.push(['O',String(a),String(b),'->',String(r)]);return r;};}catch(__e){}`
      : "";
    const exportsOnly = `;Z['n'].__pz={LK:LK,Lf:Lf,LA:LA,x:x,H:H,A:A,W:W,Li:Li,LS:LS,u:u,L3:L3,I:I,q:q,N:N,O:O,L6:L6,LU:LU,Ln:Ln,Q:Q,L4:L4,L5:L5,decodeNames:function(a8){try{t.push(175);var a9={};for(var aa in a8){try{a9[aa]=LA(a8[aa]);}catch(aA){a9[aa]='';}}return a9;}finally{t.pop();}}};`;
    // Prefer running the SDK's own dynamic init (it seeds Math.random and shuffles
    // the alphabet z), then expose internals — instead of truncating the source at
    // "var Lt=" which discards that init and leaves Math.random native + alphabet unshuffled.
    const initCall = "Lx[LA(tg.tT)]();";
    const initIdx = source.indexOf(initCall);
    if (initIdx >= 0) {
      const before = source.slice(0, initIdx + initCall.length);
      const after = source.slice(initIdx + initCall.length);
      return { source: before + envPatch + logPatch + exportsOnly + after, injected: true, mode: "modern-full" };
    }
    const exportCode = `}finally{t.pop();}}${envPatch}${logPatch}${exportsOnly}}finally{t.pop();}}()));}());`;
    const prefix = source.slice(0, source.indexOf(newMarker));
    return { source: prefix + exportCode, injected: true, mode: "modern" };
  }
  return { source, injected: false, mode: "raw" };
}

function run(options = {}) {
  const scriptPath = options.scriptPath || path.join(__dirname, "debug", "paused_km_script_latest.js");
  const rawSource = options.scriptSource ? String(options.scriptSource) : fs.readFileSync(scriptPath, "utf8");
  const injected = injectExports(rawSource, options);
  const source = injected.source;
  const { context, localStorage } = createContext(options);
  vm.runInContext(source, context, { timeout: 60000, filename: path.basename(scriptPath) });
  const payload = {
    method: options.method || "POST",
    body: options.body || "",
    url: options.url || "https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList",
  };
  context.__pz_input = payload;
  const result = vm.runInContext(`
    (function(){
      const pz = window.__pz || null;
      if (!pz) {
        let xhrUrl = "";
        let xhrSendError = "";
        try {
          const xhr = new XMLHttpRequest();
          xhr.open(__pz_input.method, __pz_input.url, true);
          try { xhr.setRequestHeader("Content-Type", "application/json"); } catch (_) {}
          try { xhr.send(__pz_input.body); } catch (err) { xhrSendError = String(err && err.message || err); }
          xhrUrl = xhr._url || xhr.responseURL || "";
        } catch (err) {
          xhrSendError = String(err && err.message || err);
        }
        try {
          if (typeof fetch === "function") {
            fetch(__pz_input.url, {
              method: __pz_input.method,
              headers: { "Content-Type": "application/json" },
              body: __pz_input.body
            });
          }
        } catch (_) {}
        return {
          decodeUrlFromA4: "",
          manualDecode: "",
          manualPayload: "",
          manualParts: [],
          globals: {},
          fakeOpenArgs: [],
          xhrHookUrlFromA5: "",
          xhrUrl,
          xhrHookDiffers: xhrUrl && xhrUrl !== __pz_input.url,
          xhrA5Url: "",
          fetchUrl: window.__lastFetchUrl || "",
          debugKeys: {},
          localStorage: localStorage.dump ? localStorage.dump() : {},
          runnerInjected: false,
          runnerError: xhrSendError
        };
      }
      const decodeMap = (obj) => {
        if (pz.decodeNames) return pz.decodeNames(obj);
        const out = {};
        for (const [key, value] of Object.entries(obj || {})) {
          try { out[key] = pz.az(value); } catch (_) { out[key] = ""; }
        }
        return out;
      };
      const method = __pz_input.method;
      const body = __pz_input.body;
      const url = __pz_input.url;
      const parsed = new URL(url);
      const target = parsed.host + parsed.pathname;
      const methodPart = String(method || "GET").toLowerCase();
      const wafBd = window._waf_bd8ce2ce37 || "";
      const wafATs = window._waf_a86dfdc5f2 || "";
      const fpId = window.__00b204e9800998__ || "";

      if (pz.LK) {
        // The real call site is: LK(input, init.body, init.method, 2).
        //   input  = request URL (fetch/XHR input)
        //   body   = request body
        //   method = HTTP method
        //   2      = mode constant (0x8e + -0x258d + 0x2501)
        const candidates = [
          [url, body, method, 2],
          [url, body, method, 0],
          [url, body, method, 1],
          [url, method, body, 2],
          [method, body, url, 2],
        ];
        let lkUrl = "";
        const attempts = [];
        for (const args of candidates) {
          try {
            const value = pz.LK(args[0], args[1], args[2], args[3]);
            attempts.push(String(value || "").slice(0, 180));
            if (String(value || "").includes("timestamp__1366=")) {
              lkUrl = String(value);
              break;
            }
          } catch (_) {}
        }
        // Extract the raw timestamp__1366 value from the signed URL.
        let timestampValue = "";
        try {
          const u = new URL(lkUrl);
          timestampValue = u.searchParams.get("timestamp__1366") || "";
        } catch (_) {}
        return {
          decodeUrlFromA4: lkUrl,
          timestampValue,
          manualDecode: "",
          manualPayload: "",
          manualParts: [],
          globals: { wafBd, wafATs, fpId, env: (typeof pz.W === "function" ? pz.W() : "") },
          fakeOpenArgs: [],
          xhrHookUrlFromA5: "",
          xhrUrl: "",
          xhrHookDiffers: false,
          xhrA5Url: "",
          fetchUrl: "",
          debugKeys: { lkAttempts: attempts },
          localStorage: localStorage.dump ? localStorage.dump() : {},
          runnerInjected: true
        };
      }

      // ---- 1. manualDecode: 手动还原 hook 内部的 decode 公式 ----
      const d2 = encodeURIComponent(wafBd + methodPart + "214d4f07715" + target + body);
      const d3 = encodeURIComponent(wafBd + methodPart + "214d4f07715" + target);
      const h1 = pz.a2(pz.a0(d2));
      const h2 = pz.a2(pz.a0(d3));
      const env = pz.w();
      const now = Date.now();
      const manualPayload = [h1, h2, env, now, wafBd, wafATs, fpId].join("|");
      const manualDecode = "214d4f07715-" + encodeURIComponent(pz.O(manualPayload));

      // ---- 2. a4 直接输出 ----
      const a4Url = pz.a4(url, body, method, 1);

      // ---- 3. a5 hook 模拟 (方式A: 伪造参数调用 a5) ----
      const gEKeys = decodeMap(pz.gE);
      const gAKeys = decodeMap(pz.gA);
      const openArgs = [method, url, true];
      const fakeA = {};
      fakeA[gEKeys.x] = openArgs;
      fakeA[gEKeys.O] = body;
      // 补齐常见 XHR 属性，以防 a5 直接读取 this.xxx
      fakeA.method = method;
      fakeA.url = url;
      fakeA._method = method;
      fakeA._url = url;
      fakeA.readyState = XMLHttpRequest.OPENED;
      fakeA.withCredentials = false;
      fakeA.responseType = "";
      fakeA.timeout = 0;
      try { pz.a5(); } catch (_) {}
      try { pz.a5(fakeA); } catch (_) {}

      // ---- 4. a5 hook 模拟 (方式B: 通过 XHR open 触发原生 hook 链路) ----
      // WAF 脚本已在沙箱中安装了 XMLHttpRequest.prototype.open 的 hook，
      // 创建真实 XHR 并调用 open 即可触发 hook，hook 会改写 this._url。
      let xhrHookUrl = "";
      let xhrHookDiffers = false;
      try {
        const xhr = new XMLHttpRequest();
        xhr.open(method, url, true);
        xhrHookUrl = xhr._url || "";
        xhrHookDiffers = xhrHookUrl !== url && xhrHookUrl.length > 0;
        // 也触发 send 以跑完整 hook 链路
        try { xhr.setRequestHeader("Content-Type", "application/json"); } catch (_) {}
        try { xhr.send(body); } catch (_) {}
      } catch (_) {}

      // ---- 5. 尝试直接用 a5 操作 XHR 实例 (方式C) ----
      let xhrA5Url = "";
      try {
        const xhr2 = new XMLHttpRequest();
        xhr2.open(method, url, true);
        const fakeC = {};
        fakeC[gEKeys.x] = [method, url, true];
        fakeC[gEKeys.O] = body;
        // 以 xhr2 为 this 调用 a5，让 hook 直接操作 XHR 属性
        try { pz.a5.call(xhr2, fakeC); } catch (_) {}
        xhrA5Url = xhr2._url || "";
      } catch (_) {}

      return {
        decodeUrlFromA4: a4Url,
        manualDecode,
        manualPayload,
        manualParts: manualPayload.split("|"),
        globals: { wafBd, wafATs, fpId, env },
        // 方式A: 伪造参数
        fakeOpenArgs: openArgs,
        xhrHookUrlFromA5: openArgs[1] || "",
        // 方式B: 真实 XHR open 触发 hook
        xhrUrl: xhrHookUrl,
        xhrHookDiffers: xhrHookDiffers,
        // 方式C: a5.call(xhr, fake)
        xhrA5Url: xhrA5Url,
        // 调试信息
        debugKeys: { gE: gEKeys, gA: gAKeys },
        localStorage: localStorage.dump ? localStorage.dump() : {},
        runnerInjected: true
      };
    })()
  `, context, { timeout: 60000 });
  result.localStorage = localStorage.dump();
  result.fpAccessLog = fpAccessLog.slice();
  try { result.__pzDbg = (context.__pzDbg && context.__pzDbg.calls) || []; } catch (_) { result.__pzDbg = []; }
  try { result.__pzDbgMeta = { alpha: (context.__pzDbg && context.__pzDbg.alpha) || "", L6: (context.__pzDbg && context.__pzDbg.L6) || "" }; } catch (_) { result.__pzDbgMeta = {}; }
  return result;
}

function runChallenge(options = {}) {
  const html = String(options.html || "");
  if (!html) throw new Error("challenge html is required");
  // Enable real network for XHR/fetch in challenge mode
  enableRealNetwork = true;
  const { context, localStorage, cookieJar } = createContext(options);

  // Debug: wrap sandbox to catch undefined property access before .bind()
  const debugHandler = {
    get(target, prop) {
      const val = target[prop];
      return val;
    }
  };
  // Wrap key objects to catch undefined reads
  const _origFnBind = Function.prototype.bind;
  Function.prototype.bind = function(...args) {
    if (this === undefined) {
      throw new Error('BUG: .bind() called on undefined — likely missing browser API');
    }
    return _origFnBind.apply(this, args);
  };
  // Real fetch using sandbox's XMLHttpRequest (which has real network send)
  envFetch = function(url, init) {
    return new Promise((resolve, reject) => {
      const XHR = context.XMLHttpRequest;
      const xhr = new XHR();
      xhr.open((init && init.method) || "GET", String(url), true);
      if (init && init.headers) {
        for (const [k, v] of Object.entries(init.headers)) {
          xhr.setRequestHeader(k, v);
        }
      }
      xhr.onload = () => resolve(new Response(xhr.responseText, {
        status: xhr.status,
        statusText: xhr.statusText,
        headers: xhr._responseHeaders || {},
      }));
      xhr.onerror = () => reject(new Error(xhr.statusText));
      xhr.send(init && init.body);
    });
  };

  // Parse all scripts from HTML - both inline and external
  const scripts = [];
  const scriptRegex = /<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi;
  let match;
  while ((match = scriptRegex.exec(html)) !== null) {
    const attrs = match[1] || "";
    const code = match[2] || "";
    const srcMatch = attrs.match(/src\s*=\s*["']([^"']+)["']/);
    const nameMatch = attrs.match(/name\s*=\s*["']([^"']+)["']/);

    if (srcMatch) {
      // External script - use options.externalScripts if provided
      const src = srcMatch[1];
      scripts.push({ name: nameMatch ? nameMatch[1] : "external", code: null, external: true, src });
    } else if (code && String(code).trim()) {
      scripts.push({ name: nameMatch ? nameMatch[1] : "inline", code: String(code), external: false });
    }
  }

  // Execute inline scripts FIRST (kyDFBf sets globals like qa/go that external scripts need)
  // Then execute external scripts
  const inlineFirst = []; const externalLast = [];
  for (const item of scripts) {
    if (item.external) externalLast.push(item); else inlineFirst.push(item);
  }
  const orderedScripts = [...inlineFirst, ...externalLast];

  // Execute scripts in order
  const errors = [];
  for (const item of orderedScripts) {
    let source;
    if (item.external) {
      // Look up in externalScripts map provided by Python
      const extScripts = options.externalScripts || {};
      const cached = extScripts[item.src];
      if (cached) {
        source = cached;
      } else {
        // Try to fetch synchronously via Node.js
        try {
          const fullUrl = item.src.startsWith("http") ? item.src :
            new URL(item.src, options.pageUrl || "https://api.pzds.com").href;
          source = fetchExternalScriptSync(fullUrl);
        } catch(e) {
          // Skip external scripts that can't be fetched
          errors.push({
            name: item.name || "external",
            external: true,
            message: e && e.message ? e.message : String(e),
            stack: e && e.stack ? String(e.stack).split("\n").slice(0, 3).join("\n") : "",
          });
          continue;
        }
      }
      if (!source) continue;
    } else {
      source = item.code;
    }

    if (source.includes("function kyDFBf()")) {
      source = injectExports(source, options);
    }
    if (options.debugBindKeys && source.includes("dn[WV(qu.Hs)]&&")) {
      source = source.replace(
        "dn[WV(qu.Hs)]&&",
        "globalThis.__debugBindKeys=[WV(qu.Hs),WV(qu.HB),WV(qu.HZ),WV(qu.HD),WV(qu.HM),WV(qu.HK),WV(qu.Hh),WV(qu.HL)];dn[WV(qu.Hs)]&&"
      );
    }
    try {
      context.document.currentScript = makeScriptNode(source);
      vm.runInContext(source, context, { timeout: 20000, filename: item.name || "challenge.js" });
      context.document.currentScript = null;
    } catch(e) {
      context.document.currentScript = null;
      errors.push({
        name: item.name || "challenge.js",
        external: Boolean(item.external),
        message: e && e.message ? e.message : String(e),
        stack: e && e.stack
          ? String(e.stack).split("\n").filter((line) => /^\s*at\s/.test(line)).slice(0, 8).join("\n")
          : "",
      });
    }
  }

  try {
    const wafTextareaMatch = html.match(/<textarea\s+id=["']aliyun_waf_fa9faf9f45["'][^>]*>([\s\S]*?)<\/textarea>/i);
    const wafTextarea = wafTextareaMatch ? wafTextareaMatch[1]
      .replace(/&quot;/g, '"')
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      : "{}";
    const wafData = JSON.parse(wafTextarea || "{}");
    const rawWaf3d = wafData._waf_3d7faf79 ? String(wafData._waf_3d7faf79) : "";
    const currentStorage = localStorage.dump();
    const rawTs = String(currentStorage._waf_a86dfdc5f2 || "").split("||", 1)[0] || String(Date.now());
    const expireTs = String(Number(rawTs) + 12 * 60 * 60 * 1000);
    if (rawWaf3d) {
      localStorage.setItem("_waf_3d7faf79", `${encodeURIComponent(`${rawWaf3d}|${rawTs}`)}||${expireTs}`);
      localStorage.setItem("__00b204e9800998__", `${encodeURIComponent(rawWaf3d)}||${expireTs}`);
    }
  } catch (_) {}

  const result = {
    localStorage: localStorage.dump(),
    cookie: Array.from(cookieJar.entries()).map(([k, v]) => `${k}=${v}`).join("; "),
    errors,
    debugBindKeys: context.__debugBindKeys || [],
    fpAccessLog: fpAccessLog.slice(),
    globals: {
      _waf_bd8ce2ce37: context._waf_bd8ce2ce37 || "",
      _waf_a86dfdc5f2: context._waf_a86dfdc5f2 || "",
      __00b204e9800998__: context.__00b204e9800998__ || "",
      "api-pc.pxb7.com_dySig": context["api-pc.pxb7.com_dySig"] || "",
    },
  };
  // Reset for next use
  enableRealNetwork = false;
  envFetch = function() { return Promise.resolve(new Response("")); };
  return result;
}

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === "--input") {
      Object.assign(args, JSON.parse(fs.readFileSync(argv[++i], "utf8")));
    } else if (key === "--script") {
      args.scriptPath = argv[++i];
    } else if (key === "--challenge") {
      args.challenge = true;
    } else if (key === "--pretty") {
      args.pretty = true;
    }
  }
  return args;
}

if (require.main === module) {
  const args = parseArgs(process.argv);
  try {
    const result = args.challenge ? runChallenge(args) : run(args);
    process.stdout.write(JSON.stringify(result, null, args.pretty ? 2 : 0));
  } catch (error) {
    process.stderr.write(String(error && error.stack ? error.stack : error));
    process.exit(1);
  }
}

module.exports = { run, runChallenge, injectExports, createContext };
