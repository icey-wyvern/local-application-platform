FROM docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
LABEL org.opencontainers.image.title="lap-build-agent-success-v2-fixture"
RUN mkdir /www && printf 'ok\n' >/www/health && chmod -R a=rX /www
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["httpd", "-f", "-p", "8080", "-h", "/www"]
