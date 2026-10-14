package v1alpha1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

const ApplicationFinalizer = "platform.lab.test/application-finalizer"

// ApplicationSpec is the complete user-facing desired-state contract.
type ApplicationSpec struct {
	// ReleaseID is the durable platform Release UUID.
	// +kubebuilder:validation:Format=uuid
	ReleaseID string `json:"releaseId"`

	// Image is an immutable OCI image reference containing a sha256 digest.
	// +kubebuilder:validation:MaxLength=2048
	// +kubebuilder:validation:Pattern=`^.+@sha256:[0-9a-f]{64}$`
	Image string `json:"image"`

	// Port is the validated application container port.
	// +kubebuilder:validation:Minimum=1024
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port"`

	// HealthPath is an absolute HTTP path without query string or fragment.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	// +kubebuilder:validation:Pattern=`^/[^?#]*$`
	// +kubebuilder:validation:XValidation:rule="!self.startsWith('//')",message="healthPath must not be a network-path reference"
	HealthPath string `json:"healthPath"`

	// Replicas is fixed at two for the first platform version.
	// +kubebuilder:validation:Minimum=2
	// +kubebuilder:validation:Maximum=2
	Replicas int32 `json:"replicas"`
}

// ApplicationPhase summarizes controller progress without replacing Conditions.
// +kubebuilder:validation:Enum=Pending;Ready;Error;Deleting
type ApplicationPhase string

const (
	ApplicationPhasePending  ApplicationPhase = "Pending"
	ApplicationPhaseReady    ApplicationPhase = "Ready"
	ApplicationPhaseError    ApplicationPhase = "Error"
	ApplicationPhaseDeleting ApplicationPhase = "Deleting"
)

// ApplicationStatus is entirely controller-owned.
type ApplicationStatus struct {
	ObservedGeneration      int64            `json:"observedGeneration,omitempty"`
	ObservedRouteGeneration int64            `json:"observedRouteGeneration,omitempty"`
	ActiveReleaseID         string           `json:"activeReleaseId,omitempty"`
	DesiredImage            string           `json:"desiredImage,omitempty"`
	CurrentImage            string           `json:"currentImage,omitempty"`
	ReadyReplicas           int32            `json:"readyReplicas,omitempty"`
	Hostname                string           `json:"hostname,omitempty"`
	Phase                   ApplicationPhase `json:"phase,omitempty"`
	Message                 string           `json:"message,omitempty"`
	LastTransitionTime      metav1.Time      `json:"lastTransitionTime,omitempty"`
	LastHealthCheckTime     *metav1.Time     `json:"lastHealthCheckTime,omitempty"`

	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty" patchStrategy:"merge" patchMergeKey:"type"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Cluster,shortName=app
// +kubebuilder:subresource:status
// +kubebuilder:validation:XValidation:rule="self.metadata.name.matches('^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$')",message="metadata.name must be the lowercase DNS-safe application name"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Release",type=string,JSONPath=`.spec.releaseId`
// +kubebuilder:printcolumn:name="Observed",type=integer,JSONPath=`.status.observedGeneration`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type Application struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ApplicationSpec   `json:"spec"`
	Status ApplicationStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type ApplicationList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Application `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Application{}, &ApplicationList{})
}
