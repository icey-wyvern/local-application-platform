package controller

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	platformv1alpha1 "platform.lab.test/controller/api/v1alpha1"
)

var (
	testConfig *rest.Config
	testClient client.Client
	testScheme *runtime.Scheme
)

func TestMain(m *testing.M) {
	assets := os.Getenv("KUBEBUILDER_ASSETS")
	if assets == "" {
		os.Exit(m.Run())
	}
	root, err := filepath.Abs("../../../..")
	if err != nil {
		panic(err)
	}
	environment := &envtest.Environment{
		BinaryAssetsDirectory: assets,
		CRDDirectoryPaths: []string{
			filepath.Join(root, "cluster", "platform-controller"),
			os.Getenv("GATEWAY_API_CRD_DIRECTORY"),
		},
		ErrorIfCRDPathMissing: true,
	}
	testConfig, err = environment.Start()
	if err != nil {
		panic(err)
	}
	testScheme = runtime.NewScheme()
	if err := platformv1alpha1.AddToScheme(testScheme); err != nil {
		panic(err)
	}
	if err := corev1.AddToScheme(testScheme); err != nil {
		panic(err)
	}
	if err := appsv1.AddToScheme(testScheme); err != nil {
		panic(err)
	}
	if err := networkingv1.AddToScheme(testScheme); err != nil {
		panic(err)
	}
	if err := gatewayv1.Install(testScheme); err != nil {
		panic(err)
	}
	testClient, err = client.New(testConfig, client.Options{Scheme: testScheme})
	if err != nil {
		panic(err)
	}
	code := m.Run()
	if err := environment.Stop(); err != nil {
		panic(err)
	}
	os.Exit(code)
}

func testReconciler(kubernetesClient client.Client) *ApplicationReconciler {
	return &ApplicationReconciler{
		Client:               kubernetesClient,
		Scheme:               testScheme,
		RegistryDockerConfig: []byte(`{"auths":{"registry.lab.test:5000":{"auth":"test"}}}`),
		HealthVerifier:       &fakeHealthVerifier{},
	}
}

type fakeHealthVerifier struct {
	err   error
	calls int
	url   string
}

func (v *fakeHealthVerifier) Verify(_ context.Context, endpoint string) error {
	v.calls++
	v.url = endpoint
	return v.err
}

func validApplication(name string) *platformv1alpha1.Application {
	return &platformv1alpha1.Application{
		TypeMeta: metav1.TypeMeta{
			APIVersion: platformv1alpha1.GroupVersion.String(),
			Kind:       "Application",
		},
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: platformv1alpha1.ApplicationSpec{
			ReleaseID:  "00000000-0000-4000-8000-000000000001",
			Image:      digestImage("a"),
			Port:       8080,
			HealthPath: "/healthz",
			Replicas:   2,
		},
	}
}

func digestImage(character string) string {
	value := ""
	for range 64 {
		value += character
	}
	return "registry.lab.test:5000/apps/example@sha256:" + value
}

func TestCRDSchemaAcceptsOnlyValidSpecification(t *testing.T) {
	if testClient == nil {
		t.Skip("KUBEBUILDER_ASSETS is required for envtest")
	}
	ctx := context.Background()
	valid := validApplication("schema-valid")
	valid.Spec.Image = digestImage("a")
	if err := testClient.Create(ctx, valid); err != nil {
		t.Fatalf("create valid Application: %v", err)
	}
	valid.Spec.ReleaseID = "00000000-0000-4000-8000-000000000002"
	if err := testClient.Update(ctx, valid); err != nil {
		t.Fatalf("update valid Application: %v", err)
	}
	if err := testClient.Delete(ctx, valid); err != nil {
		t.Fatalf("delete valid Application: %v", err)
	}

	tests := map[string]func(*platformv1alpha1.Application){
		"name": func(application *platformv1alpha1.Application) {
			application.Name = "application-name-that-is-longer-than-the-platform-limit"
		},
		"replicas": func(application *platformv1alpha1.Application) { application.Spec.Replicas = 1 },
		"port":     func(application *platformv1alpha1.Application) { application.Spec.Port = 80 },
		"image": func(application *platformv1alpha1.Application) {
			application.Spec.Image = "registry.lab.test:5000/apps/example:latest"
		},
		"healthPath": func(application *platformv1alpha1.Application) {
			application.Spec.HealthPath = "//internal.example/health"
		},
		"releaseId": func(application *platformv1alpha1.Application) {
			application.Spec.ReleaseID = "not-a-uuid"
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			application := validApplication("invalid-" + name)
			application.Spec.Image = digestImage("b")
			mutate(application)
			if err := testClient.Create(ctx, application); err == nil {
				t.Fatal("invalid Application was accepted")
			}
		})
	}
}

