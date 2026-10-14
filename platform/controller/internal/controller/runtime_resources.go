package controller

import (
	"context"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	platformv1alpha1 "platform.lab.test/controller/api/v1alpha1"
)

const (
	runtimeName            = "application"
	registryPullSecretName = "registry-pull"
	applicationLabel       = "platform.lab.test/application"
	managedByLabel         = "app.kubernetes.io/managed-by"
	releaseAnnotation      = "platform.lab.test/release-id"
)

var publicIPv4Exclusions = []string{
	"0.0.0.0/8",
	"10.0.0.0/8",
	"100.64.0.0/10",
	"127.0.0.0/8",
	"169.254.0.0/16",
	"172.16.0.0/12",
	"192.0.0.0/24",
	"192.0.2.0/24",
	"192.168.0.0/16",
	"198.18.0.0/15",
	"198.51.100.0/24",
	"203.0.113.0/24",
	"224.0.0.0/4",
	"240.0.0.0/4",
}

type runtimeResources struct {
	Deployment *appsv1.Deployment
	Service    *corev1.Service
	Route      *gatewayv1.HTTPRoute
}

func applicationNamespace(application *platformv1alpha1.Application) string {
	return "app-" + application.Name
}

func runtimeLabels(application *platformv1alpha1.Application) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":     application.Name,
		"app.kubernetes.io/instance": application.Name,
		managedByLabel:               "platform-controller",
		applicationLabel:             application.Name,
	}
}

func mergeStringMap(current map[string]string, desired map[string]string) map[string]string {
	if current == nil {
		current = map[string]string{}
	}
	for key, value := range desired {
		current[key] = value
	}
	return current
}

func (r *ApplicationReconciler) reconcileRuntimeResources(
	ctx context.Context,
	application *platformv1alpha1.Application,
) (*runtimeResources, error) {
	namespace, err := r.reconcileNamespace(ctx, application)
	if err != nil {
		return nil, err
	}

	reconcilers := []func(context.Context, *platformv1alpha1.Application, *corev1.Namespace) error{
		r.reconcileServiceAccount,
		r.reconcileResourceQuota,
		r.reconcileLimitRange,
		r.reconcileRegistryPullSecret,
		r.reconcileNetworkPolicies,
	}
	for _, reconcile := range reconcilers {
		if err := reconcile(ctx, application, namespace); err != nil {
			return nil, err
		}
	}

	deployment, err := r.reconcileDeployment(ctx, application, namespace)
	if err != nil {
		return nil, err
	}
	service, err := r.reconcileService(ctx, application, namespace)
	if err != nil {
		return nil, err
	}
	route, err := r.reconcileHTTPRoute(ctx, application, namespace)
	if err != nil {
		return nil, err
	}
	return &runtimeResources{Deployment: deployment, Service: service, Route: route}, nil
}

func (r *ApplicationReconciler) reconcileNamespace(
	ctx context.Context,
	application *platformv1alpha1.Application,
) (*corev1.Namespace, error) {
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: applicationNamespace(application)}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, namespace, func() error {
		if !namespace.CreationTimestamp.IsZero() && !metav1.IsControlledBy(namespace, application) {
			return fmt.Errorf("namespace %q is not owned by Application %q", namespace.Name, application.Name)
		}
		namespace.Labels = mergeStringMap(namespace.Labels, map[string]string{
			applicationLabel:                     application.Name,
			managedByLabel:                       "platform-controller",
			"pod-security.kubernetes.io/enforce": "restricted",
			"pod-security.kubernetes.io/audit":   "restricted",
			"pod-security.kubernetes.io/warn":    "restricted",
		})
		return controllerutil.SetControllerReference(application, namespace, r.Scheme)
	})
	if err != nil {
		return nil, fmt.Errorf("reconcile Namespace: %w", err)
	}
	return namespace, nil
}

func (r *ApplicationReconciler) reconcileServiceAccount(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) error {
	serviceAccount := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Name: runtimeName, Namespace: namespace.Name,
	}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, serviceAccount, func() error {
		serviceAccount.Labels = mergeStringMap(serviceAccount.Labels, runtimeLabels(application))
		serviceAccount.AutomountServiceAccountToken = ptr.To(false)
		serviceAccount.ImagePullSecrets = []corev1.LocalObjectReference{{Name: registryPullSecretName}}
		return controllerutil.SetControllerReference(application, serviceAccount, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("reconcile ServiceAccount: %w", err)
	}
	return nil
}

