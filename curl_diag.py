import sys, traceback, json

result = {"frozen": getattr(sys, "frozen", False)}
try:
    from curl_cffi import requests as curl_requests
    result["HAS_CURL_CFFI"] = True
    try:
        import curl_cffi
        result["version"] = curl_cffi.__version__
    except Exception as e:
        result["version_err"] = repr(e)
    # Try to actually build a session with impersonate
    try:
        s = curl_requests.Session(impersonate="chrome136")
        result["session_ok"] = True
    except Exception as e:
        result["session_err"] = repr(e)
except Exception as e:
    result["HAS_CURL_CFFI"] = False
    result["import_error"] = f"{type(e).__name__}: {e}"
    result["tb"] = traceback.format_exc()

out = r"C:\PythonProduct\tongyong\python-client-ty\curl_diag_out.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
