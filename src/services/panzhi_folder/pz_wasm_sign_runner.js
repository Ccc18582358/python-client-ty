const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");
const vm = require("vm");
const { pathToFileURL } = require("url");
const { JSDOM } = require("jsdom");

const CONFIG_URL = process.env.PZ_WASM_CONFIG_URL || "https://oss.pzds.com/mobileV3/wasm-config-prod.json";
const BROWSER_SIGN_JS_URL = "https://oss.pzds.com/mobileV3/UZejJOif.js";
const BROWSER_SIGN_WASM_URL = "https://oss.pzds.com/mobileV3/c3225e65.wasm";
const BROWSER_SIGN_VERSION = "v17";
// 缓存目录放在脚本同级目录，部署到服务器时不依赖绝对路径。
const BROWSER_SIGN_CACHE_DIR = path.join(__dirname, "pzds_browser_sign_cache");
const BROWSER_SIGN_JS_PATH = path.join(BROWSER_SIGN_CACHE_DIR, "UZejJOif.mjs");
const BROWSER_SIGN_WASM_PATH = path.join(BROWSER_SIGN_CACHE_DIR, "c3225e65.wasm");
const BROWSER_SIGN_JS_META_PATH = path.join(BROWSER_SIGN_CACHE_DIR, "UZejJOif.meta.json");
const BROWSER_SIGN_WASM_META_PATH = path.join(BROWSER_SIGN_CACHE_DIR, "c3225e65.meta.json");
const PC_GRAY_APIS = [
  "/web-client/v2/public/goodsPublic/page",
  "/web-client/v2/userCenter/saveGoods",
];

async function fetchText(url) {
  const response = await fetch(url, { headers: { "Cache-Control": "no-cache", Pragma: "no-cache" } });
  if (!response.ok) throw new Error(`HTTP ${response.status} ${url}`);
  return await response.text();
}

async function fetchBytes(url) {
  const response = await fetch(url, { headers: { "Cache-Control": "no-cache", Pragma: "no-cache" } });
  if (!response.ok) throw new Error(`HTTP ${response.status} ${url}`);
  return new Uint8Array(await response.arrayBuffer());
}

async function deriveFallbackUrl(wasmUrl) {
  const bytes = await fetchBytes(wasmUrl);
  const hash = crypto.createHash("md5").update(bytes).digest("hex").slice(0, 8);
  const fall = crypto.createHash("md5").update(`${hash}_fall`).digest("hex").slice(0, 8) + ".js";
  return wasmUrl.slice(0, wasmUrl.lastIndexOf("/") + 1) + fall;
}

async function deriveBrowserAssetUrls(wasmUrl, wasmPath = "") {
  const bytes = wasmPath && fs.existsSync(wasmPath)
    ? fs.readFileSync(wasmPath)
    : Buffer.from(await fetchBytes(wasmUrl));
  const hash = crypto.createHash("md5").update(bytes).digest("hex").slice(0, 8);
  const base = wasmUrl.slice(0, wasmUrl.lastIndexOf("/") + 1);
  return {
    glueFileUrl: base + crypto.createHash("md5").update(`${hash}_glue`).digest("hex").slice(0, 8) + ".js",
    fallbackUrl: base + crypto.createHash("md5").update(`${hash}_fall`).digest("hex").slice(0, 8) + ".js",
  };
}

function cachePathForUrl(url, suffix = "") {
  const parsed = new URL(url);
  const base = path.basename(parsed.pathname).replace(/[^a-zA-Z0-9._-]/g, "_");
  if (!suffix) return path.join(BROWSER_SIGN_CACHE_DIR, base);
  return path.join(BROWSER_SIGN_CACHE_DIR, base.replace(/\.[^.]+$/, "") + suffix);
}

function isPcGrayApi(apiUrl) {
  if (!apiUrl) return false;
  return PC_GRAY_APIS.some((item) => String(apiUrl).includes(item));
}

function selectPcVersion(pcConfig, apiUrl) {
  if (!pcConfig) return "";
  if (!pcConfig.apiFullList && !isPcGrayApi(apiUrl)) return pcConfig.currentVersion;
  const percent = Number(pcConfig.rollout && pcConfig.rollout.percent);
  const rollout = Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : 0;
  return rollout > 0 ? pcConfig.candidateVersion : pcConfig.currentVersion;
}

function readJsonIfExists(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    return null;
  }
}

