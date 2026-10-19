"""Public-domain mode: a real cert is served for the site domain (others still get minted
certs), plain HTTP redirects to HTTPS, ACME challenges are served, and the allowlist holds."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
GW = HERE.parent
HTTPS, HTTP = 28443, 28080
DOMAIN = "tensor-book.com"
TOKEN = "abcDEF123_-abcDEF123_-tok"


def _wait(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(port)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    # Stand-in for the Let's Encrypt cert: a self-signed cert for the domain + www.
    (data / "tls").mkdir()
    key, crt = data / "k.pem", data / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", f"/CN={DOMAIN}", "-addext", f"subjectAltName=DNS:{DOMAIN},DNS:www.{DOMAIN}",
                    "-keyout", key, "-out", crt], check=True, capture_output=True)
    (data / "tls" / "site.pem").write_bytes(key.read_bytes() + crt.read_bytes())
    acme = data / "acme" / ".well-known" / "acme-challenge"
    acme.mkdir(parents=True)
    (acme / TOKEN).write_text("challenge-response")
    env = {**os.environ, "FROZEN_DATA_DIR": str(data), "FROZEN_PUBLIC_IP": "127.0.0.1",
           "FROZEN_SITE_DOMAINS": f"{DOMAIN},www.{DOMAIN}", "FROZEN_ALLOW": "10.9.9.9/32",
           "PYTHONPATH": str(GW)}
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env.pop(k, None)
    cert = data / "tls" / "site.pem"
    p = subprocess.Popen(
        ["mitmdump", "--set", f"confdir={data}/ca",
         "--mode", f"reverse:http://127.0.0.1:9@{HTTPS}", "--mode", f"reverse:http://127.0.0.1:9@{HTTP}",
         "--set", "connection_strategy=lazy", "--set", "keep_host_header=true",
         "--set", "block_global=false", "--set", "upstream_cert=false",
         "--certs", f"{DOMAIN}={cert}", "--certs", f"www.{DOMAIN}={cert}",
         "-s", str(HERE / "fake_addon.py"), "-q"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait(HTTPS); _wait(HTTP); time.sleep(1)
        yield {"site_ca": crt, "frozen_ca": data / "ca" / "mitmproxy-ca-cert.pem"}
    finally:
        p.terminate()
        out = p.communicate(timeout=10)[0].decode(errors="replace")
        if out.strip():
            print(out[-3000:])


def curl(host, port, path, ca=None, scheme="https"):
    cmd = ["curl", "-sS", "--noproxy", "*", "--resolve", f"{host}:{port}:127.0.0.1", "-D", "-",
           f"{scheme}://{host}:{port}{path}"]
    if ca:
        cmd[2:2] = ["--cacert", str(ca)]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    return r.returncode, r.stdout, r.stderr.decode()


def test_site_domain_gets_real_cert(site):
    for host in (DOMAIN, f"www.{DOMAIN}"):
        rc, out, err = curl(host, HTTPS, "/healthz", site["site_ca"])
        assert rc == 0 and out.endswith(b"ok"), err
    # and NOT the frozen CA's minted cert
    rc, _, _ = curl(DOMAIN, HTTPS, "/healthz", site["frozen_ca"])
    assert rc != 0


def test_other_hosts_still_minted_by_frozen_ca(site):
    rc, out, err = curl("example.com", HTTPS, "/", site["frozen_ca"])
    assert rc == 0, err


def test_http_redirects_to_https(site):
    rc, out, _ = curl(DOMAIN, HTTP, "/some/page?x=1", scheme="http")
    assert b" 301" in out.split(b"\r\n")[0] and f"location: https://{DOMAIN}/some/page?x=1".encode() in out.lower()


def test_acme_challenge_served_over_http(site):
    rc, out, _ = curl(DOMAIN, HTTP, f"/.well-known/acme-challenge/{TOKEN}", scheme="http")
    assert out.endswith(b"challenge-response")
    rc, out, _ = curl(DOMAIN, HTTP, "/.well-known/acme-challenge/..%2f..%2fetc", scheme="http")
    assert b" 404" in out.split(b"\r\n")[0]


def test_allowlist_logic(monkeypatch):
    sys.path.insert(0, str(GW))
    import ipaddress
    from frozen import core
    monkeypatch.setattr(core, "_ALLOW_NETS", [ipaddress.ip_network("10.9.9.0/24")])
    assert core.ip_allowed("10.9.9.4") and core.ip_allowed("127.0.0.1")
    assert not core.ip_allowed("203.0.113.5") and not core.ip_allowed("garbage")
    monkeypatch.setattr(core, "_ALLOW_NETS", [])
    assert core.ip_allowed("203.0.113.5")


def test_landing_page_and_join_use_https_domain(site):
    rc, out, err = curl(DOMAIN, HTTPS, "/", site["site_ca"])
    assert rc == 0, err
    assert f"curl -fsSL https://{DOMAIN}/join.sh | sudo sh -s -- --strict".encode() in out
    assert b"github.com/rohanarun/agent-dns" in out
    assert b"34.150.228.83" not in out and b"127.0.0.1</code>" in out  # DNS IP shown, from config
    rc, out, _ = curl(DOMAIN, HTTPS, "/join.sh", site["site_ca"])
    assert f'FROZEN_DOMAIN="${{FROZEN_DOMAIN:-{DOMAIN}}}"'.encode() in out


def test_selftest_script_served(site):
    rc, out, _ = curl(DOMAIN, HTTPS, "/selftest.sh", site["site_ca"])
    assert rc == 0 and f'BASE="${{FROZEN_BASE:-https://{DOMAIN}}}"'.encode() in out
    assert b"__FROZEN_DOMAIN__" not in out
