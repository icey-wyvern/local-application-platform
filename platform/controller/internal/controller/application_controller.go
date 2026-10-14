package controller

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/go-logr/logr"
	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	crmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	platformv1alpha1 "platform.lab.test/controller/api/v1alpha1"
)

const (
	reconciledCondition   = "Reconciled"
	runtimeReadyCondition = "RuntimeReady"
	routeReadyCondition   = "RouteReady"
	healthyCondition      = "Healthy"
	availableCondition    = "Available"
	healthCheckInterval   = 30 * time.Second
)

var (
	imagePattern = regexp.MustCompile(`^.+@sha256:[0-9a-f]{64}$`)
	namePattern  = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$`)

	reconcileTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "lap_platform_controller_reconciliations_total",
			Help: "Application reconciliation outcomes.",
		},
		[]string{"result"},
	)
	healthCheckTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "lap_platform_controller_health_checks_total",
			Help: "External HTTPS health verification outcomes.",
		},
		[]string{"result"},
	)
	healthCheckDuration = prometheus.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "lap_platform_controller_health_check_duration_seconds",
			Help:    "External HTTPS health verification duration.",
			Buckets: prometheus.DefBuckets,
		},
	)
)

func init() {
	crmetrics.Registry.MustRegister(reconcileTotal, healthCheckTotal, healthCheckDuration)
}

// ApplicationReconciler validates Application intent and owns status/finalizer lifecycle.
type ApplicationReconciler struct {
	client.Client
	Scheme               *runtime.Scheme
	RegistryDockerConfig []byte
	HealthVerifier       HealthVerifier
	Now                  func() metav1.Time
}

// SetupWithManager registers the Application watch.
func (r *ApplicationReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&platformv1alpha1.Application{}).
		Owns(&corev1.Namespace{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.ResourceQuota{}).
		Owns(&corev1.LimitRange{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&networkingv1.NetworkPolicy{}).
		Owns(&gatewayv1.HTTPRoute{}).
		Complete(r)
}

// +kubebuilder:rbac:groups=platform.lab.test,resources=applications,verbs=get;list;watch
// +kubebuilder:rbac:groups=platform.lab.test,resources=applications/finalizers,verbs=update;patch
// +kubebuilder:rbac:groups=platform.lab.test,resources=applications/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts;resourcequotas;limitranges;services,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups="",resources=secrets,verbs=create
// +kubebuilder:rbac:groups="",resources=secrets,resourceNames=registry-pull,verbs=get;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=gateway.networking.k8s.io,resources=httproutes,verbs=get;list;watch;create;update;patch

func (r *ApplicationReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	logger := ctrl.LoggerFrom(ctx).WithValues("application", request.Name)
	ctx, span := otel.Tracer("platform-controller").Start(ctx, "application.reconcile")
	defer span.End()
	span.SetAttributes(attribute.String("platform.application.name", request.Name))

	application := &platformv1alpha1.Application{}
	if err := r.Get(ctx, request.NamespacedName, application); err != nil {
		if apierrors.IsNotFound(err) {
			reconcileTotal.WithLabelValues("not_found").Inc()
			return ctrl.Result{}, nil
		}
		reconcileTotal.WithLabelValues("error").Inc()
		span.RecordError(err)
		span.SetStatus(codes.Error, "get failed")
		return ctrl.Result{}, fmt.Errorf("get Application: %w", err)
	}

	if !application.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, logger, application)
	}

	if err := validateApplication(application); err != nil {
		status := application.Status.DeepCopy()
		status.ObservedGeneration = application.Generation
		status.DesiredImage = application.Spec.Image
		status.Phase = platformv1alpha1.ApplicationPhaseError
		status.Message = err.Error()
		meta.SetStatusCondition(&status.Conditions, metav1.Condition{
			Type:               reconciledCondition,
			Status:             metav1.ConditionFalse,
			ObservedGeneration: application.Generation,
			Reason:             "InvalidSpec",
			Message:            err.Error(),
		})
		if patchErr := r.patchStatus(ctx, application, *status); patchErr != nil {
			reconcileTotal.WithLabelValues("error").Inc()
			return ctrl.Result{}, patchErr
		}
		reconcileTotal.WithLabelValues("invalid").Inc()
		logger.Info("application desired state is invalid", "error", err.Error())
		return ctrl.Result{}, nil
	}

	if !controllerutil.ContainsFinalizer(application, platformv1alpha1.ApplicationFinalizer) {
		before := application.DeepCopy()
		controllerutil.AddFinalizer(application, platformv1alpha1.ApplicationFinalizer)
		if err := r.Patch(ctx, application, client.MergeFrom(before)); err != nil {
			reconcileTotal.WithLabelValues("error").Inc()
			return ctrl.Result{}, fmt.Errorf("add Application finalizer: %w", err)
		}
		reconcileTotal.WithLabelValues("finalizer_added").Inc()
		logger.Info("application finalizer added")
		return ctrl.Result{Requeue: true}, nil
	}

	resources, err := r.reconcileRuntimeResources(ctx, application)
	if err != nil {
		reconcileTotal.WithLabelValues("error").Inc()
		span.RecordError(err)
		span.SetStatus(codes.Error, "runtime resource reconciliation failed")
		return ctrl.Result{}, err
	}

	status := application.Status.DeepCopy()
	status.ObservedGeneration = application.Generation
	status.DesiredImage = application.Spec.Image
	status.ReadyReplicas = resources.Deployment.Status.ReadyReplicas
	status.Hostname = application.Name + ".apps.lab.test"
	runtimeReady := deploymentMatchesDesired(resources.Deployment, application) &&
		serviceMatchesDesired(resources.Service, application)
	if runtimeReady {
		status.ActiveReleaseID = application.Spec.ReleaseID
		status.CurrentImage = application.Spec.Image
		meta.SetStatusCondition(&status.Conditions, metav1.Condition{
			Type:               runtimeReadyCondition,
			Status:             metav1.ConditionTrue,
			ObservedGeneration: application.Generation,
			Reason:             "WorkloadAndServiceReady",
			Message:            "desired image has two ready replicas and the Service targets them",
		})
	} else {
		meta.SetStatusCondition(&status.Conditions, metav1.Condition{
			Type:               runtimeReadyCondition,
			Status:             metav1.ConditionFalse,
			ObservedGeneration: application.Generation,
			Reason:             "WorkloadProgressing",
			Message:            "waiting for the desired image, two ready replicas, and matching Service",
		})
	}

	route := assessHTTPRoute(resources.Route)
	routeStatus := metav1.ConditionFalse
	if route.ready {
		routeStatus = metav1.ConditionTrue
	}
	meta.SetStatusCondition(&status.Conditions, metav1.Condition{
		Type:               routeReadyCondition,
		Status:             routeStatus,
		ObservedGeneration: application.Generation,
		Reason:             route.reason,
		Message:            route.message,
	})

	healthy := false
	healthChecked := false
	healthMessage := "waiting for workload, Service, and HTTPRoute readiness"
	if runtimeReady && route.ready {
		healthy, healthMessage, healthChecked = r.externalHealth(ctx, application, resources.Route, status)
	}
	if healthChecked {
		healthStatus := metav1.ConditionFalse
		healthReason := "ExternalHealthCheckFailed"
		if healthy {
			healthStatus = metav1.ConditionTrue
			healthReason = "ExternalHealthCheckSucceeded"
		}
		meta.SetStatusCondition(&status.Conditions, metav1.Condition{
			Type:               healthyCondition,
			Status:             healthStatus,
			ObservedGeneration: application.Generation,
			Reason:             healthReason,
			Message:            healthMessage,
		})
	} else {
		meta.RemoveStatusCondition(&status.Conditions, healthyCondition)
	}

	available := runtimeReady && route.ready && healthy
	availableStatus := metav1.ConditionFalse
	availableReason := "ApplicationProgressing"
	availableMessage := healthMessage
	status.Phase = platformv1alpha1.ApplicationPhasePending
	status.Message = availableMessage
	if route.failed {
		availableReason = route.reason
		availableMessage = route.message
		status.Phase = platformv1alpha1.ApplicationPhaseError
		status.Message = availableMessage
	} else if healthChecked && !healthy {
		availableReason = "ExternalHealthCheckFailed"
		status.Phase = platformv1alpha1.ApplicationPhaseError
		status.Message = healthMessage
	} else if available {
		availableStatus = metav1.ConditionTrue
		availableReason = "ApplicationHealthy"
		availableMessage = "runtime, shared Gateway route, and external HTTPS health are ready"
		status.Phase = platformv1alpha1.ApplicationPhaseReady
		status.Message = availableMessage
	}
	meta.SetStatusCondition(&status.Conditions, metav1.Condition{
		Type:               availableCondition,
		Status:             availableStatus,
		ObservedGeneration: application.Generation,
		Reason:             availableReason,
		Message:            availableMessage,
	})
	meta.SetStatusCondition(&status.Conditions, metav1.Condition{
		Type:               reconciledCondition,
		Status:             metav1.ConditionTrue,
		ObservedGeneration: application.Generation,
		Reason:             "RuntimeResourcesReconciled",
		Message:            "owned runtime resources match desired state",
	})
	if err := r.patchStatus(ctx, application, *status); err != nil {
		reconcileTotal.WithLabelValues("error").Inc()
		return ctrl.Result{}, err
	}
	result := "progressing"
	if available {
		result = "ready"
	} else if route.failed || healthChecked {
		result = "failed"
	}
	reconcileTotal.WithLabelValues(result).Inc()
	logger.Info("application runtime resources reconciled", "generation", application.Generation, "phase", status.Phase)
	if !available {
		return ctrl.Result{RequeueAfter: 10 * time.Second}, nil
	}
	return ctrl.Result{RequeueAfter: healthCheckInterval}, nil
}

func (r *ApplicationReconciler) externalHealth(
	ctx context.Context,
	application *platformv1alpha1.Application,
	route *gatewayv1.HTTPRoute,
	status *platformv1alpha1.ApplicationStatus,
) (bool, string, bool) {
	now := r.now()
	condition := meta.FindStatusCondition(status.Conditions, healthyCondition)
	if condition != nil && status.LastHealthCheckTime != nil &&
		status.ObservedRouteGeneration == route.Generation &&
		condition.ObservedGeneration == application.Generation {
		age := now.Sub(status.LastHealthCheckTime.Time)
		if age >= 0 && age < healthCheckInterval {
			return condition.Status == metav1.ConditionTrue, condition.Message, true
		}
	}
	status.ObservedRouteGeneration = route.Generation
	status.LastHealthCheckTime = &now
	if r.HealthVerifier == nil {
		return false, "external HTTPS health verifier is not configured", true
	}
	endpoint := "https://" + application.Name + ".apps.lab.test" + application.Spec.HealthPath
	started := time.Now()
	err := r.HealthVerifier.Verify(ctx, endpoint)
	healthCheckDuration.Observe(time.Since(started).Seconds())
	if err != nil {
		healthCheckTotal.WithLabelValues("failed").Inc()
		return false, err.Error(), true
	}
	healthCheckTotal.WithLabelValues("succeeded").Inc()
	return true, "external HTTPS health endpoint returned a successful response", true
}

func (r *ApplicationReconciler) reconcileDelete(
	ctx context.Context,
	logger logr.Logger,
	application *platformv1alpha1.Application,
) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(application, platformv1alpha1.ApplicationFinalizer) {
		reconcileTotal.WithLabelValues("deleted").Inc()
		return ctrl.Result{}, nil
	}

	deleted, err := r.deleteRuntimeNamespace(ctx, application)
	if err != nil {
		reconcileTotal.WithLabelValues("error").Inc()
		return ctrl.Result{}, err
	}

	status := application.Status.DeepCopy()
	status.ObservedGeneration = application.Generation
	status.Phase = platformv1alpha1.ApplicationPhaseDeleting
	status.Message = "deleting owned runtime namespace and resources"
	meta.SetStatusCondition(&status.Conditions, metav1.Condition{
		Type:               "Cleanup",
		Status:             metav1.ConditionFalse,
		ObservedGeneration: application.Generation,
		Reason:             "NamespaceDeletionPending",
		Message:            status.Message,
	})
	if err := r.patchStatus(ctx, application, *status); err != nil && !apierrors.IsNotFound(err) {
		reconcileTotal.WithLabelValues("error").Inc()
		return ctrl.Result{}, err
	}
	if !deleted {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}

	before := application.DeepCopy()
	controllerutil.RemoveFinalizer(application, platformv1alpha1.ApplicationFinalizer)
	if err := r.Patch(ctx, application, client.MergeFrom(before)); err != nil && !apierrors.IsNotFound(err) {
		reconcileTotal.WithLabelValues("error").Inc()
		return ctrl.Result{}, fmt.Errorf("remove Application finalizer: %w", err)
	}
	reconcileTotal.WithLabelValues("deleted").Inc()
	logger.Info("application finalizer removed")
	return ctrl.Result{}, nil
}

func (r *ApplicationReconciler) patchStatus(
	ctx context.Context,
	application *platformv1alpha1.Application,
	desired platformv1alpha1.ApplicationStatus,
) error {
	before := application.DeepCopy()
	desired.LastTransitionTime = application.Status.LastTransitionTime
	if equality.Semantic.DeepEqual(application.Status, desired) {
		return nil
	}
	desired.LastTransitionTime = r.now()
	application.Status = desired
	if err := r.Status().Patch(ctx, application, client.MergeFrom(before)); err != nil {
		return fmt.Errorf("update Application status: %w", err)
	}
	return nil
}

func (r *ApplicationReconciler) now() metav1.Time {
	if r.Now != nil {
		return r.Now()
	}
	return metav1.Now()
}

func validateSpec(spec platformv1alpha1.ApplicationSpec) error {
	var validationErrors []error
	if _, err := uuid.Parse(spec.ReleaseID); err != nil {
		validationErrors = append(validationErrors, errors.New("releaseId must be a UUID"))
	}
	if !imagePattern.MatchString(spec.Image) {
		validationErrors = append(validationErrors, errors.New("image must contain an immutable sha256 digest"))
	}
	if spec.Port < 1024 || spec.Port > 65535 {
		validationErrors = append(validationErrors, errors.New("port must be between 1024 and 65535"))
	}
	if len(spec.HealthPath) < 1 || len(spec.HealthPath) > 256 ||
		!strings.HasPrefix(spec.HealthPath, "/") || strings.HasPrefix(spec.HealthPath, "//") ||
		strings.ContainsAny(spec.HealthPath, "?#") {
		validationErrors = append(validationErrors, errors.New("healthPath must be an absolute path without query or fragment"))
	}
	if spec.Replicas != 2 {
		validationErrors = append(validationErrors, errors.New("replicas must equal 2"))
	}
	return errors.Join(validationErrors...)
}

func validateApplication(application *platformv1alpha1.Application) error {
	if !namePattern.MatchString(application.Name) {
		return errors.New("metadata.name must be the lowercase DNS-safe application name")
	}
	return validateSpec(application.Spec)
}
