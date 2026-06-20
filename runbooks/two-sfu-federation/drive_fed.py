#!/usr/bin/env python3
# Shape-B FEDERATED call proof (PASSED 2026-06-20): a conf hosted on .158's reachable SFU,
# joined by jarvis using a SOVEREIGN CROSS-REALM token (jarvis signs an assertion -> lumina
# validates trust+pinned key -> mints with lumina's SFU secret), discovered via the relay.
# Result: lumina sees jarvis@chef.skworld w/ video, jarvis sees lumina w/ video — both ways.
# Prereqs: reuse call.html from runbooks/cross-instance-call-test/; write /tmp/b4test/{sfu,lumtok,jartok}.
#   ROOM minted on lumina (POST /conf/create + /conf/{room}/token); jartok via:
#   ssh .41 'SKAGENT=jarvis skchat conf join-federated --host https://noroc2027.tail204f0c.ts.net --room $ROOM --json'
import json, time, subprocess, threading, http.server, socketserver, urllib.request, os, sys
from urllib.parse import quote

PORT=8099; CDP=9444
SFU=open("/tmp/b4test/sfu").read().strip()
LUMTOK=open("/tmp/b4test/lumtok").read().strip()
JARTOK=open("/tmp/b4test/jartok").read().strip()
print("SFU:", SFU)

os.chdir("/tmp/b4test")
httpd=socketserver.TCPServer(("127.0.0.1",PORT), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
def page(tok,me): return f"http://127.0.0.1:{PORT}/call.html?me={me}&url={quote(SFU)}&token={quote(tok)}"

chrome=subprocess.Popen([os.path.expanduser("~/.local/bin/google-chrome"),
  "--headless=new",f"--remote-debugging-port={CDP}","--use-fake-device-for-media-stream",
  "--use-fake-ui-for-media-stream","--no-sandbox","--disable-gpu","--user-data-dir=/tmp/cdp-b4fed",
  "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(40):
    try: urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/version",timeout=2).read(); break
    except Exception: time.sleep(0.5)
def open_tab(url):
    r=urllib.request.Request(f"http://127.0.0.1:{CDP}/json/new?{quote(url,safe='')}",method="PUT")
    return json.loads(urllib.request.urlopen(r,timeout=10).read())
def ws_eval(ws_url,expr):
    from websocket import create_connection
    ws=create_connection(ws_url,timeout=15)
    ws.send(json.dumps({"id":1,"method":"Runtime.enable"})); ws.recv()
    ws.send(json.dumps({"id":2,"method":"Runtime.evaluate","params":{"expression":expr,"returnByValue":True,"awaitPromise":True}}))
    out=None
    for _ in range(10):
        m=json.loads(ws.recv())
        if m.get("id")==2: out=m; break
    ws.close()
    try: return out["result"]["result"]["value"]
    except Exception: return out

t1=open_tab(page(LUMTOK,"lumina")); t2=open_tab(page(JARTOK,"jarvis"))
print("joining (15s)..."); time.sleep(15)
s1=ws_eval(t1["webSocketDebuggerUrl"],"JSON.stringify(window.__lk)")
s2=ws_eval(t2["webSocketDebuggerUrl"],"JSON.stringify(window.__lk)")
print("\nLUMINA:",s1); print("JARVIS:",s2)
a=json.loads(s1) if s1 else {}; b=json.loads(s2) if s2 else {}
ok=(a.get("connected") and b.get("connected")
    and any("jarvis" in r.get("id","") for r in a.get("remotes",[]))
    and any("lumina" in r.get("id","") for r in b.get("remotes",[])))
vid=a.get("remoteVideo") and b.get("remoteVideo")
print("\nVERDICT:", "PASS — Shape-B federated call (jarvis sovereign cross-realm mint), video both ways"
      if (ok and vid) else "PARTIAL connect" if ok else "FAIL")
chrome.terminate(); httpd.shutdown()
