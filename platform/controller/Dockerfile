FROM golang:1.26.7-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514 AS build

WORKDIR /src
COPY platform/controller/go.mod platform/controller/go.sum ./
RUN go mod download
COPY platform/controller/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/platform-controller ./cmd

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/platform-controller /platform-controller
USER 65532:65532
ENTRYPOINT ["/platform-controller"]
