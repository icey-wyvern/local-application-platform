package controller

import (
	"context"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPSHealthVerifierUsesNormalCertificateVerification(t *testing.T) {
	calls := 0
	server := httptest.NewTLSServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		calls++
		if calls == 1 {
			response.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	rootPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})
	verifier, err := NewHTTPSHealthVerifier(rootPEM)
	if err != nil {
		t.Fatalf("create verifier: %v", err)
	}
	if err := verifier.Verify(context.Background(), server.URL); err != nil {
		t.Fatalf("verify trusted HTTPS endpoint: %v", err)
	}
	if calls != healthCheckAttempts {
		t.Fatalf("expected bounded retry to make %d requests, got %d", healthCheckAttempts, calls)
	}

	wrongHostname := strings.Replace(server.URL, "127.0.0.1", "localhost", 1)
	if err := verifier.Verify(context.Background(), wrongHostname); err == nil {
		t.Fatal("HTTPS endpoint with a mismatched hostname was accepted")
	}
}