func (r *ApplicationReconciler) reconcileResourceQuota(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) error {
	quota := &corev1.ResourceQuota{ObjectMeta: metav1.ObjectMeta{
		Name: runtimeName, Namespace: namespace.Name,
	}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, quota, func() error {
		quota.Labels = mergeStringMap(quota.Labels, runtimeLabels(application))
		quota.Spec.Hard = corev1.ResourceList{
			corev1.ResourcePods:                           resource.MustParse("3"),
			corev1.ResourceRequestsCPU:                    resource.MustParse("500m"),
			corev1.ResourceRequestsMemory:                 resource.MustParse("512Mi"),
			corev1.ResourceLimitsCPU:                      resource.MustParse("3"),
			corev1.ResourceLimitsMemory:                   resource.MustParse("1536Mi"),
			corev1.ResourceServices:                       resource.MustParse("1"),
			corev1.ResourceName("count/deployments.apps"): resource.MustParse("1"),
		}
		return controllerutil.SetControllerReference(application, quota, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("reconcile ResourceQuota: %w", err)
	}
	return nil
}

func (r *ApplicationReconciler) reconcileLimitRange(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) error {
	limitRange := &corev1.LimitRange{ObjectMeta: metav1.ObjectMeta{
		Name: runtimeName, Namespace: namespace.Name,
	}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, limitRange, func() error {
		limitRange.Labels = mergeStringMap(limitRange.Labels, runtimeLabels(application))
		limitRange.Spec.Limits = []corev1.LimitRangeItem{{
			Type: corev1.LimitTypeContainer,
			DefaultRequest: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
			Default: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("1"),
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
			Max: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("1"),
				corev1.ResourceMemory: resource.MustParse("512Mi"),
			},
		}}
		return controllerutil.SetControllerReference(application, limitRange, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("reconcile LimitRange: %w", err)
	}
	return nil
}

func (r *ApplicationReconciler) reconcileRegistryPullSecret(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) error {
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Name: registryPullSecretName, Namespace: namespace.Name,
	}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, secret, func() error {
		secret.Labels = mergeStringMap(secret.Labels, runtimeLabels(application))
		secret.Type = corev1.SecretTypeDockerConfigJson
		secret.Data = map[string][]byte{
			corev1.DockerConfigJsonKey: append([]byte(nil), r.RegistryDockerConfig...),
		}
		return controllerutil.SetControllerReference(application, secret, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("reconcile registry pull Secret: %w", err)
	}
	return nil
}

func (r *ApplicationReconciler) reconcileDeployment(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) (*appsv1.Deployment, error) {
	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Name: runtimeName, Namespace: namespace.Name,
	}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, deployment, func() error {
		labels := runtimeLabels(application)
		deployment.Labels = mergeStringMap(deployment.Labels, labels)
		deployment.Spec.Replicas = ptr.To[int32](2)
		deployment.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{
			applicationLabel: application.Name,
		}}
		deployment.Spec.Strategy = appsv1.DeploymentStrategy{
			Type: appsv1.RollingUpdateDeploymentStrategyType,
			RollingUpdate: &appsv1.RollingUpdateDeployment{
				MaxUnavailable: ptr.To(intstr.FromInt32(0)),
				MaxSurge:       ptr.To(intstr.FromInt32(1)),
			},
		}
		deployment.Spec.ProgressDeadlineSeconds = ptr.To[int32](300)
		deployment.Spec.RevisionHistoryLimit = ptr.To[int32](3)
		deployment.Spec.Template.Labels = mergeStringMap(deployment.Spec.Template.Labels, labels)
		deployment.Spec.Template.Annotations = mergeStringMap(
			deployment.Spec.Template.Annotations,
			map[string]string{releaseAnnotation: application.Spec.ReleaseID},
		)
		deployment.Spec.Template.Spec.ServiceAccountName = runtimeName
		deployment.Spec.Template.Spec.AutomountServiceAccountToken = ptr.To(false)
		deployment.Spec.Template.Spec.RestartPolicy = corev1.RestartPolicyAlways
		deployment.Spec.Template.Spec.DNSPolicy = corev1.DNSClusterFirst
		deployment.Spec.Template.Spec.SchedulerName = corev1.DefaultSchedulerName
		deployment.Spec.Template.Spec.TerminationGracePeriodSeconds = ptr.To[int64](30)
		deployment.Spec.Template.Spec.EnableServiceLinks = ptr.To(false)
		deployment.Spec.Template.Spec.HostNetwork = false
		deployment.Spec.Template.Spec.HostPID = false
		deployment.Spec.Template.Spec.HostIPC = false
		deployment.Spec.Template.Spec.SecurityContext = &corev1.PodSecurityContext{
			RunAsNonRoot:   ptr.To(true),
			RunAsUser:      ptr.To[int64](65532),
			RunAsGroup:     ptr.To[int64](65532),
			SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
		}
		deployment.Spec.Template.Spec.InitContainers = nil
		deployment.Spec.Template.Spec.Containers = []corev1.Container{{
			Name:            runtimeName,
			Image:           application.Spec.Image,
			ImagePullPolicy: corev1.PullIfNotPresent,
			Ports: []corev1.ContainerPort{{
				Name: "http", ContainerPort: application.Spec.Port, Protocol: corev1.ProtocolTCP,
			}},
			Resources: corev1.ResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("100m"),
					corev1.ResourceMemory: resource.MustParse("128Mi"),
				},
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("1"),
					corev1.ResourceMemory: resource.MustParse("512Mi"),
				},
			},
			ReadinessProbe: httpProbe(application.Spec.HealthPath),
			LivenessProbe:  httpProbe(application.Spec.HealthPath),
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				ReadOnlyRootFilesystem:   ptr.To(true),
				RunAsNonRoot:             ptr.To(true),
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
				},
			},
		}}
		deployment.Spec.Template.Spec.TopologySpreadConstraints = []corev1.TopologySpreadConstraint{{
			MaxSkew:           1,
			TopologyKey:       corev1.LabelHostname,
			WhenUnsatisfiable: corev1.DoNotSchedule,
			LabelSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
				applicationLabel: application.Name,
			}},
		}}
		return controllerutil.SetControllerReference(application, deployment, r.Scheme)
	})
	if err != nil {
		return nil, fmt.Errorf("reconcile Deployment: %w", err)
	}
	return deployment, nil
}

