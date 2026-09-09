require 'minitest/autorun'
require 'open3'
require 'yaml'

class CutlineChartTest < Minitest::Test
  CHART = File.expand_path('..', __dir__)

  def self.render(*overrides)
    out, err, status = Open3.capture3('rtk', 'proxy', 'helm', 'template', 'cutline-studio', CHART,
                                     '--namespace', 'apps', *overrides)
    raise err unless status.success?
    YAML.load_stream(out).compact
  end

  MIGRATION = render.freeze
  LIVE = render('--set', 'migration.enabled=false').freeze

  def resource(kind, component = nil, docs = LIVE)
    name = component ? "cutline-studio-#{component}" : 'cutline-studio'
    docs.find { |doc| doc['kind'] == kind && doc.dig('metadata', 'name') == name } ||
      raise("Missing #{kind}/#{name}")
  end

  def pod(component, docs = LIVE)
    resource(component == 'postgres' ? 'StatefulSet' : 'Deployment', component, docs)
      .dig('spec', 'template', 'spec')
  end

  def container(component, docs = LIVE)
    pod(component, docs).fetch('containers').first
  end

  def test_only_public_route_targets_auth_proxy
    routes = LIVE.select { |doc| %w[HTTPRoute Ingress TCPRoute GRPCRoute].include?(doc['kind']) }
    assert_equal 1, routes.length
    route = routes.first.fetch('spec')
    assert_equal ['cutline-general1.api-api-api.com'], route['hostnames']
    assert_equal [{ 'group' => 'gateway.networking.k8s.io', 'kind' => 'Gateway',
                    'name' => 'public', 'namespace' => 'networking', 'sectionName' => 'https' }], route['parentRefs']
    assert_equal [{ 'group' => '', 'kind' => 'Service',
                    'name' => 'cutline-studio-oauth2-proxy', 'port' => 4180 }], route['rules'].first['backendRefs']
  end

  def test_services_are_cluster_only_and_ports_match_images
    services = LIVE.select { |doc| doc['kind'] == 'Service' }
    assert_equal 4, services.length
    assert services.all? { |doc| doc.dig('spec', 'type') == 'ClusterIP' }
    { 'api' => [8001, 8001], 'frontend' => [80, 8080],
      'oauth2-proxy' => [4180, 4180], 'postgres' => [5432, 5432] }.each do |name, ports|
      assert_equal ports[0], resource('Service', name).dig('spec', 'ports', 0, 'port')
      assert_equal ports[1], container(name).dig('ports', 0, 'containerPort')
    end
  end

  def test_owner_group_remains_nonempty_and_no_auth_bypasses
    auth = container('oauth2-proxy')
    args = auth.fetch('args')
    assert_includes args, '--allowed-group=cutline-studio-owner'
    assert_includes args, '--scope=openid email profile offline_access'
    assert_includes args, '--code-challenge-method=S256'
    assert_includes args, '--insecure-oidc-skip-nonce=false'
    assert_includes args, '--cookie-secure=true'
    refute args.any? { |arg| arg.start_with?('--skip-auth-route', '--trusted-ip=', '--skip-auth-regex') }
    required = auth.fetch('env').find { |env| env['name'] == 'OAUTH2_PROXY_ALLOWED_GROUPS' }
    assert_equal false, required.dig('valueFrom', 'secretKeyRef', 'optional')
  end

  def test_schema_rejects_empty_or_wildcard_owner_group_and_multiple_api_workers
    ['auth.allowedGroup=', 'auth.allowedGroup=*', 'replicas.api=2', 'migration.enabled=oops'].each do |override|
      _out, _err, status = Open3.capture3('rtk', 'proxy', 'helm', 'template', 'cutline-studio', CHART, '--set', override)
      refute status.success?, "Unsafe override accepted: #{override}"
    end
  end

  def test_initial_migration_mode_keeps_api_stopped
    assert_equal 0, resource('Deployment', 'api', MIGRATION).dig('spec', 'replicas')
    assert_equal 1, resource('Deployment', 'migration', MIGRATION).dig('spec', 'replicas')
    assert_equal 1, resource('Deployment', 'api').dig('spec', 'replicas')
    refute LIVE.any? { |doc| doc.dig('metadata', 'name') == 'cutline-studio-migration' }
    assert_equal 'Recreate', resource('Deployment', 'api').dig('spec', 'strategy', 'type')
  end

  def test_restore_tools_share_media_identity_and_only_db_credentials
    restore_pod = pod('migration', MIGRATION)
    assert_equal 10001, restore_pod.dig('securityContext', 'runAsUser')
    assert_equal 'cutline-studio-data', restore_pod['volumes'].find { |v| v['name'] == 'data' }.dig('persistentVolumeClaim', 'claimName')
    restore = container('migration', MIGRATION)
    assert_equal 'docker.io/library/postgres:17-bookworm', restore['image']
    assert_equal ['sh', '-ec'], restore['command']
    refute restore.key?('envFrom')
    assert_equal %w[PGDATABASE PGPASSWORD PGUSER], restore['env'].select { |e| e.key?('valueFrom') }.map { |e| e['name'] }.sort
  end

  def test_credentials_are_only_vault_references
    refute LIVE.any? { |doc| doc['kind'] == 'Secret' }
    secrets = LIVE.select { |doc| doc['kind'] == 'VaultStaticSecret' }
    assert_equal 4, secrets.length
    assert_equal 'apps/cutline-studio/registry', resource('VaultStaticSecret', 'registry').dig('spec', 'path')
    assert_equal 'apps/cutline-studio/auth', resource('VaultStaticSecret', 'auth').dig('spec', 'path')
    assert_equal 'apps/cutline-studio/env', resource('VaultStaticSecret', 'env').dig('spec', 'path')
    pg = resource('VaultStaticSecret', 'postgres-env').dig('spec', 'destination', 'transformation')
    assert_equal ['.*'], pg['excludes']
    assert_equal %w[POSTGRES_DB POSTGRES_PASSWORD POSTGRES_USER], pg.fetch('templates').keys.sort
    assert_equal ['^POSTGRES_.*$'], resource('VaultStaticSecret', 'env').dig('spec', 'destination', 'transformation', 'excludes')
  end

  def test_both_persistent_volumes_survive_argo_prune_and_delete
    { 'data' => '30Gi', 'postgres' => '10Gi' }.each do |name, size|
      claim = resource('PersistentVolumeClaim', name)
      assert_equal 'Delete=false,Prune=false', claim.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-options')
      assert_equal 'nfs-csi', claim.dig('spec', 'storageClassName')
      assert_equal size, claim.dig('spec', 'resources', 'requests', 'storage')
    end
  end

  def test_all_containers_have_nonroot_readonly_security_and_no_service_account_token
    MIGRATION.select { |doc| %w[Deployment StatefulSet].include?(doc['kind']) }.each do |workload|
      spec = workload.dig('spec', 'template', 'spec')
      assert_equal false, spec['automountServiceAccountToken']
      assert_equal true, spec.dig('securityContext', 'runAsNonRoot')
      assert_equal 'RuntimeDefault', spec.dig('securityContext', 'seccompProfile', 'type')
      (spec.fetch('containers') + spec.fetch('initContainers', [])).each do |c|
        assert_equal false, c.dig('securityContext', 'allowPrivilegeEscalation')
        assert_equal true, c.dig('securityContext', 'readOnlyRootFilesystem')
        assert_equal ['ALL'], c.dig('securityContext', 'capabilities', 'drop')
      end
    end
  end

  def test_api_paths_probes_and_migration_safe_flags
    api = container('api')
    env = api['env'].to_h { |e| [e['name'], e['value']] }
    %w[STUDIO_RECOVER_GENERATION_JOBS STUDIO_PURGE_LEGACY_DATA TELEGRAM_BOT_ENABLED TELEGRAM_DAILY_MEMES_ENABLED].each do |key|
      assert_equal 'false', env[key]
    end
    assert_equal '/data/assets/manifest.json', env['ASSET_MANIFEST_PATH']
    assert_equal '/data/generated', env['GENERATED_ASSET_ROOT']
    assert_equal '/data/outputs', env['STUDIO_OUTPUT_ROOT']
    assert_equal 'http://127.0.0.1:8001', env['HEADLESS_WEBAV_BACKEND_ORIGIN']
    assert_equal '/api/v1/ready', api.dig('readinessProbe', 'httpGet', 'path')
    assert_equal '/healthz', container('frontend').dig('readinessProbe', 'httpGet', 'path')
    assert_equal '512Mi', pod('api')['volumes'].find { |v| v['name'] == 'shm' }.dig('emptyDir', 'sizeLimit')
    assert_equal '4Gi', pod('api')['volumes'].find { |v| v['name'] == 'tmp' }.dig('emptyDir', 'sizeLimit')
  end

  def test_api_and_frontend_images_use_dedicated_registry_project
    assert_match %r{\Aharbor\.internal\.api-api-api\.com/cutline-studio/api:}, container('api')['image']
    assert_match %r{\Aharbor\.internal\.api-api-api\.com/cutline-studio/frontend:}, container('frontend')['image']
  end

  def test_default_deny_is_scoped_to_chart
    spec = resource('NetworkPolicy', 'default-deny').fetch('spec')
    assert_equal %w[Ingress Egress], spec['policyTypes']
    assert_equal({ 'app.kubernetes.io/instance' => 'cutline-studio',
                   'app.kubernetes.io/part-of' => 'cutline-studio' }, spec.dig('podSelector', 'matchLabels'))
    refute spec.key?('ingress')
    refute spec.key?('egress')
  end

  def test_ingress_requires_auth_chain_and_migration_permission_is_temporary
    { 'frontend' => ['oauth2-proxy'], 'api' => %w[frontend api], 'postgres' => ['api'] }.each do |component, allowed|
      peers = resource('NetworkPolicy', component).dig('spec', 'ingress', 0, 'from')
      assert_equal allowed, peers.map { |p| p.dig('podSelector', 'matchLabels', 'app.kubernetes.io/component') }
      assert peers.all? { |p| !p.key?('namespaceSelector') && !p.key?('ipBlock') }
    end
    migration_peers = resource('NetworkPolicy', 'postgres', MIGRATION).dig('spec', 'ingress', 0, 'from')
    assert_equal %w[api migration], migration_peers.map { |p| p.dig('podSelector', 'matchLabels', 'app.kubernetes.io/component') }
    restore_policy = resource('NetworkPolicy', 'migration', MIGRATION).fetch('spec')
    refute restore_policy.key?('ingress')
    assert_equal 'postgres', restore_policy.dig('egress', 0, 'to', 0, 'podSelector', 'matchLabels', 'app.kubernetes.io/component')
    assert_equal [{ 'protocol' => 'TCP', 'port' => 5432 }], restore_policy.dig('egress', 0, 'ports')
  end

  def test_auth_ingress_only_from_traefik_not_an_entire_namespace
    peer = resource('NetworkPolicy', 'oauth2-proxy').dig('spec', 'ingress', 0, 'from', 0)
    assert_equal 'kube-system', peer.dig('namespaceSelector', 'matchLabels', 'kubernetes.io/metadata.name')
    assert_equal 'traefik', peer.dig('podSelector', 'matchLabels', 'app.kubernetes.io/name')
  end

  def test_postgres_probes_allow_recovery_and_client_timeout_before_exec_timeout
    postgres = container('postgres')
    %w[startupProbe readinessProbe].each do |name|
      probe = postgres.fetch(name)
      assert_equal 'exec pg_isready -h 127.0.0.1 -t 5 -U "$POSTGRES_USER" -d "$POSTGRES_DB"', probe.dig('exec', 'command', 2)
      assert_operator probe.fetch('timeoutSeconds'), :>, 5
    end
    assert_equal({ 'port' => 'postgres' }, postgres.dig('livenessProbe', 'tcpSocket'))
    refute postgres.fetch('livenessProbe').key?('exec')
    assert_equal '/var/lib/postgresql/data/pgdata17', postgres.fetch('env').find { |e| e['name'] == 'PGDATA' }['value']
  end
end