func TestCRDRejectsUnknownSpecFieldsInStrictMode(t *testing.T) {
	if testConfig == nil {
		t.Skip("KUBEBUILDER_ASSETS is required for envtest")
	}
	resource := schema.GroupVersionResource{
		Group: "platform.lab.test", Version: "v1alpha1", Resource: "applications",
	}
	application := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "platform.lab.test/v1alpha1",
		"kind":       "Application",
		"metadata":   map[string]any{"name": "unknown-field"},
		"spec": map[string]any{
			"releaseId":  "00000000-0000-4000-8000-000000000001",
			"image":      digestImage("c"),
			"port":       int64(8080),
			"healthPath": "/healthz",
			"replicas":   int64(2),
			"unknown":    true,
		},
	}}
	dynamicClient, err := dynamic.NewForConfig(testConfig)
	if err != nil {
		t.Fatalf("create dynamic client: %v", err)
	}
	_, err = dynamicClient.Resource(resource).Create(
		context.Background(), application, metav1.CreateOptions{FieldValidation: "Strict"},
	)
	if err == nil {
		t.Fatal("unknown spec field was accepted in strict mode")
	}
}

func TestReconcileStatusFinalizerIdempotencyRestartAndDeletion(t *testing.T) {
	if testClient == nil {
		t.Skip("KUBEBUILDER_ASSETS is required for envtest")
	}
	ctx := context.Background()
	application := validApplication("reconcile-lifecycle")
	application.Spec.Image = digestImage("d")
	if err := testClient.Create(ctx, application); err != nil {
		t.Fatalf("create Application: %v", err)
	}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(application)}
	fixedTime := metav1.NewTime(time.Unix(1_800_000_000, 0))
	newReconciler := func() *ApplicationReconciler {
		reconciler := testReconciler(testClient)
		reconciler.Now = func() metav1.Time { return fixedTime }
		return reconciler
	}

	if _, err := newReconciler().Reconcile(ctx, request); err != nil {
		t.Fatalf("add finalizer: %v", err)
	}
	if _, err := newReconciler().Reconcile(ctx, request); err != nil {
		t.Fatalf("reconcile status: %v", err)
	}
	stored := &platformv1alpha1.Application{}
	if err := testClient.Get(ctx, request.NamespacedName, stored); err != nil {
		t.Fatalf("read reconciled Application: %v", err)
	}
	if len(stored.Finalizers) != 1 || stored.Finalizers[0] != platformv1alpha1.ApplicationFinalizer {
		t.Fatalf("unexpected finalizers: %v", stored.Finalizers)
	}
	if stored.Status.ObservedGeneration != stored.Generation || stored.Status.Phase != platformv1alpha1.ApplicationPhasePending {
		t.Fatalf("unexpected status: %#v", stored.Status)
	}
	if condition := meta.FindStatusCondition(stored.Status.Conditions, reconciledCondition); condition == nil || condition.Reason != "RuntimeResourcesReconciled" {
		t.Fatalf("unexpected conditions: %#v", stored.Status.Conditions)
	}

	resourceVersion := stored.ResourceVersion
	if _, err := newReconciler().Reconcile(ctx, request); err != nil {
		t.Fatalf("repeat reconciliation: %v", err)
	}
	if err := testClient.Get(ctx, request.NamespacedName, stored); err != nil {
		t.Fatalf("read repeatedly reconciled Application: %v", err)
	}
	if stored.ResourceVersion != resourceVersion {
		t.Fatalf("idempotent reconcile changed resource version: %s -> %s", resourceVersion, stored.ResourceVersion)
	}

	stored.Spec.ReleaseID = "00000000-0000-4000-8000-000000000009"
	stored.Spec.Image = digestImage("e")
	if err := testClient.Update(ctx, stored); err != nil {
		t.Fatalf("update desired state: %v", err)
	}
	if _, err := newReconciler().Reconcile(ctx, request); err != nil {
		t.Fatalf("restart reconciliation: %v", err)
	}
	if err := testClient.Get(ctx, request.NamespacedName, stored); err != nil {
		t.Fatalf("read updated Application: %v", err)
	}
	if stored.Status.ObservedGeneration != stored.Generation || stored.Status.DesiredImage != stored.Spec.Image {
		t.Fatalf("updated generation was not observed: %#v", stored.Status)
	}

	if err := testClient.Delete(ctx, stored); err != nil {
		t.Fatalf("begin Application deletion: %v", err)
	}
	if _, err := newReconciler().Reconcile(ctx, request); err != nil {
		t.Fatalf("begin runtime deletion: %v", err)
	}
	// envtest does not run the namespace controller, so complete its finalize
	// subresource step before asking the Application reconciler to continue.
	namespace := &corev1.Namespace{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: applicationNamespace(stored)}, namespace); err == nil {
		namespace.Spec.Finalizers = nil
		if err := testClient.SubResource("finalize").Update(ctx, namespace); err != nil {
			t.Fatalf("simulate namespace controller finalization: %v", err)
		}
	}
	if _, err := newReconciler().Reconcile(ctx, request); err != nil {
		t.Fatalf("finish finalizer deletion: %v", err)
	}
	if err := testClient.Get(ctx, request.NamespacedName, stored); client.IgnoreNotFound(err) != nil || err == nil {
		t.Fatalf("Application still exists after finalizer cleanup: %v", err)
	}
}