async function updateCachedFile(url, filePath, metaPath, kind) {
  await fs.promises.mkdir(BROWSER_SIGN_CACHE_DIR, { recursive: true });
  const meta = readJsonIfExists(metaPath) || {};
  const headers = {
    "Cache-Control": "no-cache",
    Pragma: "no-cache",
  };
  if (meta.etag) headers["If-None-Match"] = meta.etag;
  if (meta.lastModified) headers["If-Modified-Since"] = meta.lastModified;

  const response = await fetch(url, { headers });
  if (response.status === 304 && fs.existsSync(filePath)) {
    return;
  }
  if (!response.ok) throw new Error(`HTTP ${response.status} ${url}`);

  let body;
  if (kind === "text") {
    body = await response.text();
    await fs.promises.writeFile(filePath, body, "utf8");
  } else {
    body = Buffer.from(await response.arrayBuffer());
    await fs.promises.writeFile(filePath, body);
  }

  const nextMeta = {
    etag: response.headers.get("etag") || "",
    lastModified: response.headers.get("last-modified") || "",
    sha256: crypto.createHash("sha256").update(body).digest("hex"),
    fetchedAt: new Date().toISOString(),
  };
  await fs.promises.writeFile(metaPath, JSON.stringify(nextMeta, null, 2), "utf8");
}

async function ensureBrowserSignAssets() {
  // 先更新本地缓存；如果远端资源没有变化，就直接复用已有文件。
  await updateCachedFile(BROWSER_SIGN_JS_URL, BROWSER_SIGN_JS_PATH, BROWSER_SIGN_JS_META_PATH, "text");
  await updateCachedFile(BROWSER_SIGN_WASM_URL, BROWSER_SIGN_WASM_PATH, BROWSER_SIGN_WASM_META_PATH, "bytes");
}

async function fetchRemoteBrowserAssets() {
  // 本地缓存不可写时，直接把远端 JS/WASM 拉到临时目录再执行。
  const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "pzds-browser-sign-"));
  const jsPath = path.join(tempDir, "03574d3f.mjs");
  const wasmPath = path.join(tempDir, "ad96acb6.wasm");
  const jsSource = await fetchText(BROWSER_SIGN_JS_URL);
  const wasmBytes = await fetchBytes(BROWSER_SIGN_WASM_URL);
  await fs.promises.writeFile(jsPath, jsSource, "utf8");
  await fs.promises.writeFile(wasmPath, Buffer.from(wasmBytes));
  return { jsPath, wasmPath, source: "remote" };
}

async function resolveConfiguredBrowserAssetPaths(apiUrl) {
  const config = JSON.parse(await fetchText(CONFIG_URL));
  const pc = config.pc || config;
  const selectedVersion = selectPcVersion(pc, apiUrl) || pc.currentVersion;
  const wasm = pc.wasm && pc.wasm[selectedVersion];
  if (!wasm || !wasm.url) throw new Error(`pc wasm version missing: ${selectedVersion}`);

  await fs.promises.mkdir(BROWSER_SIGN_CACHE_DIR, { recursive: true });
  const wasmPath = cachePathForUrl(wasm.url);
  const wasmMetaPath = cachePathForUrl(wasm.url, ".meta.json");
  await updateCachedFile(wasm.url, wasmPath, wasmMetaPath, "bytes");
  const derived = await deriveBrowserAssetUrls(wasm.url, wasmPath);
  const jsUrl = wasm.glueFileUrl || derived.glueFileUrl;
  const jsPath = cachePathForUrl(jsUrl, ".mjs");
  const jsMetaPath = cachePathForUrl(jsUrl, ".meta.json");
  await updateCachedFile(jsUrl, jsPath, jsMetaPath, "text");
  return {
    jsPath,
    wasmPath,
    wasmUrl: wasm.url,
    version: selectedVersion,
    source: "config",
  };
}

async function resolveBrowserAssetPaths(apiUrl = "") {
  try {
    return await resolveConfiguredBrowserAssetPaths(apiUrl);
  } catch (error) {
    if (process.env.PZ_STRICT_WASM_CONFIG === "1") throw error;
  }
  try {
    await ensureBrowserSignAssets();
    if (fs.existsSync(BROWSER_SIGN_JS_PATH) && fs.existsSync(BROWSER_SIGN_WASM_PATH)) {
      return {
        jsPath: BROWSER_SIGN_JS_PATH,
        wasmPath: BROWSER_SIGN_WASM_PATH,
        wasmUrl: BROWSER_SIGN_WASM_URL,
        version: BROWSER_SIGN_VERSION,
        source: "cache",
      };
    }
  } catch (error) {
    // 本地缓存更新失败时，回退到直接使用远端资源。
  }
  return await fetchRemoteBrowserAssets();
}

function copyWindowProps(src, target) {
  for (const key of Reflect.ownKeys(src)) {
    if (key in target) continue;
    const desc = Object.getOwnPropertyDescriptor(src, key);
    if (desc) Object.defineProperty(target, key, desc);
  }
}