func httpProbe(path string) *corev1.Probe {
	return &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
			Path: path,
			Port: intstr.FromString("http"),
		}},
		InitialDelaySeconds: 5,
		PeriodSeconds:       10,
		TimeoutSeconds:      2,
		FailureThreshold:    3,
		SuccessThreshold:    1,
	}
}

func (r *ApplicationReconciler) reconcileService(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) (*corev1.Service, error) {
	service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{
		Name: runtimeName, Namespace: namespace.Name,
	}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, service, func() error {
		service.Labels = mergeStringMap(service.Labels, runtimeLabels(application))
		service.Spec.Type = corev1.ServiceTypeClusterIP
		service.Spec.Selector = map[string]string{applicationLabel: application.Name}
		service.Spec.Ports = []corev1.ServicePort{{
			Name: "http", Port: application.Spec.Port,
			TargetPort: intstr.FromString("http"), Protocol: corev1.ProtocolTCP,
		}}
		return controllerutil.SetControllerReference(application, service, r.Scheme)
	})
	if err != nil {
		return nil, fmt.Errorf("reconcile Service: %w", err)
	}
	return service, nil
}

func (r *ApplicationReconciler) reconcileHTTPRoute(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) (*gatewayv1.HTTPRoute, error) {
	route := &gatewayv1.HTTPRoute{ObjectMeta: metav1.ObjectMeta{
		Name: runtimeName, Namespace: namespace.Name,
	}}
	_, err := controllerutil.CreateOrPatch(ctx, r.Client, route, func() error {
		route.Labels = mergeStringMap(route.Labels, runtimeLabels(application))
		route.Spec = desiredHTTPRouteSpec(application)
		return controllerutil.SetControllerReference(application, route, r.Scheme)
	})
	if err != nil {
		return nil, fmt.Errorf("reconcile HTTPRoute: %w", err)
	}
	return route, nil
}

func desiredHTTPRouteSpec(application *platformv1alpha1.Application) gatewayv1.HTTPRouteSpec {
	group := gatewayv1.Group(gatewayv1.GroupName)
	gatewayKind := gatewayv1.Kind("Gateway")
	serviceGroup := gatewayv1.Group("")
	serviceKind := gatewayv1.Kind("Service")
	gatewayNamespace := gatewayv1.Namespace("envoy-gateway-system")
	sectionName := gatewayv1.SectionName("https")
	pathType := gatewayv1.PathMatchPathPrefix
	path := "/"
	weight := int32(1)
	return gatewayv1.HTTPRouteSpec{
		CommonRouteSpec: gatewayv1.CommonRouteSpec{ParentRefs: []gatewayv1.ParentReference{{
			Group:       &group,
			Kind:        &gatewayKind,
			Namespace:   &gatewayNamespace,
			Name:        gatewayv1.ObjectName("platform"),
			SectionName: &sectionName,
		}}},
		Hostnames: []gatewayv1.Hostname{
			gatewayv1.Hostname(application.Name + ".apps.lab.test"),
		},
		Rules: []gatewayv1.HTTPRouteRule{{
			Matches: []gatewayv1.HTTPRouteMatch{{Path: &gatewayv1.HTTPPathMatch{
				Type: &pathType, Value: &path,
			}}},
			BackendRefs: []gatewayv1.HTTPBackendRef{{BackendRef: gatewayv1.BackendRef{
				BackendObjectReference: gatewayv1.BackendObjectReference{
					Group: &serviceGroup,
					Kind:  &serviceKind,
					Name:  gatewayv1.ObjectName(runtimeName),
					Port:  ptr.To(gatewayv1.PortNumber(application.Spec.Port)),
				},
				Weight: &weight,
			}}},
		}},
	}
}

