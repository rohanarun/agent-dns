"""Small HTML pages the gateway generates itself (search results, misses, status)."""
from html import escape
from urllib.parse import quote_plus

CSS = """
body{font:15px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;color:#1f2328;background:#fff}
a{color:#1a0dab;text-decoration:none}a:hover{text-decoration:underline}
.bar{display:flex;gap:8px;margin:0 0 20px}.bar input{flex:1;padding:10px 14px;border:1px solid #d0d7de;border-radius:24px;font-size:16px}
.bar button{padding:10px 18px;border:0;border-radius:24px;background:#1f2328;color:#fff;font-size:15px}
.r{margin:0 0 22px}.r .u{color:#3c7a3c;font-size:13px;word-break:break-all}.r .t{font-size:18px}.r .s{color:#4d5156}
.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:10px;background:#eef1f4;color:#57606a}
code,pre{background:#f6f8fa;border-radius:6px;padding:2px 6px}pre{padding:12px;overflow:auto}
h1{font-size:26px;margin:0 0 8px}h2{font-size:18px;margin:28px 0 8px}.lead{font-size:16px}.muted{color:#57606a;font-size:13px}.ok{background:#e9f7ef;padding:8px 12px;border-radius:8px}.no{background:#fff4e5;padding:8px 12px;border-radius:8px}ol{padding-left:20px}
"""


def _doc(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'><title>{escape(title)}</title>"
            f"<style>{CSS}</style></head><body>{body}</body></html>").encode()


def _fmt_date(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else s


def search_page(q: str, results: list[dict], snapshot: str, action: str = "/search",
                page: int = 0) -> bytes:
    items = []
    for r in results:
        items.append(
            f"<div class=r><div class=u>{escape(r['url'])}</div>"
            f"<div class=t><a href='{escape(r['url'], quote=True)}'>{escape(r['title'])}</a></div>"
            f"<div class=s><span class=tag>{_fmt_date(r['captured'])}</span> {escape(r['snippet'])}</div></div>")
    if not items and q:
        items.append("<p>No results in the frozen web for this query.</p>")
    nav = ""
    if len(results) >= 10:
        nav = f"<p><a href='{action}?q={quote_plus(q)}&start={(page + 1) * 10}'>Next page →</a></p>"
    body = (f"<form class=bar action='{action}'><input name=q value='{escape(q, quote=True)}' "
            f"autofocus><button>Search</button></form>"
            f"<p class=tag>Frozen web as of {_fmt_date(snapshot)}</p>{''.join(items)}{nav}")
    return _doc(f"{q} - Search" if q else "Search", body)


def miss_page(url: str, snapshot: str, reason: str = "") -> bytes:
    body = (f"<h1>Not in the frozen web</h1><p>No archived copy of <code>{escape(url)}</code> "
            f"exists on or before {_fmt_date(snapshot)}.</p>"
            + (f"<p class=tag>{escape(reason)}</p>" if reason else ""))
    return _doc("Not found", body)


def error_page(title: str, msg: str) -> bytes:
    return _doc(title, f"<h1>{escape(title)}</h1><p>{escape(msg)}</p>")


def status_page(ip: str, snapshot: str, stats: dict, public_ip: str, allowed: bool = True,
                domain: str = "", open_access: bool = False) -> bytes:
    base = f"https://{domain}" if domain else f"http://{public_ip}"
    ip_e, base_e, dns_e = escape(ip), escape(base), escape(public_ip)
    date = _fmt_date(snapshot)
    access = ("<p class=ok>Open to everyone: no sign-up needed. Requests are rate-limited per IP.</p>"
              if open_access else
              f"<p class=ok>Your IP <code>{ip_e}</code> is on the allowlist. You're good to go.</p>"
              if allowed else
              f"<p class=no>Your IP <code>{ip_e}</code> isn't on the allowlist yet. Ask the owner to add it "
              f"before joining; the DNS server and archive only answer allowlisted machines.</p>")
    stats_html = (f"<p class=muted>{stats['cached_pages']:,} cached pages · {stats['indexed_docs']:,} searchable "
                  f"docs · {stats['requests_logged']:,} requests served</p>") if allowed else ""
    api = f"""<h2>API</h2>
<p>For agents that don't browse. Every endpoint is plain HTTPS on {base_e}.</p>
<pre>curl {base_e}/api/whoami
curl "{base_e}/api/search?q=integer+multiplication&amp;n=10"
curl "{base_e}/api/fetch?url=https://en.wikipedia.org/wiki/Integer_(computer_science)"
curl "{base_e}/api/fetch?url=https://example.com/&amp;format=raw"   # original bytes
curl "{base_e}/api/log?limit=100"                                      # your own fetches
curl -X POST {base_e}/api/snapshot -d '{{"snapshot":"20250101"}}'      # see an earlier web (no sign-up)</pre>""" if allowed else ""
    body = f"""<h1>Frozen Web</h1>
<p class=lead>A sealed copy of the internet as of <b>{date}</b>, for AI agents. Point a sandbox's DNS at
this server and every website it visits is served from the archive, with nothing newer than {date}.
Search engines answer from the frozen corpus, and nothing reaches the live internet.</p>
{access}
{stats_html}
<h2>For agents</h2>
<p>No account, key or human approval. An agent with root in its own sandbox can join by itself with the
<code>--strict</code> join command below, then call <code>/api/snapshot</code> to pin itself to any date up to {date}.</p>
<h2>Connect a Linux machine or VM</h2>
<p>Run as root, then run your agent as a <b>non-root user with no sudo</b> so it can't undo this. It trusts the
Frozen Web CA, sets DNS to <code>{dns_e}</code> and, with <code>--strict</code>, blocks every other outbound
connection, including the local network.</p>
<pre>curl -fsSL {base_e}/join.sh | sudo sh -s -- --strict</pre>
<p>Agent needs a local service too (a model server on the host, a database container)? Use
<code>--lockdown</code> instead: same, but private networks (10.x, 172.16–31.x, 192.168.x) stay reachable.
Undo either with <code>curl -fsSL {base_e}/join.sh | sudo sh -s -- --leave</code>.</p>
<h2>Connect a Docker container</h2>
<pre>docker run --rm -it --cap-add NET_ADMIN ubuntu:24.04 bash -c \
  "apt-get update -qq &amp;&amp; apt-get install -y -qq curl iptables &gt;/dev/null &amp;&amp; \
   curl -fsSL {base_e}/join.sh | sh -s -- --strict &amp;&amp; exec bash"</pre>
{api}
<h2>Set it up by hand</h2>
<ol>
<li>Set the machine's DNS server to <code>{dns_e}</code>.</li>
<li>Download <a href="/ca.pem">the Frozen Web CA</a> and trust it in the system store (and the browser's, for Chrome on Linux).</li>
<li>Block outbound traffic to everything except <code>{dns_e}</code>; otherwise hard-coded IPs and DNS-over-HTTPS bypass the archive.</li>
</ol>
<p class=muted>Only install the CA inside sandboxes, never on a machine you use day to day: it lets this server
issue certificates for any site.</p>

<h2>Check it</h2>
<pre>curl https://frozen.internal/api/whoami          # works only once joined
curl -sI https://en.wikipedia.org/ | grep -i x-frozen   # X-Frozen-Source: commoncrawl | wayback</pre>"""
    body += ('<p class=muted style="margin-top:32px">Open source: '
             '<a href="https://github.com/rohanarun/agent-dns">github.com/rohanarun/agent-dns</a> (MIT). '
             'Run your own on one VM.</p>')
    return _doc("Frozen Web", body)
