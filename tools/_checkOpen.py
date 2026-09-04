# import requests
# from urllib.parse import quote
#
# token = "4d867ea0a1dc3eafa7db1790d58ab506"
# name = quote("扫号器代理")
# url = f"https://api.nyyyds.com/open/apipaid/function/deductionCount?name={name}&type=2"
#
# resp = requests.get(
#   url,
#   headers={"token": token},
#   timeout=6,
# )
#
# print(resp.status_code)
# print(resp.headers.get("content-type"))
# print(resp.text)
#
#

# ! /usr/bin/python
# coding=utf-8

# ! /usr/bin/python
# coding=utf-8
import requests
import time

# 请求地址
targetUrl = "http://myip.ipip.net"
#
# #代理服务器
proxyHost = "代理IP"
proxyPort = 代理端口

# 非账号密码验证
proxyMeta = "http://%(host)s:%(port)s" % {

  "host": proxyHost,
  "port": proxyPort,
}
# 账号密码验证
# proxyMeta = "http://账号:密码@%(host)s:%(port)s" % {
#    "host": proxyHost,
#    "port": proxyPort,
# }

#
# #pip install -U requests[socks]  socks5代理
# 非账号密码验证
# proxyMeta = "socks5://%(host)s:%(port)s" % {
#
#     "host" : proxyHost,
#
#     "port" : proxyPort,
#
# }
# 账号密码验证
# proxyMeta = "socks5://账号:密码%(host)s:%(port)s" % {
#
#     "host" : proxyHost,
#
#     "port" : proxyPort,
#
# }
#
proxies = {
  "http": proxyMeta,
  "https": proxyMeta
}
#
start = int(round(time.time() * 1000))
resp = requests.get(targetUrl, proxies=proxies, timeout=10)
costTime = int(round(time.time() * 1000)) - start
print(resp.text)
print("耗时：" + str(costTime) + "ms")
