{{/*
Names.
*/}}
{{- define "uione.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "uione.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "uione.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "uione.labels" -}}
app.kubernetes.io/name: {{ include "uione.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "uione.selectorLabels" -}}
app.kubernetes.io/name: {{ include "uione.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "uione.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "uione.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "uione.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/*
Whether this release is running on SQLite.

Read from the URL rather than a separate flag, because a flag and a URL can
disagree and then the guards below are protecting the wrong thing.
*/}}
{{- define "uione.isSqlite" -}}
{{- if .Values.database.existingSecret -}}
false
{{- else if hasPrefix "sqlite" .Values.database.url -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{/*
Refuse configurations that schedule cleanly and then misbehave.

Every one of these is a failure that Kubernetes cannot see: the pods are Ready,
the probes pass, and the damage shows up in a corrupt database or in every
employee getting two identical morning briefs. Failing at `helm template` puts
the error in front of the person doing the install, which is the only moment
anybody is in a position to fix it cheaply.
*/}}
{{- define "uione.validate" -}}

{{- if not .Values.modelPlane.url -}}
{{- fail "modelPlane.url is required. There is deliberately no default: this product must never point at an endpoint nobody chose, and a model plane on the public internet is the specific thing an on-premise deployment exists to avoid." -}}
{{- end -}}

{{- $sqlite := eq (include "uione.isSqlite" .) "true" -}}

{{- if and $sqlite (gt (int .Values.replicaCount) 1) -}}
{{- fail "replicaCount > 1 requires PostgreSQL. SQLite is a single writer on a single filesystem; two pods sharing it over ReadWriteMany produces a database that passes every health check and is quietly corrupt. Set database.existingSecret to a Secret holding a postgresql+asyncpg:// URL." -}}
{{- end -}}

{{- if and $sqlite .Values.scheduler.separate -}}
{{- fail "scheduler.separate requires PostgreSQL. A separate scheduler is a second pod, and a second pod cannot share a SQLite file with the web pods. Either leave scheduler.separate=false (the appliance profile runs it in-process) or move to PostgreSQL." -}}
{{- end -}}

{{- if and (not $sqlite) .Values.persistence.enabled (not .Values.persistence.existingClaim) -}}
{{- if eq .Values.persistence.accessMode "ReadWriteOnce" -}}
{{- if or (gt (int .Values.replicaCount) 1) .Values.scheduler.separate -}}
{{- fail "persistence.accessMode ReadWriteOnce cannot be mounted by more than one pod. With PostgreSQL the volume is only needed for a file share (UIONE_FILES_ROOT); set persistence.enabled=false if you have none, or use ReadWriteMany." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- if and .Values.metrics.serviceMonitor.enabled (not .Values.metrics.existingSecret) -}}
{{- fail "metrics.serviceMonitor.enabled needs metrics.existingSecret. Without a token the /metrics endpoint returns 404 by design, and the ServiceMonitor would scrape nothing while reporting itself healthy." -}}
{{- end -}}

{{/*
The file share has to be on a writable mount.

docker-entrypoint.sh creates UIONE_FILES_ROOT if it is missing, deliberately —
the directory lives on a volume, so it cannot exist at image build time, and
without it the file connector fails every refresh with "root not found". Under
`readOnlyRootFilesystem: true` that mkdir fails instead, and the container never
starts. CrashLoopBackOff with a permission error four levels down in an entrypoint
is a bad afternoon; this is a sentence.
*/}}
{{- $share := index .Values.config "UIONE_FILES_ROOT" | default "" -}}
{{- if and $share .Values.securityContext.readOnlyRootFilesystem -}}
{{- if not (or (hasPrefix "/data" $share) (hasPrefix "/tmp" $share)) -}}
{{- fail (printf "UIONE_FILES_ROOT is %s, which is on the read-only root filesystem. The entrypoint creates this directory at startup and cannot, so the container will CrashLoopBackOff. Put it under /data (the PersistentVolume) or set securityContext.readOnlyRootFilesystem=false." $share) -}}
{{- end -}}
{{- if not .Values.persistence.enabled -}}
{{- fail "UIONE_FILES_ROOT is set but persistence.enabled is false, so the share would live on an ephemeral filesystem and every document the assistant writes would vanish with the pod." -}}
{{- end -}}
{{- end -}}

{{- end -}}

{{/*
Environment shared by the web and scheduler pods.

The only difference between them is UIONE_SCHEDULER_ENABLED, which is set by the
caller — everything else must be identical or the two will disagree about the
world they are acting on.
*/}}
{{- define "uione.env" -}}
- name: UIONE_HOST
  value: "0.0.0.0"
- name: UIONE_PORT
  value: {{ .Values.service.port | quote }}
{{- if .Values.database.existingSecret }}
- name: UIONE_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.existingSecretKey }}
{{- else }}
- name: UIONE_DATABASE_URL
  value: {{ .Values.database.url | quote }}
{{- end }}
- name: UIONE_MODEL_PLANE_URL
  value: {{ .Values.modelPlane.url | quote }}
{{- if .Values.modelPlane.apiKeySecret }}
- name: UIONE_MODEL_PLANE_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.modelPlane.apiKeySecret }}
      key: {{ .Values.modelPlane.apiKeySecretKey }}
{{- end }}
{{- if .Values.metrics.existingSecret }}
- name: UIONE_METRICS_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.metrics.existingSecret }}
      key: {{ .Values.metrics.existingSecretKey }}
{{- end }}
{{- if .Values.tracing.endpoint }}
- name: UIONE_OTEL_ENDPOINT
  value: {{ .Values.tracing.endpoint | quote }}
