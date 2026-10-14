FROM docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
LABEL org.opencontainers.image.title="lap-build-agent-unhealthy-fixture"
RUN mkdir -p /www/cgi-bin \
    && printf '%s\n' '#!/bin/sh' \
      'case "$HTTP_USER_AGENT" in' \
      '  kube-probe/*) printf "Content-Type: text/plain\\r\\n\\r\\nready\\n" ;;' \
      '  *) printf "Status: 503 Service Unavailable\\r\\nContent-Type: text/plain\\r\\n\\r\\nunhealthy\\n" ;;' \
      'esac' >/www/cgi-bin/health \
    && chmod 0555 /www/cgi-bin/health
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["httpd", "-f", "-p", "8080", "-h", "/www"]
