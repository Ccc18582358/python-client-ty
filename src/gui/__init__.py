"""gui 包：PySide6 + qfluentwidgets 界面

├── main_window    主窗口（FluentWindow + 4 页面）
├── login_window   登录窗（独立的 QEventLoop 阻塞）
├── theme          主题/字体/QSS 工具
└── pages
    ├── scan_task_page    扫号任务管理
    ├── deal_page         捡漏商品（已估价 + 溢价 > 0）
    ├── history_page      历史归档
    └── calc_page         Excel 文件上传/管理
"""
