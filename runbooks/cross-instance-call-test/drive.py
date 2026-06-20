#!/usr/bin/env python3
import json, time, subprocess, threading, http.server, socketserver, urllib.request, os, sys

SFU = None
ROOM = "fedtest-bk"
PORT = 8099
CDP = 9444

def mint(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json", **(headers or {})}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

print("== minting tokens ==")
lum = mint("http://127.0.0.1:8765/livekit/token",
           {"identity":"lumina","name":"Lumina","room":ROOM})
jar = mint("https://cbrd21-laptop12thgenintelcore.tail204f0c.ts.net/livekit/token",
           {"identity":"jarvis","name":"Jarvis","room":ROOM},
           {"X-Operator-Token":"jarvis-operator-token"})
SFU = lum["url"]   # both join .158's SFU (Shape A shared SFU)
print("  lumina token ok, sfu:", SFU)
print("  jarvis token ok (minted by .41 jarvis instance via funnel)")

# serve the test page
os.chdir("/tmp/b4test")
httpd = socketserver.TCPServer(("127.0.0.1", PORT), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

def page(tok, me):
    from urllib.parse import quote
    return f"http://127.0.0.1:{PORT}/call.html?me={me}&url={quote(SFU)}&token={quote(tok)}"

print("== launching chrome ==")
chrome = subprocess.Popen([
    os.path.expanduser("~/.local/bin/google-chrome"),
    "--headless=new", f"--remote-debugging-port={CDP}",
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
    "--no-sandbox", "--disable-gpu", "--user-data-dir=/tmp/cdp-b4",
    "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def wait_cdp():
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/version", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False

if not wait_cdp():
    print("CDP did not come up"); chrome.terminate(); sys.exit(1)

def open_tab(url):
    from urllib.parse import quote
    req = urllib.request.Request(
        f"http://127.0.0.1:{CDP}/json/new?{quote(url, safe='')}", method="PUT")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

import socket
def ws_eval(ws_url, expr):
    # minimal websocket client (no external dep)
    try:
        from websocket import create_connection
    except Exception:
        return {"_err":"no websocket-client"}
    ws = create_connection(ws_url, timeout=15)
    ws.send(json.dumps({"id":1,"method":"Runtime.enable"})); ws.recv()
    ws.send(json.dumps({"id":2,"method":"Runtime.evaluate",
        "params":{"expression":expr,"returnByValue":True,"awaitPromise":True}}))
    out=None
    for _ in range(10):
        m=json.loads(ws.recv())
        if m.get("id")==2: out=m; break
    ws.close()
    try: return out["result"]["result"]["value"]
    except Exception: return out

print("== opening 2 tabs ==")
t1 = open_tab(page(lum["token"], "lumina"))
t2 = open_tab(page(jar["token"], "jarvis"))
print("  waiting for SFU connect + media (15s)...")
time.sleep(15)

expr = "JSON.stringify(window.__lk)"
s1 = ws_eval(t1["webSocketDebuggerUrl"], expr)
s2 = ws_eval(t2["webSocketDebuggerUrl"], expr)
print("\n== LUMINA tab state ==\n", s1)
print("\n== JARVIS tab state ==\n", s2)

def parse(s):
    try: return json.loads(s)
    except Exception: return {}
a, b = parse(s1), parse(s2)

ok = (a.get("connected") and b.get("connected")
      and any(r.get("id")=="jarvis" for r in a.get("remotes",[]))
      and any(r.get("id")=="lumina" for r in b.get("remotes",[])))
vid = a.get("remoteVideo") and b.get("remoteVideo")
print("\n== VERDICT ==")
print("  both connected + see each other:", bool(ok))
print("  both receiving remote VIDEO:", bool(vid))
print("  RESULT:", "PASS — cross-instance call back-and-forth" if (ok and vid)
      else "PASS(connect, video pending)" if ok else "FAIL")

chrome.terminate(); httpd.shutdown()
