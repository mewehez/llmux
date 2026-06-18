{{- define "llm-server.labels" -}}
app.kubernetes.io/part-of: llm-server
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