func TestRuntimeResourcesAreDeterministicAndMatchContract(t *testing.T) {
	if testClient == nil {
		t.Skip("KUBEBUILDER_ASSETS is required for envtest")
	}
	ctx := context.Background()
	application := validApplication("runtime-contract")
	application.Spec.Image = digestImage("f")
	if err := testClient.Create(ctx, application); err != nil {
		t.Fatalf("create Application: %v", err)
	}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(application)}
	reconciler := testReconciler(testClient)
	for range 2 {
		if _, err := reconciler.Reconcile(ctx, request); err != nil {
			t.Fatalf("reconcile runtime resources: %v", err)
		}
	}

	namespaceName := applicationNamespace(application)
	namespace := &corev1.Namespace{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: namespaceName}, namespace); err != nil {
		t.Fatalf("read Namespace: %v", err)
	}
	if namespace.Labels["pod-security.kubernetes.io/enforce"] != "restricted" {
		t.Fatalf("Namespace does not enforce restricted Pod Security: %v", namespace.Labels)
	}

	serviceAccount := &corev1.ServiceAccount{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: runtimeName, Namespace: namespaceName}, serviceAccount); err != nil {
		t.Fatalf("read ServiceAccount: %v", err)
	}
	if serviceAccount.AutomountServiceAccountToken == nil || *serviceAccount.AutomountServiceAccountToken ||
		len(serviceAccount.ImagePullSecrets) != 1 || serviceAccount.ImagePullSecrets[0].Name != registryPullSecretName {
		t.Fatalf("unexpected ServiceAccount: %#v", serviceAccount)
	}

	secret := &corev1.Secret{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: registryPullSecretName, Namespace: namespaceName}, secret); err != nil {
		t.Fatalf("read pull Secret: %v", err)
	}
	if secret.Type != corev1.SecretTypeDockerConfigJson || len(secret.Data[corev1.DockerConfigJsonKey]) == 0 {
		t.Fatalf("unexpected pull Secret contract: %#v", secret.Type)
	}

	quota := &corev1.ResourceQuota{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: runtimeName, Namespace: namespaceName}, quota); err != nil {
		t.Fatalf("read ResourceQuota: %v", err)
	}
	podLimit := quota.Spec.Hard[corev1.ResourcePods]
	cpuLimit := quota.Spec.Hard[corev1.ResourceLimitsCPU]
	if podLimit.String() != "3" || cpuLimit.String() != "3" {
		t.Fatalf("unexpected ResourceQuota: %#v", quota.Spec.Hard)
	}
	limitRange := &corev1.LimitRange{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: runtimeName, Namespace: namespaceName}, limitRange); err != nil {
		t.Fatalf("read LimitRange: %v", err)
	}
	if got := limitRange.Spec.Limits[0].DefaultRequest.Cpu().String(); got != "100m" {
		t.Fatalf("unexpected default CPU request: %s", got)
	}

	deployment := &appsv1.Deployment{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: runtimeName, Namespace: namespaceName}, deployment); err != nil {
		t.Fatalf("read Deployment: %v", err)
	}
	container := deployment.Spec.Template.Spec.Containers[0]
	if *deployment.Spec.Replicas != 2 || container.Image != application.Spec.Image ||
		container.Ports[0].ContainerPort != application.Spec.Port ||
		container.ReadinessProbe.HTTPGet.Path != application.Spec.HealthPath ||
		container.LivenessProbe.HTTPGet.Path != application.Spec.HealthPath ||
		container.Resources.Requests.Cpu().String() != "100m" ||
		container.Resources.Limits.Memory().String() != "512Mi" ||
		deployment.Spec.ProgressDeadlineSeconds == nil || *deployment.Spec.ProgressDeadlineSeconds != 300 ||
		deployment.Spec.Strategy.RollingUpdate.MaxUnavailable.IntValue() != 0 ||
		deployment.Spec.Strategy.RollingUpdate.MaxSurge.IntValue() != 1 ||
		len(deployment.Spec.Template.Spec.TopologySpreadConstraints) != 1 ||
		deployment.Spec.Template.Spec.SecurityContext == nil ||
		container.SecurityContext == nil || container.SecurityContext.Privileged != nil {
		t.Fatalf("unexpected Deployment contract: %#v", deployment.Spec)
	}

	service := &corev1.Service{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: runtimeName, Namespace: namespaceName}, service); err != nil {
		t.Fatalf("read Service: %v", err)
	}
	if service.Spec.Type != corev1.ServiceTypeClusterIP || service.Spec.Ports[0].Port != application.Spec.Port {
		t.Fatalf("unexpected Service contract: %#v", service.Spec)
	}
	route := &gatewayv1.HTTPRoute{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: runtimeName, Namespace: namespaceName}, route); err != nil {
		t.Fatalf("read HTTPRoute: %v", err)
	}
	desiredRoute := desiredHTTPRouteSpec(application)
	if !reflect.DeepEqual(route.Spec, desiredRoute) ||
		string(route.Spec.Hostnames[0]) != application.Name+".apps.lab.test" ||
		route.Spec.ParentRefs[0].SectionName == nil || string(*route.Spec.ParentRefs[0].SectionName) != "https" ||
		string(route.Spec.Rules[0].BackendRefs[0].Name) != runtimeName ||
		int32(*route.Spec.Rules[0].BackendRefs[0].Port) != application.Spec.Port {
		t.Fatalf("unexpected HTTPRoute contract: %#v", route.Spec)
	}

	policyList := &networkingv1.NetworkPolicyList{}
	if err := testClient.List(ctx, policyList, client.InNamespace(namespaceName)); err != nil {
		t.Fatalf("list NetworkPolicies: %v", err)
	}
	if len(policyList.Items) != 4 {
		t.Fatalf("expected four NetworkPolicies, got %d", len(policyList.Items))
	}
	policies := map[string]networkingv1.NetworkPolicy{}
	for _, policy := range policyList.Items {
		policies[policy.Name] = policy
	}
	defaultDeny := policies["default-deny"]
	if len(defaultDeny.Spec.PolicyTypes) != 2 || len(defaultDeny.Spec.Ingress) != 0 || len(defaultDeny.Spec.Egress) != 0 {
		t.Fatalf("unexpected default-deny policy: %#v", defaultDeny.Spec)
	}
	dns := policies["allow-dns"].Spec.Egress[0]
	if len(dns.Ports) != 2 ||
		dns.To[0].NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] != "kube-system" ||
		dns.To[0].PodSelector.MatchLabels["k8s-app"] != "kube-dns" {
		t.Fatalf("unexpected DNS policy: %#v", dns)
	}
	ingress := policies["allow-envoy-ingress"].Spec.Ingress[0]
	if ingress.Ports[0].Port.IntValue() != int(application.Spec.Port) ||
		ingress.From[0].NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] != "envoy-gateway-system" ||
		ingress.From[0].PodSelector.MatchLabels["app.kubernetes.io/component"] != "proxy" {
		t.Fatalf("unexpected Envoy ingress policy: %#v", ingress)
	}
	https := policies["allow-public-https"].Spec.Egress[0]
	if https.Ports[0].Port.IntValue() != 443 || https.To[0].IPBlock.CIDR != "0.0.0.0/0" ||
		len(https.To[0].IPBlock.Except) != len(publicIPv4Exclusions) {
		t.Fatalf("unexpected public HTTPS policy: %#v", https)
	}
	first := desiredNetworkPolicies(application, namespaceName)
	second := desiredNetworkPolicies(application, namespaceName)
	if !reflect.DeepEqual(first, second) {
		t.Fatal("runtime NetworkPolicy generation is not deterministic")
	}

	deployment.Status.ObservedGeneration = deployment.Generation
	deployment.Status.Replicas = 2
	deployment.Status.UpdatedReplicas = 2
	deployment.Status.ReadyReplicas = 2
	deployment.Status.AvailableReplicas = 2
	if err := testClient.Status().Update(ctx, deployment); err != nil {
		t.Fatalf("mark Deployment available: %v", err)
	}
	setRouteAccepted(t, ctx, route)
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatalf("reconcile ready status: %v", err)
	}
	stored := &platformv1alpha1.Application{}
	if err := testClient.Get(ctx, request.NamespacedName, stored); err != nil {
		t.Fatalf("read ready Application: %v", err)
	}
	if stored.Status.Phase != platformv1alpha1.ApplicationPhaseReady ||
		stored.Status.ReadyReplicas != 2 || stored.Status.ActiveReleaseID != stored.Spec.ReleaseID ||
		stored.Status.CurrentImage != stored.Spec.Image ||
		meta.IsStatusConditionTrue(stored.Status.Conditions, healthyCondition) == false {
		t.Fatalf("unexpected ready status: %#v", stored.Status)
	}
}

