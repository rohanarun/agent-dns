# Example sealed agent container. Build once, then:
#   docker run --rm -it --dns <FROZEN_IP> -e FROZEN_IP=<FROZEN_IP> --cap-add NET_ADMIN frozen-agent bash
# The entrypoint joins the frozen web (CA + DNS + egress lockdown) as root, then drops to an
# unprivileged user so the agent can't undo the lockdown.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates iptables libnss3-tools util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 agent
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
