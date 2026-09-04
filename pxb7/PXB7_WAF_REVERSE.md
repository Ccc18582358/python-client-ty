# PXB7 阿里云 WAF `timestamp__1366` 逆向记录

目标站点 `www.pxb7.com`（螃蟹游戏交易平台），接口域名 `api-pc.pxb7.com`。
本文记录 `timestamp__1366` 请求签名参数的离线生成方案（Python + Node，不打开浏览器）。

## 一、结论

- 签名由阿里云 WAF SDK（`.cdp_script15.js`，75KB 单行混淆脚本）中的核心函数
  `LK(input, body, method, 2)` 生成。
- 前缀 `2790552d` 是脚本内写死的常量（`td` 表 `J` 项 / `tg` 表 `LS` 项）。
- 完整签名结构：

```
timestamp__1366 = "2790552d-" + u(LR)

LR = [ h1, h2, env, now, wafBd, wafATs, fpId ].join("|")

h1     = Li(LS(encodeURIComponent(wafBd + methodLower + "2790552d" + canonicalUrl + encodeURIComponent(body)))
h2     = Li(LS(encodeURIComponent(wafBd + methodLower + "2790552d" + canonicalUrl))
env    = L3()                                  # 浏览器环境派生值
now    = String(new Date().getTime())          # 时间戳
wafBd  = window._waf_bd8ce2ce37                # WAF 挑战后的 localStorage
wafATs = window._waf_a86dfdc5f2                # WAF 挑战后的 localStorage
fpId   = I()                                   # 设备指纹 id（localStorage __00b204e9800998__）
```

其中 `methodLower` 为小写 HTTP 方法；`canonicalUrl` 为 `N(LD,false,false)` 规范化后的
URL；body 仅在方法非 `get`/`head`（即 POST 等）时拼接进 `h1`。

- `u()` 是 LZ77 压缩 + 自定义 base64 编码（把 7 字段 payload 压成一段密文串）。
- `Li` / `LS` 是自定义 64 位哈希（16 位十六进制输出，非标准 MD5/SHA1）。
- 与 Panzhi 的 `decode__1174` 同属阿里云 WAF 一族，结构完全一致，仅前缀不同
  （PXB7=`2790552d`，Panzhi=`214d4f07715`）与哈希/压缩内部实现不同。

## 二、调用点还原

抓包定位到的调用点（已反混淆）：

```js
x[SC(tB.r)](LK, LZ[SC(tB.h)], LZ[SC(tB.N)][SC(tB.w)], LZ[SC(tB.Y)][SC(tB.a)], 2)
//  = eToFv(LK, input, init.body, init.method, 2)
```

`eToFv` 直接转发：`eToFv(a,b,c,d,e) => a(b,c,d,e)`。
即最终等价于 `LK(input, init.body, init.method, 2)`：

| 形参 | 含义 |
|------|------|
| `input` | fetch/XHR 的请求 URL |
| `init.body` | 请求体 |
| `init.method` | HTTP 方法 |
| `2` | 模式常量（`0x8e + -0x258d + 0x2501`） |

`tB` 表关键项（已解码）：`r`→`eToFv`、`h`→`input`、`N`→`init`、`w`→`body`、
`Y`→`init`、`a`→`method`。

## 三、`td` 映射表（完整解码）

`LK` 内部使用 `td` 表（混淆标识符 → 字符串索引），解码后：

```
x:fLUGb(状态机顺序 "8|2|4|0|11|3|12|5|9|6|7|1|10")
Z:split  E:ylVFE(==)  b:zpfYx("get")  j:AosYx("head")  W:indexOf
M:Uint8Array  d:fwOel(instanceof)  B:Nzoqx(<)  c:byteLength
R:encodeURIC + D:omponent  → encodeURIComponent
V:RmUQI("GET")  m:toLowerCas(+e → toLowerCase)
I:iqTyG(call)  g:fUWZN(call)  s:KzCsf(call)  k:DZpHF(call3)
p:encodeURIC + l:omponent  → encodeURIComponent
H:tPXof(+)  z:HdWjo(+)  A:cgNBj(+)
v:_waf_bd8ce + u:2ce37 → _waf_bd8ce2ce37
J:HZEji("2790552d")  q:aHVAN(!=)  Q:KBvsd(call)
N:Sjoiv(call)  G:jNQGK(call)  O:rGdKY(invoke)
r:LVwIr(+)  h:Date  w:getTime
Y:_waf_bd8ce  a:_waf_a86df  F:dc5f2 → _waf_a86dfdc5f2
X:VbnzQ(invoke)  C:join  e:HbiDl(+)
o:YqKiX("2790552d-")  T:now  P:now  y:freuI(call)
L0:hostname  L1:search  L2:vVZOL(call3)  L3:search  L4:xGkPC(call3)
L5:Math  L6:random  L7:QOEUm(call)
```

`LK` 状态机执行顺序（`x[fLUGb]`）：`8 → 2 → 4 → 0 → 11 → 3 → 12 → 5 → 9 → 6 → 7 → 1 → 10`：