async function loadBrowserGenerateSign(apiUrl = "") {
  const assetPaths = await resolveBrowserAssetPaths(apiUrl);
  const dom = new JSDOM("", {
    url: "https://www.pzds.com/releaseRealGoods",
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });
  const win = dom.window;
  global.window = win;
  global.self = win;
  global.document = win.document;
  global.navigator = win.navigator;
  global.location = win.location;
  global.history = win.history;
  global.screen = win.screen;
  global.performance = win.performance;
  // 修复 jsdom 覆盖原生 performance 导致 Node.js fetch/undici 报错
  // TypeError: performance.markResourceTiming is not a function
  // undici 需要的 resource timing API，jsdom 的 performance 没有这些方法
  if (!global.performance.markResourceTiming) {
    global.performance.markResourceTiming = function () {};
  }
  if (!global.performance.clearResourceTimings) {
    global.performance.clearResourceTimings = function () {};
  }
  if (!global.performance.setResourceTimingBufferSize) {
    global.performance.setResourceTimingBufferSize = function () {};
  }
  global.crypto = win.crypto;
  global.top = win;
  global.parent = win;
  global.globalThis = global;
  global.global = global;
  copyWindowProps(win, global);

  const mod = await import(pathToFileURL(assetPaths.jsPath).href);
  if (typeof mod.default === "function") {
    try {
      await mod.default(assetPaths.wasmUrl || fs.readFileSync(assetPaths.wasmPath));
    } catch (error) {
      await mod.default(fs.readFileSync(assetPaths.wasmPath));
    }
  } else if (typeof mod.initSync === "function") {
    mod.initSync(fs.readFileSync(assetPaths.wasmPath));
  }
  if (typeof mod.generate_sign !== "function") {
    throw new Error("browser generate_sign not found");
  }
  return {
    generateSign: mod.generate_sign,
    version: assetPaths.version || BROWSER_SIGN_VERSION,
  };
}

function loadFallbackGenerateSign(source, mode) {
  const isServer = mode === "server";
  const sandbox = {
    module: { exports: {} },
    exports: {},
    window: isServer
      ? {}
      : { location: { href: "https://www.pzds.com/releaseRealGoods", hostname: "www.pzds.com" } },
    globalThis: {},
    self: {},
    console,
    process: { env: { WASM_SSR_KEY: "wasm-access-pzds-3qXyB7uf" } },
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  vm.runInNewContext(source, sandbox, { timeout: 5000 });
  const candidates = [
    sandbox.WasmAntibotFallback,
    sandbox.window && sandbox.window.WasmAntibotFallback,
    sandbox.default,
    sandbox.module && sandbox.module.exports,
  ];
  for (const item of candidates) {
    if (item && typeof item.generateSign === "function") return item.generateSign;
    if (typeof item === "function") return item;
  }
  const found = Object.values(sandbox).find((value) => value && typeof value.generateSign === "function");
  if (found) return found.generateSign;
  throw new Error("fallback generateSign not found");
}

async function sign(body, method, apiUrl, timestamp, random, version, mode) {
  if (mode === "browser") {
    // browser 模式不打开真实浏览器，只补 JS/WASM 环境后直接计算 v17 签名。
    const browserSign = await loadBrowserGenerateSign(apiUrl);
    const generateSign = browserSign.generateSign;
    const effectiveTimestamp = timestamp || Date.now();
    const effectiveRandom = random || Math.floor(111111 + Math.random() * 888888);
    const signValue = generateSign(
      JSON.stringify(body == null ? {} : body),
      String(method || "GET"),
      String(effectiveTimestamp),
      String(effectiveRandom)
    );
    return {
      Timestamp: effectiveTimestamp,
      Random: effectiveRandom,
      sign: signValue,
      version: browserSign.version || BROWSER_SIGN_VERSION,
      mode: "browser",
    };
  }

  const config = JSON.parse(await fetchText(CONFIG_URL));
  const pc = config.pc || config;
  const selectedVersion = version || pc.currentVersion;
  const wasm = pc.wasm[selectedVersion];
  if (!wasm || !wasm.url) throw new Error(`wasm version missing: ${selectedVersion}`);
  const fallbackUrl = wasm.fallback || (await deriveFallbackUrl(wasm.url));
  const source = await fetchText(fallbackUrl);
  const generateSign = loadFallbackGenerateSign(source, mode || "browser");
  const effectiveTimestamp = timestamp || Date.now();
  const effectiveRandom = random || Math.floor(111111 + Math.random() * 888888);
  const signValue = generateSign(
    JSON.stringify(body == null ? {} : body),
    String(method || "GET"),
    String(effectiveTimestamp),
    String(effectiveRandom)
  );
  return {
    Timestamp: effectiveTimestamp,
    Random: effectiveRandom,
    sign: signValue,
    version: selectedVersion,
    mode: mode || "browser",
    fallbackUrl,
  };
}

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === "--input") Object.assign(args, JSON.parse(fs.readFileSync(argv[++i], "utf8")));
    else if (key === "--version") args.version = argv[++i];
    else if (key === "--mode") args.mode = argv[++i];
    else if (key === "--config") args.printConfig = true;
  }
  return args;
}

if (require.main === module) {
  const args = parseArgs(process.argv);
  const task = args.printConfig
    ? fetchText(CONFIG_URL).then((text) => JSON.parse(text))
    : sign(args.body, args.method || "post", args.apiUrl || "", args.timestamp, args.random, args.version, args.mode);
  task
    .then((result) => process.stdout.write(JSON.stringify(result)))
    .catch((error) => {
      process.stderr.write(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
}

module.exports = { sign };



