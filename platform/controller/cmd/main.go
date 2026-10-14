package main

import (
	"context"
	"encoding/json"
	"flag"
	"os"
	"time"

	"go.uber.org/zap/zapcore"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	platformv1alpha1 "platform.lab.test/controller/api/v1alpha1"
	applicationcontroller "platform.lab.test/controller/internal/controller"
	"platform.lab.test/controller/internal/telemetry"
)

var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(platformv1alpha1.AddToScheme(scheme))
	utilruntime.Must(gatewayv1.Install(scheme))
}

func main() {
	var metricsAddress string
	var healthAddress string
	flag.StringVar(&metricsAddress, "metrics-bind-address", ":8080", "Metrics bind address")
	flag.StringVar(&healthAddress, "health-probe-bind-address", ":8081", "Probe bind address")
	loggerOptions := zap.Options{Development: false, TimeEncoder: zapcore.ISO8601TimeEncoder}
	loggerOptions.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&loggerOptions)))
	logger := ctrl.Log.WithName("startup")
	registryConfigPath := os.Getenv("REGISTRY_DOCKER_CONFIG_PATH")
	if registryConfigPath == "" {
		registryConfigPath = "/var/run/secrets/platform-controller/registry/.dockerconfigjson"
	}
	registryDockerConfig, err := os.ReadFile(registryConfigPath)
	if err != nil {
		logger.Error(err, "unable to read mounted registry credentials")
		os.Exit(1)
	}
	var registryConfig struct {
		Auths map[string]json.RawMessage `json:"auths"`
	}
	if err := json.Unmarshal(registryDockerConfig, &registryConfig); err != nil || len(registryConfig.Auths) == 0 {
		logger.Error(err, "mounted registry credentials are not a valid Docker config")
		os.Exit(1)
	}
	labRootCAPath := os.Getenv("LAB_ROOT_CA_PATH")
	if labRootCAPath == "" {
		labRootCAPath = "/var/run/platform-controller/ca/lab-root-ca.crt"
	}
	labRootCA, err := os.ReadFile(labRootCAPath)
	if err != nil {
		logger.Error(err, "unable to read mounted lab root CA")
		os.Exit(1)
	}
	healthVerifier, err := applicationcontroller.NewHTTPSHealthVerifier(labRootCA)
	if err != nil {
		logger.Error(err, "unable to configure external HTTPS health verification")
		os.Exit(1)
	}

	shutdownTracing, err := telemetry.ConfigureTracing(
		context.Background(), os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
	)
	if err != nil {
		logger.Error(err, "unable to configure tracing")
		os.Exit(1)
	}
	defer func() {
		shutdownContext, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := shutdownTracing(shutdownContext); err != nil {
			logger.Error(err, "unable to flush tracing")
		}
	}()

	manager, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		Client: client.Options{Cache: &client.CacheOptions{
			DisableFor: []client.Object{&corev1.Secret{}},
		}},
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: healthAddress,
		LeaderElection:         false,
	})
	if err != nil {
		logger.Error(err, "unable to create manager")
		os.Exit(1)
	}

	if err := (&applicationcontroller.ApplicationReconciler{
		Client:               manager.GetClient(),
		Scheme:               manager.GetScheme(),
		RegistryDockerConfig: registryDockerConfig,
		HealthVerifier:       healthVerifier,
	}).SetupWithManager(manager); err != nil {
		logger.Error(err, "unable to register Application controller")
		os.Exit(1)
	}
	if err := manager.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		logger.Error(err, "unable to register liveness check")
		os.Exit(1)
	}
	if err := manager.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		logger.Error(err, "unable to register readiness check")
		os.Exit(1)
	}

	logger.Info("starting platform controller", "leaderElection", false)
	if err := manager.Start(ctrl.SetupSignalHandler()); err != nil {
		logger.Error(err, "manager stopped with an error")
		os.Exit(1)
	}
}