| 状态 | 动作 |
|------|------|
| 8 | `Ls = window` |
| 2 | `Lb`（method）转小写，缺省 `GET` |
| 4 | `LD = q(Ln(LZ))` 解析 URL；`Lm = N(LD,false,false)` 规范化 URL；`LI = Lm = encodeURIComponent(bd+method+"2790552d"+Lm)` |
| 0 | 若非 get/head，追加 `encodeURIComponent(body)` 到 `Lm` |
| 11 | `window.Math.random = H(A)`（`A="65920277"`，注入确定性 PRNG） |
| 3 | `LR = LS(Lm)`（哈希 Lm） |
| 12 | `Lk = LR` |
| 5 | 若 `Lm != LI`，`Lk = LS(LI)`；组装 7 字段 `LR = [Li(LR), Li(Lk), L3(), now, bd, ats, I()].join('|')` |
| 9 | `Date.now()` 预热 |
| 6 | 声明 `Lg` |
| 7 | `Lg = "2790552d-" + u(LR)`（最终签名） |
| 1 | `Lc = {}` |
| 10 | `Lc[LU(hostname)] = Lg`；`LD.search = Q(LD.search, Lc)`；`return N(LD, mode)` |

### 3.1 参数名由 hostname 派生（`LU`）

`timestamp__1366` 里的参数名 **不是** 写死的，而是由域名字符码之和派生：

```js
function LU(hostname) {                 // hostname = api-pc.pxb7.com
  if (L5[hostname]) return L5[hostname];
  var sum = 0;
  for (var i = 0; i < hostname.length; i++) sum += hostname[i].charCodeAt();
  var name = L4[sum % L4.length] + String(sum % 10000);
  return L5[hostname] = name;
}
```

其中 `L4 = ["type__","refer__","ipcity__","md5__","decode__","encode__","time__","timestamp__","type__"]`。

验证：

| hostname | sum | `L4[sum%9]` | 后缀 `sum%10000` | 参数名 |
|----------|-----|-------------|------------------|--------|
| `api-pc.pxb7.com` | 1366 | `timestamp__` | 1366 | `timestamp__1366` |
| `www.pxb7.com`    | 1153 | `refer__`     | 1153 | `refer__1153` |

这解释了同一站点多个 WAF 参数（`timestamp__`/`refer__`/`decode__`/`md5__` 等）
共存的原因：每个子域名落点不同。

## 四、核心辅助函数

| 函数 | 位置(字符偏移) | 作用 |
|------|--------------|------|
| `LK` | 70705 | 签名主函数（控制流扁平化状态机） |
| `Lf` | 72240 | fetch/XHR hook（拦截请求并改写 URL） |
| `LA`(=i) | 18275 | 字符串解码器 |
| `Li` | 70359 | 哈希 → 小写十六进制 |
| `LS` | 69517 | 哈希 → 16 元素数组 |
| `L3` | 67191 | 环境派生值（随机/指纹） |
| `u` | 36745 | LZ77 压缩 + base64 编码 |
| `I` | 34620 | 设备指纹 id（缓存于 `m`，读 localStorage） |
| `LU` | — | 由 hostname 派生 WAF 参数名（见 §3.1） |
| `q` | — | URL 解析（protocol/host/hostname/pathname/search/hash） |
| `N` | — | URL 重建（模式控制全/相对路径） |
| `Q` | — | 查询串合并 |
| `O` | — | 字符串拼接工具 |
| `H` | — | 确定性 PRNG 工厂（LCG） |
| `L6` | — | 256 项字节 → percent 编码表 |

## 五、离线生成

```
pxb7_waf.py            # Python 桥
pxb7_waf_runner.js     # Node vm 沙箱 runner
.cdp_script15.js       # pxb7 WAF SDK（静态脚本）
```

```python
from pxb7 import pxb7_waf

url = "https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList"
body = {"page": 1, "pageSize": 20}
signed = pxb7_waf.build_request_url(url, body, method="POST")
# → ...?timestamp__1366=2790552d-...
```

需要传入真实会话的 WAF 状态（`local_storage_data`），否则签名的
`wafBd`/`wafATs`/`fpId` 字段为空或随机，服务端可能拒绝：

```python
pxb7_waf.build_request_url(
    url, body,
    local_storage_data={
        "_waf_bd8ce2ce37": "...",       # 挑战后 localStorage
        "_waf_a86dfdc5f2": "...",
        "__00b204e9800998__": "...",    # 设备指纹 id
        "api-pc.pxb7.com_dySig": "...",
    },
)
```

## 六、注意事项

- `tk` 自防御 token 对 PXB7 为 **276**（Panzhi 旧脚本为 790）。runner 已硬编码 276，
  并跳过 `tl()` 重算，保证字符串表正确解码（否则旋转 IIFE 死循环）。
- `L3()` / `I()` 含随机分量（`Math.random` / `Date.now`），在真实浏览器中首次计算后
  会写入 localStorage（`__00b204e9800998__`），后续请求读取缓存值，从而稳定。
  离线复现时需提供该缓存值。
- 该脚本使用 `Math.random = H(A)` 注入确定性 PRNG，保证哈希/压缩部分可复现。
