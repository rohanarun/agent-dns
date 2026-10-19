"""Runs the real join.sh inside a network namespace (needs root + iproute2 + iptables) and checks
what a non-root agent can reach under --lockdown and --strict."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

JOIN = Path(__file__).resolve().parent.parent / "join.sh"
pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("ip") or not shutil.which("iptables"),
                                reason="needs root, iproute2 and iptables")
SH = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def bed(tmp_path_factory):
    t = tmp_path_factory.mktemp("bed")
    (t / "www").mkdir()
    (t / "www" / "ping").write_text("ok")
    SH(f"openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=t -keyout {t}/k -out {t}/www/ca.pem")
    SH("ip netns del fwtest; ip link del fwh")
    for c in ["ip netns add fwtest", "ip link add fwh type veth peer name fwa", "ip link set fwa netns fwtest",
              "ip addr add 10.98.0.1/24 dev fwh", "ip addr add 10.98.0.3/24 dev fwh",
              "ip addr add 198.51.100.9/32 dev fwh", "ip link set fwh up",
              "ip netns exec fwtest ip addr add 10.98.0.2/24 dev fwa", "ip netns exec fwtest ip link set fwa up",
              "ip netns exec fwtest ip link set lo up", "ip netns exec fwtest ip route add default via 10.98.0.1"]:
        assert SH(c).returncode == 0, c
    servers = [subprocess.Popen(["python3", "-m", "http.server", "80", "--bind", ip], cwd=t / "www",
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for ip in ("10.98.0.1", "10.98.0.3", "198.51.100.9")]
    SH("sleep 1")
    yield t
    for s in servers:
        s.terminate()
    SH("ip netns del fwtest; ip link del fwh")


def run(bed, flag):
    etc = bed / f"etc{flag}"
    shutil.rmtree(etc, ignore_errors=True)
    SH(f"cp -a /etc {etc}; mkdir -p {bed}/uls")
    probe = lambda target: (f"ip netns exec fwtest setpriv --reuid=65534 --regid=65534 --clear-groups "
                            f"curl -s -m 3 --noproxy '*' http://{target}/ping >/dev/null && echo {target}=open || echo {target}=closed")
    script = f"""
mount --bind {etc} /etc; mount --bind {bed}/uls /usr/local/share
ip netns exec fwtest env FROZEN_DOMAIN=__none timeout 60 sh {JOIN} 10.98.0.1 {flag} >/dev/null 2>&1; echo join=$?
{probe('10.98.0.1')}; {probe('10.98.0.3')}; {probe('198.51.100.9')}
ip netns exec fwtest setpriv --reuid=65534 --regid=65534 --clear-groups iptables -F FROZEN >/dev/null 2>&1 && echo undo=yes || echo undo=no
grep -q 'nameserver 10.98.0.1' /etc/resolv.conf && echo dns=frozen
ip netns exec fwtest sh {JOIN} --leave >/dev/null 2>&1
ip netns exec fwtest iptables -S OUTPUT | grep -q FROZEN && echo left=no || echo left=yes
"""
    out = subprocess.run(["unshare", "-m", "sh", "-c", script], capture_output=True, text=True, timeout=120).stdout
    return dict(line.split("=", 1) for line in out.split() if "=" in line)


def test_lockdown_keeps_local_network(bed):
    r = run(bed, "--lockdown")
    assert r == {"join": "0", "10.98.0.1": "open", "10.98.0.3": "open", "198.51.100.9": "closed",
                 "undo": "no", "dns": "frozen", "left": "yes"}


def test_strict_blocks_local_network(bed):
    r = run(bed, "--strict")
    assert r == {"join": "0", "10.98.0.1": "open", "10.98.0.3": "closed", "198.51.100.9": "closed",
                 "undo": "no", "dns": "frozen", "left": "yes"}