func TestRuntimeUpdateAndDriftRepairAreIdempotent(t *testing.T) {
	if testClient == nil {
		t.Skip("KUBEBUILDER_ASSETS is required for envtest")
	}
	ctx := context.Background()
	application := validApplication("runtime-drift")
	application.Spec.Image = digestImage("1")
	if err := testClient.Create(ctx, application); err != nil {
		t.Fatalf("create Application: %v", err)
	}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(application)}
	reconciler := testReconciler(testClient)
	for range 2 {
		if _, err := reconciler.Reconcile(ctx, request); err != nil {
			t.Fatalf("initial reconciliation: %v", err)
		}
	}

	namespaceName := applicationNamespace(application)
	service := &corev1.Service{}
	serviceKey := client.ObjectKey{Name: runtimeName, Namespace: namespaceName}
	if err := testClient.Get(ctx, serviceKey, service); err != nil {
		t.Fatalf("read Service: %v", err)
	}
	clusterIP := service.Spec.ClusterIP
	service.Annotations = map[string]string{"safe.example.test/preserved": "true"}
	service.Spec.Selector = map[string]string{"drifted": "true"}
	if err := testClient.Update(ctx, service); err != nil {
		t.Fatalf("drift Service: %v", err)
	}
	policy := &networkingv1.NetworkPolicy{}
	policyKey := client.ObjectKey{Name: "allow-dns", Namespace: namespaceName}
	if err := testClient.Get(ctx, policyKey, policy); err != nil {
		t.Fatalf("read NetworkPolicy: %v", err)
	}
	if err := testClient.Delete(ctx, policy); err != nil {
		t.Fatalf("delete NetworkPolicy: %v", err)
	}
	route := &gatewayv1.HTTPRoute{}
	routeKey := client.ObjectKey{Name: runtimeName, Namespace: namespaceName}
	if err := testClient.Get(ctx, routeKey, route); err != nil {
		t.Fatalf("read HTTPRoute: %v", err)
	}
	route.Spec.Hostnames = []gatewayv1.Hostname{"drifted.invalid"}
	if err := testClient.Update(ctx, route); err != nil {
		t.Fatalf("drift HTTPRoute: %v", err)
	}
	storedApplication := &platformv1alpha1.Application{}
	if err := testClient.Get(ctx, request.NamespacedName, storedApplication); err != nil {
		t.Fatalf("read Application: %v", err)
	}
	storedApplication.Spec.ReleaseID = "00000000-0000-4000-8000-000000000008"
	storedApplication.Spec.Image = digestImage("2")
	storedApplication.Spec.Port = 9090
	storedApplication.Spec.HealthPath = "/ready"
	if err := testClient.Update(ctx, storedApplication); err != nil {
		t.Fatalf("update Application: %v", err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatalf("repair drift: %v", err)
	}
	if err := testClient.Get(ctx, serviceKey, service); err != nil {
		t.Fatalf("read repaired Service: %v", err)
	}
	if service.Spec.ClusterIP != clusterIP || service.Spec.Selector[applicationLabel] != application.Name ||
		service.Annotations["safe.example.test/preserved"] != "true" {
		t.Fatalf("Service drift was not safely repaired: %#v", service.Spec)
	}
	if err := testClient.Get(ctx, policyKey, policy); err != nil {
		t.Fatalf("deleted NetworkPolicy was not recreated: %v", err)
	}
	if err := testClient.Get(ctx, routeKey, route); err != nil {
		t.Fatalf("read repaired HTTPRoute: %v", err)
	}
	if !reflect.DeepEqual(route.Spec, desiredHTTPRouteSpec(storedApplication)) {
		t.Fatalf("HTTPRoute drift was not repaired: %#v", route.Spec)
	}
	deployment := &appsv1.Deployment{}
	if err := testClient.Get(ctx, serviceKey, deployment); err != nil {
		t.Fatalf("read updated Deployment: %v", err)
	}
	if deployment.Spec.Template.Spec.Containers[0].Image != storedApplication.Spec.Image {
		t.Fatal("Deployment image was not updated")
	}
	if deployment.Spec.Template.Spec.Containers[0].Ports[0].ContainerPort != 9090 ||
		deployment.Spec.Template.Spec.Containers[0].ReadinessProbe.HTTPGet.Path != "/ready" {
		t.Fatal("Deployment port and health path were not updated")
	}
	resourceVersion := deployment.ResourceVersion
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatalf("repeat reconciliation: %v", err)
	}
	if err := testClient.Get(ctx, serviceKey, deployment); err != nil {
		t.Fatalf("read repeated Deployment: %v", err)
	}
	if deployment.ResourceVersion != resourceVersion {
		t.Fatalf("idempotent reconciliation rewrote Deployment: %s -> %s", resourceVersion, deployment.ResourceVersion)
	}
}

