# -*- mode: python ; coding: utf-8 -*-

# ★ 修复估价引擎：formulas 用 importlib.import_module('.info'/'.logic'/...) 动态加载
#   functions 子模块，PyInstaller 静态分析追踪不到 → 冻结后估价子进程报
#   "No module named 'formulas.functions.info'"。collect_all 一并收集其全部子模块
#   (hiddenimports) + 数据文件(units.json 等)，避免漏打。
from PyInstaller.utils.hooks import collect_all, collect_submodules
_formulas_datas, _formulas_binaries, _formulas_hidden = collect_all('formulas')

# ★ 修复估价：scipy 的 array_api_compat 用 __import__(__package__ + '.fft'/'.linalg') 动态加载
#   后端各子模块，PyInstaller 追踪不到 → 冻结后估价子进程报
#   "No module named 'scipy._lib.array_api_compat.numpy.fft'"。这里补齐其后端子模块。
_scipy_aac_hidden = collect_submodules('scipy._lib.array_api_compat')


a = Analysis(
    ['main.py'],
    pathex=['src', '.'],  # ★ src: 业务包; '.'(项目根): 让 PyInstaller 能解析到 pxb7 逆向包
    binaries=_formulas_binaries,
    datas=[
        ('src', 'src'),
        # ★ 修复逆向资源定位：frozen 下模块 __file__ 无 src/ 前缀(pathex 让 services 成为顶层包)，
        #   而 ('src','src') 只把 .js 打到 src/services/...，差一个 src/。这里把 Node runner/动态脚本
        #   再补一份到 services/panzhi_folder/（与 __file__ 同级），让 Path(__file__).parent 直接命中。
        ('src/services/panzhi_folder/*.js', 'services/panzhi_folder'),
        # ★ 修复签名：pz_wasm_sign_runner.js 需要 __dirname/pzds_browser_sign_cache（WASM/mjs 缓存），
        #   *.js glob 抓不到这个子目录，导致冻结后签名回退到 MD5（服务端拒绝 → 460）。
        ('src/services/panzhi_folder/pzds_browser_sign_cache', 'services/panzhi_folder/pzds_browser_sign_cache'),
        # ★ 修复签名：pz_wasm_sign_runner.js 顶部 require('jsdom')，冻结后 _MEIPASS 里没有 node_modules，
        #   Node 找不到 jsdom → 签名回退 MD5。把整个 node_modules 打到 _MEIPASS/node_modules，
        #   让 runner 从 services/panzhi_folder/ 向上解析到 node_modules/jsdom。
        ('node_modules', 'node_modules'),
        # ★ 修复螃蟹：整个 pxb7 包打进 bundle（原来完全漏打；含隐藏文件 .cdp_script15.js，必须整目录打）
        ('pxb7', 'pxb7'),
        # ('config', 'config'),  # ★ 2026-06-16 不再打包 config/：让用户直接在 exe 根目录编辑 config/active.json 切换环境
        ('logo', 'logo'),
    ] + _formulas_datas,
    hiddenimports=[
        # ---- PySide6 ----
        'PySide6',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtNetwork',
        'shiboken6',
        # ---- qfluentwidgets / qframelesswindow ----
        'qfluentwidgets',
        'qframelesswindow',
        'darkdetect',
        # ---- 业务包 ----
        'gui',
        'gui.theme',
        'gui.login_window',
        'gui.main_window',
        'gui.widgets',
        'gui.widgets.pagination',
        'gui.pages',
        'gui.pages.scan_task_page',
        'gui.pages.deal_page',
        'gui.pages.history_page',
        'gui.pages.calc_page',
        'api',
        'api.auth_client',
        'api.session',
        'database',
        'database.db_manager_v2',
        'config',
        'config.config',  # ★ session.py 动态 sys.path 导入，PyInstaller 分析阶段追踪不到
        'config.config_manager',
        'config.api_config',
        'excel',
        'excel.excel_calculator_final',
        # ---- 工具层 ----
        'utils',
        'utils.logger',
        # ---- 服务层 ----
        'services',
        'services.proxy_manager',
        'services.proxy_alert_notifier',
        'services.proxy_paywall',
        'services.proxy_switch_budget',
        'services.proxy_token_provider',
        'services.shenlong_client',
        'services.dingtalk_notify',
        # ---- 螃蟹逆向包（pxb7，项目根下，需 pathex '.' + datas 打包）----
        'pxb7',
        'pxb7.pxb7_waf_client',
        'pxb7.pxb7_waf',
        'pxb7.pxb7_refresh_waf_state',
        'pxb7.pxb7_browser_fingerprint',
        # ---- 第三方 ----
        'webbrowser',
        'sqlite3',
        'win32com',
        'win32com.client',
        'pythoncom',
        'flask',
        'flask_cors',
        'werkzeug',
        'jinja2',
        'markupsafe',
        'itsdangerous',
        'click',
        'blinker',
        'tempfile',
        'openpyxl',
        'openpyxl.styles',
        'openpyxl.styles.font',
        'openpyxl.styles.alignment',
        'openpyxl.styles.border',
        'openpyxl.styles.side',
        'openpyxl.styles.pattern',
        'openpyxl.utils',
        'openpyxl.workbook',
        'openpyxl.worksheet',
        'openpyxl.cell',
        'openpyxl.xml',
        'openpyxl.xml.functions',
        'openpyxl.xml.constants',
        'et_xmlfile',
        'requests',
        'requests.adapters',
        'requests.auth',
        'requests.cookies',
        'requests.exceptions',
        'requests.models',
        'requests.sessions',
        'requests.utils',
        'urllib3',
        'urllib3.util',
        'urllib3.util.retry',
        'urllib3.util.url',
        'urllib3.connectionpool',
        'urllib3.poolmanager',
        'urllib3.response',
        'PIL',
        'PIL.Image',
    ] + _formulas_hidden + _scipy_aac_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        '_tkinter',
        'tkinter.ttk',
        'tkinter.scrolledtext',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'pkg_resources',  # ★ PyInstaller 6.0 + setuptools>=67 兼容性：app 未使用，排除避免 jaraco 导入错误
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='扫号器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
