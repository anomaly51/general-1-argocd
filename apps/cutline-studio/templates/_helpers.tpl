{{- define "cutline.labels" -}}
app.kubernetes.io/name: cutline-studio
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: cutline-studio
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "cutline.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: cutline-studio
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "cutline.workerAffinity" -}}
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:
      - matchExpressions:
          - key: node-role.kubernetes.io/control-plane
            operator: DoesNotExist
{{- end -}}
