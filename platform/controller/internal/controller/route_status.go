package controller

import (
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	platformv1alpha1 "platform.lab.test/controller/api/v1alpha1"
)

const envoyGatewayController = gatewayv1.GatewayController("gateway.envoyproxy.io/gatewayclass-controller")

type routeReadiness struct {
	ready   bool
	failed  bool
	reason  string
	message string
}

func deploymentMatchesDesired(
	deployment *appsv1.Deployment,
	application *platformv1alpha1.Application,
) bool {
	return deployment.Status.ObservedGeneration >= deployment.Generation &&
		deployment.Status.Replicas == application.Spec.Replicas &&
		deployment.Status.UpdatedReplicas == application.Spec.Replicas &&
		deployment.Status.ReadyReplicas == application.Spec.Replicas &&
		deployment.Status.AvailableReplicas == application.Spec.Replicas &&
		len(deployment.Spec.Template.Spec.Containers) == 1 &&
		deployment.Spec.Template.Spec.Containers[0].Image == application.Spec.Image
}

func serviceMatchesDesired(
	service *corev1.Service,
	application *platformv1alpha1.Application,
) bool {
	return service.Spec.Type == corev1.ServiceTypeClusterIP && service.Spec.ClusterIP != "" &&
		service.Spec.Selector[applicationLabel] == application.Name &&
		len(service.Spec.Ports) == 1 && service.Spec.Ports[0].Port == application.Spec.Port
}

func assessHTTPRoute(route *gatewayv1.HTTPRoute) routeReadiness {
	for _, parent := range route.Status.Parents {
		if !isIntendedGatewayParent(parent) {
			continue
		}
		accepted := findRouteCondition(parent.Conditions, string(gatewayv1.RouteConditionAccepted))
		resolved := findRouteCondition(parent.Conditions, string(gatewayv1.RouteConditionResolvedRefs))
		if accepted == nil || resolved == nil ||
			accepted.ObservedGeneration != route.Generation || resolved.ObservedGeneration != route.Generation {
			return routeReadiness{
				reason: "RouteStatusPending", message: "waiting for current HTTPRoute status from the shared Gateway",
			}
		}
		if accepted.Status == metav1.ConditionFalse {
			return routeReadiness{
				failed: true, reason: "RouteNotAccepted",
				message: fmt.Sprintf("shared Gateway rejected HTTPRoute: %s", accepted.Reason),
			}
		}
		if resolved.Status == metav1.ConditionFalse {
			return routeReadiness{
				failed: true, reason: "RouteReferencesUnresolved",
				message: fmt.Sprintf("HTTPRoute references are unresolved: %s", resolved.Reason),
			}
		}
		if accepted.Status == metav1.ConditionTrue && resolved.Status == metav1.ConditionTrue {
			return routeReadiness{
				ready: true, reason: "RouteAccepted",
				message: "HTTPRoute is accepted by the shared HTTPS Gateway and references are resolved",
			}
		}
		return routeReadiness{
			reason: "RouteStatusPending", message: "HTTPRoute acceptance is not yet determined",
		}
	}
	return routeReadiness{
		reason: "GatewayAttachmentPending", message: "HTTPRoute is not yet attached to the intended shared Gateway",
	}
}

func isIntendedGatewayParent(parent gatewayv1.RouteParentStatus) bool {
	reference := parent.ParentRef
	return parent.ControllerName == envoyGatewayController &&
		reference.Group != nil && *reference.Group == gatewayv1.Group(gatewayv1.GroupName) &&
		reference.Kind != nil && *reference.Kind == gatewayv1.Kind("Gateway") &&
		reference.Namespace != nil && *reference.Namespace == gatewayv1.Namespace("envoy-gateway-system") &&
		reference.Name == gatewayv1.ObjectName("platform") &&
		reference.SectionName != nil && *reference.SectionName == gatewayv1.SectionName("https")
}

func findRouteCondition(conditions []metav1.Condition, conditionType string) *metav1.Condition {
	for index := range conditions {
		if conditions[index].Type == conditionType {
			return &conditions[index]
		}
	}
	return nil
}
