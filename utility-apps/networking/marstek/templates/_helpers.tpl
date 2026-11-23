{{- define "marstek.frontendName" -}}
{{- printf "marstek-frontend-%s" ((.Files.Glob "frontend/**").AsSecrets | sha256sum | trunc 16) -}}
{{- end -}}