func setRouteAccepted(t *testing.T, ctx context.Context, route *gatewayv1.HTTPRoute) {
	t.Helper()
	if err := testClient.Get(ctx, client.ObjectKeyFromObject(route), route); err != nil {
		t.Fatalf("read HTTPRoute for status: %v", err)
	}
	route.Status.Parents = []gatewayv1.RouteParentStatus{{
		ParentRef:      route.Spec.ParentRefs[0],
		ControllerName: envoyGatewayController,
		Conditions: []metav1.Condition{
			{
				Type: string(gatewayv1.RouteConditionAccepted), Status: metav1.ConditionTrue,
				ObservedGeneration: route.Generation, Reason: "Accepted", Message: "Route is accepted",
				LastTransitionTime: metav1.Now(),
			},
			{
				Type: string(gatewayv1.RouteConditionResolvedRefs), Status: metav1.ConditionTrue,
				ObservedGeneration: route.Generation, Reason: "ResolvedRefs", Message: "References resolved",
				LastTransitionTime: metav1.Now(),
			},
		},
	}}
	if err := testClient.Status().Update(ctx, route); err != nil {
		t.Fatalf("mark HTTPRoute accepted: %v", err)
	}
}

func TestHTTPSFailureDoesNotReportApplicationReady(t *testing.T) {
	if testClient == nil {
		t.Skip("KUBEBUILDER_ASSETS is required for envtest")
	}
	ctx := context.Background()
	application := validApplication("runtime-health-failure")
	application.Spec.Image = digestImage("4")
	if err := testClient.Create(ctx, application); err != nil {
		t.Fatalf("create Application: %v", err)
	}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(application)}
	verifier := &fakeHealthVerifier{err: errors.New("trusted HTTPS endpoint returned HTTP 503")}
	reconciler := testReconciler(testClient)
	reconciler.HealthVerifier = verifier
	for range 2 {
		if _, err := reconciler.Reconcile(ctx, request); err != nil {
			t.Fatalf("reconcile runtime resources: %v", err)
		}
	}
	namespaceName := applicationNamespace(application)
	deployment := &appsv1.Deployment{}
	key := client.ObjectKey{Name: runtimeName, Namespace: namespaceName}
	if err := testClient.Get(ctx, key, deployment); err != nil {
		t.Fatalf("read Deployment: %v", err)
	}
	deployment.Status.ObservedGeneration = deployment.Generation
	deployment.Status.Replicas = 2
	deployment.Status.UpdatedReplicas = 2
	deployment.Status.ReadyReplicas = 2
	deployment.Status.AvailableReplicas = 2
	if err := testClient.Status().Update(ctx, deployment); err != nil {
		t.Fatalf("mark Deployment available: %v", err)
	}
	route := &gatewayv1.HTTPRoute{}
	if err := testClient.Get(ctx, key, route); err != nil {
		t.Fatalf("read HTTPRoute: %v", err)
	}
	setRouteAccepted(t, ctx, route)
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatalf("reconcile failed health: %v", err)
	}
	stored := &platformv1alpha1.Application{}
	if err := testClient.Get(ctx, request.NamespacedName, stored); err != nil {
		t.Fatalf("read Application: %v", err)
	}
	if verifier.calls != 1 || verifier.url != "https://runtime-health-failure.apps.lab.test/healthz" ||
		stored.Status.Phase != platformv1alpha1.ApplicationPhaseError ||
		meta.IsStatusConditionFalse(stored.Status.Conditions, healthyCondition) == false ||
		meta.IsStatusConditionFalse(stored.Status.Conditions, availableCondition) == false {
		t.Fatalf("HTTPS failure was not represented safely: %#v", stored.Status)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatalf("repeat failed health reconciliation: %v", err)
	}
	if verifier.calls != 1 {
		t.Fatalf("health verification was not throttled: %d calls", verifier.calls)
	}
}

