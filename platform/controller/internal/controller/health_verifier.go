package controller

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

const (
	healthRequestTimeout = 3 * time.Second
	healthCheckAttempts  = 2
	healthRetryDelay     = 250 * time.Millisecond
)

// HealthVerifier verifies externally routed application health.
type HealthVerifier interface {
	Verify(context.Context, string) error
}

// HTTPSHealthVerifier performs bounded HTTPS checks with normal hostname and CA verification.
type HTTPSHealthVerifier struct {
	client *http.Client
}

// NewHTTPSHealthVerifier constructs a verifier that trusts only the supplied lab CA roots.
func NewHTTPSHealthVerifier(rootPEM []byte) (*HTTPSHealthVerifier, error) {
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(rootPEM) {
		return nil, errors.New("lab root CA file does not contain a certificate")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{
		MinVersion: tls.VersionTLS12,
		RootCAs:    roots,
	}
	return &HTTPSHealthVerifier{client: &http.Client{
		Transport: transport,
		Timeout:   healthRequestTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return errors.New("health endpoint redirects are not followed")
		},
	}}, nil
}

// Verify requires a successful HTTPS response and never disables TLS verification.
func (v *HTTPSHealthVerifier) Verify(ctx context.Context, endpoint string) error {
	var lastError error
	for attempt := 1; attempt <= healthCheckAttempts; attempt++ {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return fmt.Errorf("create health request: %w", err)
		}
		request.Header.Set("User-Agent", "platform-controller-health/1.0")
		response, err := v.client.Do(request)
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
			_ = response.Body.Close()
			if response.StatusCode >= http.StatusOK && response.StatusCode < http.StatusMultipleChoices {
				return nil
			}
			err = fmt.Errorf("health endpoint returned HTTP %d", response.StatusCode)
		}
		lastError = err
		if attempt < healthCheckAttempts {
			timer := time.NewTimer(healthRetryDelay)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
		}
	}
	return fmt.Errorf("external HTTPS health verification failed after %d attempts: %w", healthCheckAttempts, lastError)
}
