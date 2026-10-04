require 'minitest/autorun'
require 'open3'
require 'yaml'

class RuntimeRenderTest < Minitest::Test
  CHART = File.expand_path('..', __dir__)
  NAMESPACE = 'argo-workflows'

  def self.documents
    @documents ||= begin
      output, error, status = Open3.capture3(
        'helm', 'template', 'engine', CHART, '--namespace', NAMESPACE
      )
      raise "helm template failed: #{error}" unless status.success?

      YAML.load_stream(output).compact
    end
  end

  def documents
    self.class.documents
  end

  def resource(kind, name)
    documents.find { |item| item['kind'] == kind && item.dig('metadata', 'name') == name }
  end

  def controller_config
    YAML.load(resource('ConfigMap', 'argo-workflow-controller-configmap').dig('data', 'config'))
  end

  def test_no_migrated_workflows_jobs_credentials_or_public_routes
    forbidden = %w[Workflow WorkflowTemplate ClusterWorkflowTemplate CronWorkflow Job CronJob Secret ExternalSecret VaultStaticSecret Ingress IngressRoute HTTPRoute Gateway]
    assert_empty documents.select { |item| forbidden.include?(item['kind']) }
    assert_empty documents.select { |item| %w[ClusterRole ClusterRoleBinding].include?(item['kind']) }
    assert_empty documents.select { |item| item.dig('metadata', 'namespace') && item.dig('metadata', 'namespace') != NAMESPACE }
  end

  def test_pinned_namespaced_runtime_and_authentication
    deployments = documents.select { |item| item['kind'] == 'Deployment' }
    assert_equal 2, deployments.length
    deployments.each do |deployment|
      container = deployment.dig('spec', 'template', 'spec', 'containers').first
      assert_match(/:v3\.5\.5\z/, container['image'])
      assert_includes container['args'], '--namespaced'
      assert_equal false, container.dig('securityContext', 'allowPrivilegeEscalation')
      assert_equal true, container.dig('securityContext', 'readOnlyRootFilesystem')
      %w[requests limits].each do |type|
        %w[cpu memory].each { |key| refute_nil container.dig('resources', type, key) }
      end
    end

    args = resource('Deployment', 'argo-server').dig('spec', 'template', 'spec', 'containers').first['args']
    assert_includes args, '--auth-mode=client'
    assert_includes args, '--secure=true'
    refute_includes args, '--auth-mode=server'
    documents.select { |item| item['kind'] == 'Service' }.each do |service|
      assert_equal 'ClusterIP', service.dig('spec', 'type')
    end
  end

  def test_crds_are_gitops_managed_and_retained
    crds = documents.select { |item| item['kind'] == 'CustomResourceDefinition' }
    assert_equal 7, crds.length
    assert_nil resource('CustomResourceDefinition', 'clusterworkflowtemplates.argoproj.io')
    crds.each do |crd|
      assert_equal '-10', crd.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave')
      assert_equal 'Delete=false,Prune=false', crd.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-options')
      assert_equal 'keep', crd.dig('metadata', 'annotations', 'helm.sh/resource-policy')
    end
  end

  def test_executor_and_viewer_have_limited_rbac
    executor = resource('Role', 'argo-workflow-executor')
    assert_equal [{ 'apiGroups' => ['argoproj.io'], 'resources' => ['workflowtaskresults'], 'verbs' => %w[create patch] }], executor['rules']
    viewer = resource('Role', 'argo-viewer')
    viewer['rules'].each do |rule|
      assert_empty rule['verbs'] - %w[get list watch]
      refute_includes rule['resources'], 'secrets'
    end
    documents.select { |item| item['kind'] == 'RoleBinding' }.each do |binding|
      binding['subjects'].each { |subject| assert_equal NAMESPACE, subject['namespace'] }
    end
    assert_equal false, resource('ServiceAccount', 'argo-viewer')['automountServiceAccountToken']
    assert_equal 'argo-workflow', controller_config.dig('workflowDefaults', 'spec', 'serviceAccountName')
  end

  def test_minio_endpoint_does_not_enable_state_or_artifact_writes
    assert_equal 'minio.minio-system.svc.cluster.local:9000', resource('ConfigMap', 'utility-migration-endpoints').dig('data', 'minioEndpoint')
    assert_equal 'false', resource('ConfigMap', 'utility-migration-endpoints').dig('data', 'terraformWorkflowsEnabled')
    refute controller_config.key?('artifactRepository')
    refute controller_config.key?('persistence')
    assert_equal 2, controller_config['parallelism']
    assert_equal 2, controller_config['namespaceParallelism']
    assert_equal '256Mi', controller_config.dig('executor', 'resources', 'limits', 'memory')
  end
end