func TestHTTPRouteReadinessRequiresAcceptedAndResolvedReferences(t *testing.T) {
	application := validApplication("route-readiness")
	route := &gatewayv1.HTTPRoute{
		ObjectMeta: metav1.ObjectMeta{Generation: 4},
		Spec:       desiredHTTPRouteSpec(application),
	}
	condition := func(conditionType string, status metav1.ConditionStatus, reason string) metav1.Condition {
		return metav1.Condition{
			Type: conditionType, Status: status, ObservedGeneration: route.Generation,
			Reason: reason, Message: reason, LastTransitionTime: metav1.Now(),
		}
	}
	route.Status.Parents = []gatewayv1.RouteParentStatus{{
		ParentRef: route.Spec.ParentRefs[0], ControllerName: envoyGatewayController,
		Conditions: []metav1.Condition{
			condition(string(gatewayv1.RouteConditionAccepted), metav1.ConditionTrue, "Accepted"),
			condition(string(gatewayv1.RouteConditionResolvedRefs), metav1.ConditionTrue, "ResolvedRefs"),
		},
	}}
	if assessment := assessHTTPRoute(route); !assessment.ready || assessment.failed {
		t.Fatalf("accepted route was not ready: %#v", assessment)
	}
	route.Status.Parents[0].Conditions[0] = condition(
		string(gatewayv1.RouteConditionAccepted), metav1.ConditionFalse, "NotAllowedByListeners",
	)
	if assessment := assessHTTPRoute(route); assessment.ready || !assessment.failed || assessment.reason != "RouteNotAccepted" {
		t.Fatalf("rejected route was not failed: %#v", assessment)
	}
	route.Status.Parents[0].Conditions[0] = condition(
		string(gatewayv1.RouteConditionAccepted), metav1.ConditionTrue, "Accepted",
	)
	route.Status.Parents[0].Conditions[1] = condition(
		string(gatewayv1.RouteConditionResolvedRefs), metav1.ConditionFalse, "BackendNotFound",
	)
	if assessment := assessHTTPRoute(route); assessment.ready || !assessment.failed || assessment.reason != "RouteReferencesUnresolved" {
		t.Fatalf("unresolved route was not failed: %#v", assessment)
	}
}

type failOnceClient struct {
	client.Client
	failed bool
}

func (c *failOnceClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if _, isService := object.(*corev1.Service); isService && !c.failed {
		c.failed = true
		return errors.New("injected Service creation failure")
	}
	return c.Client.Create(ctx, object, options...)
}

func TestPartialFailureIsSafelyRetried(t *testing.T) {
	if testClient == nil {
		t.Skip("KUBEBUILDER_ASSETS is required for envtest")
	}
	ctx := context.Background()
	application := validApplication("runtime-partial")
	application.Spec.Image = digestImage("3")
	if err := testClient.Create(ctx, application); err != nil {
		t.Fatalf("create Application: %v", err)
	}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(application)}
	failingClient := &failOnceClient{Client: testClient}
	reconciler := testReconciler(failingClient)
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatalf("add finalizer: %v", err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err == nil {
		t.Fatal("injected partial failure was not returned")
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatalf("retry partial reconciliation: %v", err)
	}
	deployment := &appsv1.Deployment{}
	if err := testClient.Get(ctx, client.ObjectKey{Name: runtimeName, Namespace: applicationNamespace(application)}, deployment); err != nil {
		t.Fatalf("partial retry did not complete resources: %v", err)
	}
}