func (r *ApplicationReconciler) reconcileNetworkPolicies(
	ctx context.Context,
	application *platformv1alpha1.Application,
	namespace *corev1.Namespace,
) error {
	policies := desiredNetworkPolicies(application, namespace.Name)
	for index := range policies {
		policy := policies[index]
		_, err := controllerutil.CreateOrPatch(ctx, r.Client, policy, func() error {
			policy.Labels = mergeStringMap(policy.Labels, runtimeLabels(application))
			policy.Spec = desiredNetworkPolicies(application, namespace.Name)[index].Spec
			return controllerutil.SetControllerReference(application, policy, r.Scheme)
		})
		if err != nil {
			return fmt.Errorf("reconcile NetworkPolicy %q: %w", policy.Name, err)
		}
	}
	return nil
}

func desiredNetworkPolicies(
	application *platformv1alpha1.Application,
	namespace string,
) []*networkingv1.NetworkPolicy {
	allPods := metav1.LabelSelector{}
	workloadPods := metav1.LabelSelector{MatchLabels: map[string]string{
		applicationLabel: application.Name,
	}}
	tcp := corev1.ProtocolTCP
	udp := corev1.ProtocolUDP
	return []*networkingv1.NetworkPolicy{
		{
			ObjectMeta: metav1.ObjectMeta{Name: "default-deny", Namespace: namespace},
			Spec: networkingv1.NetworkPolicySpec{
				PodSelector: allPods,
				PolicyTypes: []networkingv1.PolicyType{
					networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress,
				},
			},
		},
		{
			ObjectMeta: metav1.ObjectMeta{Name: "allow-dns", Namespace: namespace},
			Spec: networkingv1.NetworkPolicySpec{
				PodSelector: allPods,
				PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeEgress},
				Egress: []networkingv1.NetworkPolicyEgressRule{{
					To: []networkingv1.NetworkPolicyPeer{{
						NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
							"kubernetes.io/metadata.name": "kube-system",
						}},
						PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
							"k8s-app": "kube-dns",
						}},
					}},
					Ports: []networkingv1.NetworkPolicyPort{
						{Protocol: &udp, Port: ptr.To(intstr.FromInt32(53))},
						{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(53))},
					},
				}},
			},
		},
		{
			ObjectMeta: metav1.ObjectMeta{Name: "allow-envoy-ingress", Namespace: namespace},
			Spec: networkingv1.NetworkPolicySpec{
				PodSelector: workloadPods,
				PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
				Ingress: []networkingv1.NetworkPolicyIngressRule{{
					From: []networkingv1.NetworkPolicyPeer{{
						NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
							"kubernetes.io/metadata.name": "envoy-gateway-system",
						}},
						PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
							"app.kubernetes.io/component":               "proxy",
							"gateway.envoyproxy.io/owning-gateway-name": "platform",
						}},
					}},
					Ports: []networkingv1.NetworkPolicyPort{{
						Protocol: &tcp, Port: ptr.To(intstr.FromInt32(application.Spec.Port)),
					}},
				}},
			},
		},
		{
			ObjectMeta: metav1.ObjectMeta{Name: "allow-public-https", Namespace: namespace},
			Spec: networkingv1.NetworkPolicySpec{
				PodSelector: allPods,
				PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeEgress},
				Egress: []networkingv1.NetworkPolicyEgressRule{{
					To: []networkingv1.NetworkPolicyPeer{{
						IPBlock: &networkingv1.IPBlock{CIDR: "0.0.0.0/0", Except: publicIPv4Exclusions},
					}},
					Ports: []networkingv1.NetworkPolicyPort{{
						Protocol: &tcp, Port: ptr.To(intstr.FromInt32(443)),
					}},
				}},
			},
		},
	}
}

func (r *ApplicationReconciler) deleteRuntimeNamespace(
	ctx context.Context,
	application *platformv1alpha1.Application,
) (bool, error) {
	namespace := &corev1.Namespace{}
	err := r.Get(ctx, types.NamespacedName{Name: applicationNamespace(application)}, namespace)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("read runtime Namespace: %w", err)
	}
	if !metav1.IsControlledBy(namespace, application) {
		return false, fmt.Errorf("refusing to delete Namespace %q not owned by Application", namespace.Name)
	}
	if namespace.DeletionTimestamp.IsZero() {
		if err := r.Delete(ctx, namespace); err != nil && !apierrors.IsNotFound(err) {
			return false, fmt.Errorf("delete runtime Namespace: %w", err)
		}
	}
	return false, nil
}
