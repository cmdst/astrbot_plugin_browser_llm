import json, os, urllib.request

body = open("/tmp/pt.txt").read()[-6000:]
data = json.dumps({
    "title": "[CI DEBUG] pytest output",
    "body": "```\n" + body + "\n```",
}).encode()
req = urllib.request.Request(
    "https://api.github.com/repos/cmdst/astrbot_plugin_browser_llm/issues",
    data=data, method="POST",
    headers={
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "ci-debug",
    },
)
try:
    r = urllib.request.urlopen(req, timeout=30)
    print("issue created:", json.loads(r.read())["html_url"])
except Exception as e:
    print("issue create failed:", e)
