"""End-to-end: real mitmdump + addon (fake archive) + real DNS server; real curl with real TLS
verification against the gateway's CA, hostnames resolved the way the agent would."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
GW = HERE.parent
HTTPS, HTTP, DNS = 18443, 18080, 15353


def _wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(port)


@pytest.fixture(scope="module")
def gw(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    env = {**os.environ, "FROZEN_DATA_DIR": str(data), "FROZEN_PUBLIC_IP": "127.0.0.1",
           "FROZEN_ADMIN_TOKEN": "secret", "FROZEN_SNAPSHOT": "20250601",
           "FROZEN_DNS_PORT": str(DNS), "FROZEN_DNS_BIND": "127.0.0.1", "PYTHONPATH": str(GW)}
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env.pop(k, None)
    mitm = subprocess.Popen(
        ["mitmdump", "--set", f"confdir={data}/ca",
         "--mode", f"reverse:http://127.0.0.1:9@{HTTPS}", "--mode", f"reverse:http://127.0.0.1:9@{HTTP}",
         "--set", "connection_strategy=lazy", "--set", "keep_host_header=true",
         "--set", "block_global=false", "--set", "upstream_cert=false",
         "-s", str(HERE / "fake_addon.py"), "-q"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    dns = subprocess.Popen([sys.executable, "-m", "frozen.dns_server"], env=env, cwd=GW,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_port(HTTPS)
        _wait_port(HTTP)
        time.sleep(1)
        yield {"ca": data / "ca" / "mitmproxy-ca-cert.pem", "data": data}
    finally:
        mitm.terminate()
        dns.terminate()
        out = mitm.communicate(timeout=10)[0].decode(errors="replace")
        if out.strip():
            print("---- mitmdump ----\n" + out[-4000:])


def curl(gw, url, *extra, host=None):
    """curl as an agent would: hostname resolves to the gateway, TLS verified against our CA."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    port = HTTPS if u.scheme == "https" else HTTP
    host = host or u.hostname
    target = url.replace(f"{u.scheme}://{u.hostname}", f"{u.scheme}://{host}:{port}", 1)
    cmd = ["curl", "-sS", "--noproxy", "*", "--resolve", f"{host}:{port}:127.0.0.1",
           "--cacert", str(gw["ca"]), "-D", "-", target, *extra]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode()
    head, _, body = r.stdout.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    headers = {}
    for line in head.decode().split("\r\n")[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


def test_dns_answers_everything_with_gateway_ip(gw):
    from dnslib import DNSRecord
    for name in ("google.com", "arxiv.org", "some.random.host.xyz"):
        q = DNSRecord.question(name, "A")
        ans = DNSRecord.parse(q.send("127.0.0.1", DNS, timeout=3))
        assert [str(r.rdata) for r in ans.rr] == ["127.0.0.1"]
    q = DNSRecord.question("google.com", "AAAA")
    assert DNSRecord.parse(q.send("127.0.0.1", DNS, timeout=3)).rr == []
    q = DNSRecord.question("google.com", "A")
    assert DNSRecord.parse(q.send("127.0.0.1", DNS, tcp=True, timeout=3)).rr[0].rdata.__str__() == "127.0.0.1"


def test_https_archived_page_with_valid_minted_cert(gw):
    status, h, body = curl(gw, "https://example.com/")
    assert status == 200 and b"archived example page" in body
    assert h["x-frozen-source"] == "fake" and h["x-frozen-captured"] == "20250301000000"
    assert "content-encoding" not in h  # body was stored decoded
    status2, _, body2 = curl(gw, "https://example.com/")  # second hit comes from the pinned cache
    assert status2 == 200 and body2 == body


def test_cert_is_for_requested_host(gw):
    r = subprocess.run(["openssl", "s_client", "-connect", f"127.0.0.1:{HTTPS}", "-servername", "arxiv.org"],
                       input=b"", capture_output=True, timeout=15)
    cert = subprocess.run(["openssl", "x509", "-noout", "-ext", "subjectAltName"], input=r.stdout,
                          capture_output=True).stdout.decode()
    assert "arxiv.org" in cert


def test_future_content_never_served(gw):
    status, h, body = curl(gw, "https://example.com/new")
    assert status == 404 and b"Not in the frozen web" in body


def test_redirect_preserved(gw):
    status, h, _ = curl(gw, "http://old.example.org/")
    assert status == 301 and h["location"] == "https://example.com/"


def test_writes_blocked(gw):
    status, _, _ = curl(gw, "https://example.com/", "-X", "POST", "-d", "a=1")
    assert status == 405


def test_google_search_answered_from_index(gw):
    curl(gw, "https://arxiv.org/abs/1")  # visiting a page indexes it
    status, _, body = curl(gw, "https://www.google.com/search?q=integer+multiplication")
    assert status == 200 and b"https://arxiv.org/abs/1" in body
    status, _, body = curl(gw, "https://duckduckgo.com/?q=kappa")
    assert b"arxiv.org/abs/1" in body


def test_control_api(gw):
    status, _, body = curl(gw, "https://frozen.internal/api/whoami")
    assert json.loads(body)["snapshot"] == "20250601"
    status, _, body = curl(gw, "http://127.0.0.1/ca.pem", host="127.0.0.1")
    assert b"BEGIN CERTIFICATE" in body
    status, _, body = curl(gw, "http://127.0.0.1/join.sh", host="127.0.0.1")
    assert b'FROZEN_IP="${FROZEN_IP:-127.0.0.1}"' in body
    status, _, body = curl(gw, "https://frozen.internal/api/fetch?url=https://example.com/")
    j = json.loads(body)
    assert j["found"] and j["title"] == "Example Domain"
    status, _, body = curl(gw, "https://frozen.internal/api/search?q=kappa")
    assert len(json.loads(body)["results"]) >= 1
    status, _, body = curl(gw, "https://frozen.internal/api/log?limit=5")
    assert all(r["client"] == "127.0.0.1" for r in json.loads(body))


def test_agent_cannot_pick_newer_snapshot(gw):
    status, _, body = curl(gw, "https://frozen.internal/api/fetch?url=https://example.com/new&snapshot=20270101")
    assert status == 404


def test_admin_sets_per_client_snapshot(gw):
    status, _, body = curl(gw, "https://frozen.internal/admin/client", "-X", "POST",
                           "-d", '{"ip":"127.0.0.1","snapshot":"20270101","label":"t"}')
    assert status == 401
    status, _, body = curl(gw, "https://frozen.internal/admin/client", "-X", "POST",
                           "-H", "Authorization: Bearer secret",
                           "-d", '{"ip":"127.0.0.1","snapshot":"20270101","label":"t"}')
    assert status == 200
    status, h, body = curl(gw, "https://example.com/new")
    assert status == 200 and b"future" in body and h["x-frozen-snapshot"] == "20270101"
    curl(gw, "https://frozen.internal/admin/client", "-X", "DELETE", "-H", "Authorization: Bearer secret",
         "-d", '{"ip":"127.0.0.1"}')
    status, _, _ = curl(gw, "https://example.com/new")
    assert status == 404


def test_self_service_snapshot(gw):
    status, _, body = curl(gw, "https://frozen.internal/api/snapshot", "-X", "POST", "-d", '{"snapshot":"20270101"}')
    assert status == 400  # can't move itself past the server's cutoff
    status, _, body = curl(gw, "https://frozen.internal/api/snapshot", "-X", "POST", "-d", '{"snapshot":"20250101"}')
    assert status == 200
    assert json.loads(curl(gw, "https://frozen.internal/api/whoami")[2])["snapshot"] == "20250101"
    status, _, _ = curl(gw, "https://example.com/")  # captured 2025-03-01, now after this client's date
    assert status == 404
    curl(gw, "https://frozen.internal/api/snapshot", "-X", "POST", "-d", '{"snapshot":"default"}')
    assert json.loads(curl(gw, "https://frozen.internal/api/whoami")[2])["snapshot"] == "20250601"