- name: UIONE_OTEL_SERVICE_NAME
  value: {{ .Values.tracing.serviceName | quote }}
{{- end }}
{{/*
Never on in a pod. Migrations are a Job that runs once — see job-migrate.yaml.
Set here rather than left to the image default so that an image built with a
different default cannot turn every replica into a competing migrator.
*/}}
- name: UIONE_DB_AUTO_UPGRADE
  value: "false"
{{- end -}}

{{- define "uione.envFrom" -}}
{{- if .Values.existingSecrets }}
envFrom:
{{- range .Values.existingSecrets }}
  - secretRef:
      name: {{ .name }}
{{- end }}
  - configMapRef:
      name: {{ include "uione.fullname" $ }}-config
{{- else }}
envFrom:
  - configMapRef:
      name: {{ include "uione.fullname" . }}-config
{{- end }}
{{- end -}}

{{/*
Probes.

**Liveness and readiness both use /health, and /ready is deliberately not used
as a probe at all.**

/ready returns 503 when the model plane is unreachable. That is the right answer
for the question it was written for — "should a load balancer send *chat* here"
— and exactly the wrong answer for a Kubernetes readiness probe, because the
model plane is a shared dependency rather than a property of one pod. When it
goes down, every replica fails the probe at the same moment, Kubernetes empties
the Service, and users get a connection refused.

They should instead get the workspace, rendering the brief, the approval queue
and the transparency page — none of which need a model — with an honest banner
saying the model plane is unreachable. That is gap G8, and a readiness probe
pointed at /ready converts a visible partial outage into an invisible total one.

/ready remains useful, from a dashboard or an alert rule. Just not here.
*/}}
{{- define "uione.probes" -}}
livenessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 10
  periodSeconds: 20
  timeoutSeconds: 3
readinessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 3
startupProbe:
  httpGet:
    path: /health
    port: http
  periodSeconds: 5
  # Generous: the first start rebuilds the retrieval index from stored
  # documents, which on a large corpus is minutes rather than seconds.
  failureThreshold: 60
{{- end -}}

{{/*
readOnlyRootFilesystem means the writable paths have to be declared.
*/}}
{{- define "uione.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if .Values.persistence.enabled }}
- name: data
  mountPath: /data
{{- end }}
{{- end -}}

{{- define "uione.volumes" -}}
- name: tmp
  emptyDir: {}
{{- if .Values.persistence.enabled }}
- name: data
  persistentVolumeClaim:
    claimName: {{ .Values.persistence.existingClaim | default (printf "%s-data" (include "uione.fullname" .)) }}
{{- end }}
{{- end -}}
