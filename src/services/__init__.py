"""services 包：业务服务层

├── scan_scheduler              调度器（每 30s tick）
├── estimation_worker           异步估价 Worker（QThread）
├── scan_task_service           业务编排（议价公式/去重/单商品估价）
├── scanner_registry            爬虫函数注册中心（多爬虫用）
├── scanner_service             爬虫回调落库入口（直接嵌套，不再走 HTTP）
├── demo_scanner                爬虫模板（真实爬虫接入后可以删除）
├── jingxi_spider_pangxie       螃蟹(鲸汐)账号爬虫 — 异步翻页+详情抓取
├── jingxi_scanner              螃蟹爬虫适配器 — 接入 GUI 调度系统
├── jingxi_spider_panzhi        盼之代售账号爬虫 — 同步翻页抓取
├── jingxi_panzhi_scanner       盼之爬虫适配器 — 接入 GUI 调度系统
├── jingxi_spider_kejinshou     氪金兽账号爬虫 — 异步翻页(H5 GET 签名)
├── jingxi_kejinshou_scanner    氪金兽爬虫适配器 — 接入 GUI 调度系统
├── jingxi_spider_7881          7881账号列表爬虫 — 列表页签名抓取
├── jingxi_7881_scanner         7881爬虫适配器 — 接入 GUI 调度系统
└── panzhi_folder/              盼之辅助模块（签名生成 + WAF Cookie 生成）
"""
