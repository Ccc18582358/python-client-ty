## 打包命令
    # 1. 用 PyInstaller 打包 exe
    .\.venv\Scripts\python.exe -m PyInstaller --clean price-monitor-system.spec
    
    # 2. 拷贝 config/ 到 dist/
    .\.venv\Scripts\python.exe build.py
    
    # 3. 编译 Inno Setup 安装包
    & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build_output\ScanNumber.iss


### 平台每页spu数量
```
螃蟹是 pageSize: 100，氪金兽 60，7881 是 30, 盼之是10
```