require 'minitest/autorun'
require 'open3'
require 'yaml'
require 'json'
require 'tmpdir'
require 'fileutils'
require 'timeout'

class CutlineOciBootstrapTest < Minitest::Test
  ROOT = File.expand_path('..', __dir__)
  APPSET = YAML.load_file(File.join(ROOT, 'cluster/applicationsets/apps.yaml'))
  BOOTSTRAP = YAML.load_stream(File.read(File.join(ROOT, 'cluster/cutline-studio-oci-repository.yaml'))).compact
  ROLE = JSON.parse(File.read(File.join(ROOT, 'bootstrap/cutline-studio-oci-role.json')))

  def render(name, enabled)
    # Helm uses Go text/template, exercising the committed conditions verbatim.
    # The patch's map merge/null deletion/list replacement is the same subset
    # used by Argo v3.4.5's strategic merge for these Application fields.
    Dir.mktmpdir('cutline-appset-test-') do |directory|
      FileUtils.mkdir_p(File.join(directory, 'templates'))
      File.write(File.join(directory, 'Chart.yaml'), "apiVersion: v2\nname: appset-test\nversion: 0.1.0\n")
      File.write(File.join(directory, 'templates/render.yaml'),
                 '{{ tpl .Values.input (merge (dict "path" .Values.path "values" .Values.flags) .) }}')
      values = {
        'input' => YAML.dump(APPSET.dig('spec', 'template')) + "\n---\n" + APPSET.dig('spec', 'templatePatch'),
        'path' => { 'path' => "apps/#{name}", 'basenameNormalized' => name },
        'flags' => { 'cutlineOCIEnabled' => enabled }
      }
      File.write(File.join(directory, 'fixture.json'), JSON.generate(values))
      output, error, status = Open3.capture3('rtk', 'proxy', 'helm', 'template', 'appset-test', directory,
                                            '-f', File.join(directory, 'fixture.json'))
      raise error unless status.success?
      base, patch = YAML.load_stream(output).compact
      [base, patch, merge_patch(base, patch)]
    end
  end

  def merge_patch(base, patch)
    return patch unless patch.is_a?(Hash)
    result = Marshal.load(Marshal.dump(base))
    patch.each do |key, value|
      if value.nil?
        result.delete(key)
      elsif value.is_a?(Hash)
        result[key] = merge_patch(result.fetch(key, {}), value)
      else
        result[key] = value
      end
    end
    result
  end

  def resource(kind)
    BOOTSTRAP.find { |doc| doc['kind'] == kind } || raise("Missing #{kind}")
  end

  def test_inactive_mode_preserves_original_cutline_source
    assert_equal true, APPSET.dig('spec', 'goTemplate')
    assert_includes APPSET.dig('spec', 'goTemplateOptions'), 'missingkey=error'
    assert_includes %w[false true], APPSET.dig('spec', 'generators', 0, 'git', 'values', 'cutlineOCIEnabled')
    base, patch, app = render('cutline-studio', 'false')
    assert_equal({}, patch)
    assert_equal base, app
    assert_equal 'apps/cutline-studio', app.dig('spec', 'source', 'path')
    assert_equal 'https://github.com/anomaly51/general-1-argocd.git', app.dig('spec', 'source', 'repoURL')
    refute app.dig('spec').key?('sources')
  end

  def test_active_cutline_switches_only_source_fields_and_preserves_identity
    base, patch, app = render('cutline-studio', 'true')
    assert_equal ['spec'], patch.keys
    assert_equal %w[source sources], patch.fetch('spec').keys.sort
    assert_nil patch.dig('spec', 'source')
    refute app.fetch('spec').key?('source')
    assert_equal base.fetch('metadata'), app.fetch('metadata')
    assert_equal 'apps-cutline-studio', app.dig('metadata', 'name')
    assert_equal ['resources-finalizer.argocd.argoproj.io'], app.dig('metadata', 'finalizers')
    %w[project destination syncPolicy].each { |field| assert_equal base.dig('spec', field), app.dig('spec', field) }
    chart, values = app.dig('spec', 'sources')
    assert_equal({
      'repoURL' => 'harbor.internal.api-api-api.com/cutline-studio',
      'chart' => 'cutline-studio', 'targetRevision' => '0.1.*',
      'helm' => { 'releaseName' => 'cutline-studio', 'valueFiles' => ['$values/apps/cutline-studio/values.yaml'] }
    }, chart)
    assert_equal({ 'repoURL' => 'https://github.com/anomaly51/general-1-argocd.git',
                   'targetRevision' => 'main', 'ref' => 'values' }, values)
    refute values.key?('path')
    refute values.key?('chart')
  end

  def test_other_apps_are_unchanged_in_both_activation_states
    %w[anomaly51-prod online-shop-fastapi job-application-crm cutline-studio-other].each do |name|
      %w[false true].each do |enabled|
        base, patch, app = render(name, enabled)
        assert_equal({}, patch, "Unexpected patch for #{name}/#{enabled}")
        assert_equal base, app
        assert_equal "apps/#{name}", app.dig('spec', 'source', 'path')
        refute app.fetch('spec').key?('sources')
      end
    end
  end

  def test_unrecognized_activation_strings_cannot_enable_private_source
    ['', 'TRUE', 'yes', 'false\ntrue'].each do |enabled|
      base, patch, app = render('cutline-studio', enabled)
      assert_equal({}, patch)
      assert_equal base, app
    end
  end

  def test_bootstrap_is_root_owned_and_has_no_literal_secret
    kustomization = YAML.load_file(File.join(ROOT, 'cluster/kustomization.yaml'))
    assert_equal 1, kustomization.fetch('resources').count('cutline-studio-oci-repository.yaml')
    assert_equal %w[ServiceAccount VaultAuth VaultStaticSecret], BOOTSTRAP.map { |doc| doc['kind'] }
    BOOTSTRAP.each do |doc|
      assert_equal 'argocd', doc.dig('metadata', 'namespace')
      assert_equal 'cutline-studio-oci', doc.dig('metadata', 'name')
      refute doc.key?('data')
      refute doc.key?('stringData')
    end
    assert_equal false, resource('ServiceAccount')['automountServiceAccountToken']
  end

  def test_vault_auth_is_narrow_and_does_not_reuse_application_identity
    auth = resource('VaultAuth').fetch('spec')
    assert_equal 'kubernetes', auth.fetch('method')
    assert_equal 'kubernetes', auth.fetch('mount')
    assert_equal({ 'role' => 'cutline-studio-argocd-oci', 'serviceAccount' => 'cutline-studio-oci',
                   'audiences' => ['vault'], 'tokenExpirationSeconds' => 600 }, auth.fetch('kubernetes'))
    assert_equal ['cutline-studio-oci'], ROLE.fetch('bound_service_account_names')
    assert_equal ['argocd'], ROLE.fetch('bound_service_account_namespaces')
    assert_equal 'vault', ROLE.fetch('audience')
    assert_equal ['cutline-studio-argocd-oci'], ROLE.fetch('token_policies')
    assert_equal true, ROLE.fetch('token_no_default_policy')
    assert_equal 600, ROLE.fetch('token_ttl')
    assert_equal 3600, ROLE.fetch('token_max_ttl')
  end

  def test_vso_self_renewal_and_cache_lookup_keep_data_access_narrow
    policy = File.read(File.join(ROOT, 'bootstrap/cutline-studio-oci-policy.hcl')).lines.reject { |line| line.start_with?('#') }.join
    rule = /path "([^"]+)"\s*\{\s*capabilities\s*=\s*\["([^"]+)"\]\s*\}/
    rules = policy.scan(rule)
    assert_equal 3, rules.length
    assert_equal({ 'kv/data/apps/cutline-studio/registry' => 'read',
                   'auth/token/renew-self' => 'update',
                   'auth/token/lookup-self' => 'read' }, rules.to_h)
    assert_empty policy.gsub(rule, '').strip, 'No extra ACL stanza or capability may be hidden outside the three rules'
    assert_equal true, ROLE.fetch('token_no_default_policy')
    refute_includes policy, '*'
  end

  def test_repository_secret_is_project_scoped_pull_only_and_excludes_raw_data
    secret = resource('VaultStaticSecret').fetch('spec')
    assert_equal 'cutline-studio-oci', secret.fetch('vaultAuthRef')
    assert_equal 'kv', secret.fetch('mount')
    assert_equal 'kv-v2', secret.fetch('type')
    assert_equal 'apps/cutline-studio/registry', secret.fetch('path')
    destination = secret.fetch('destination')
    assert_equal true, destination.fetch('create')
    assert_equal 'Opaque', destination.fetch('type')
    assert_equal({ 'argocd.argoproj.io/secret-type' => 'repository' }, destination.fetch('labels'))
    transform = destination.fetch('transformation')
    assert_equal true, transform.fetch('excludeRaw')
    assert_equal ['.*'], transform.fetch('excludes')
    templates = transform.fetch('templates').transform_values { |value| value.fetch('text') }
    assert_equal %w[enableOCI name password project type url username], templates.keys.sort
    assert_equal 'helm', templates.fetch('type')
    assert_equal 'true', templates.fetch('enableOCI')
    assert_equal 'gitops-apps', templates.fetch('project')
    assert_equal 'harbor.internal.api-api-api.com/cutline-studio', templates.fetch('url')
    assert_equal '{{ get .Secrets "username" }}', templates.fetch('username')
    assert_equal '{{ get .Secrets "password" }}', templates.fetch('password')
    refute templates.key?('insecure')
    refute templates.key?('passCredentials')
  end

  def test_committed_activation_and_overlay_tag_ownership_are_consistent
    values = YAML.load_file(File.join(ROOT, 'apps/cutline-studio/values.yaml'))
    active = APPSET.dig('spec', 'generators', 0, 'git', 'values', 'cutlineOCIEnabled') == 'true'
    %w[api frontend].each do |component|
      if active
        refute values.fetch('images').fetch(component).key?('tag'), "OCI chart must own #{component} tag"
      else
        assert_match(/\Asha-[0-9a-f]{12}\z/, values.dig('images', component, 'tag'))
      end
    end
    assert_includes [0, 1], values.dig('replicas', 'api')
  end

  def test_activation_tag_removal_preserves_every_other_value
    values = YAML.load_file(File.join(ROOT, 'apps/cutline-studio/values.yaml'))
    # Independent fixture remains valid after a future authorized activation.
    %w[api frontend].each { |component| values.fetch('images').fetch(component)['tag'] = 'sha-012345abcdef' }
    original = Marshal.load(Marshal.dump(values))
    removed = %w[api frontend].map { |component| values.fetch('images').fetch(component).delete('tag') }
    assert_equal 2, removed.compact.length
    restored = Marshal.load(Marshal.dump(values))
    %w[api frontend].zip(removed) { |component, tag| restored.fetch('images').fetch(component)['tag'] = tag }
    assert_equal original, restored
    assert_equal original.dig('images', 'oauth2Proxy', 'tag'), values.dig('images', 'oauth2Proxy', 'tag')
    assert_equal original.dig('images', 'postgres', 'tag'), values.dig('images', 'postgres', 'tag')
    assert_equal original['replicas'], values['replicas']
    assert_equal original['migration'], values['migration']
  end

  def test_optional_real_argocd_generate_removes_single_source
    context = ENV['CUTLINE_ARGO_CONTEXT']
    skip 'Set CUTLINE_ARGO_CONTEXT for the non-mutating owner-authorized Argo Generate RPC' unless context
    kubeconfig = ENV.fetch('CUTLINE_ARGO_KUBECONFIG')
    Dir.mktmpdir('cutline-argo-generate-test-') do |directory|
      fixture = Marshal.load(Marshal.dump(APPSET))
      # Generate performs no writes. A list fixture needs neither Git nor Harbor.
      fixture['spec']['generators'] = [{ 'list' => { 'elements' => [{
        'path' => { 'path' => 'apps/cutline-studio', 'basenameNormalized' => 'cutline-studio' },
        'values' => { 'cutlineOCIEnabled' => 'true' }
      }] } }]
      path = File.join(directory, 'application-set.yaml')
      output_path, error_path = %w[output.json error.log].map { |name| File.join(directory, name) }
      File.write(path, YAML.dump(fixture))
      pid = Process.spawn({ 'KUBECONFIG' => kubeconfig }, 'rtk', 'proxy', 'argocd', 'appset', 'generate', path,
                          '--argocd-context', context, '--port-forward', '--port-forward-namespace', 'argocd',
                          '-o', 'json', out: output_path, err: error_path)
      begin
        Timeout.timeout(30) { Process.wait(pid) }
      rescue Timeout::Error
        Process.kill('TERM', pid)
        Process.wait(pid)
        flunk 'Argo Generate timed out; owned CLI process stopped (response omitted)'
      end
      success = $?.success?
      unless success
        # Diagnose only fixed categories; never print the raw auth/client response.
        error = File.read(error_path)
        category = if error.match?(/expired|Unauthenticated|invalid session/i)
                     'existing session expired or unauthenticated'
                   elsif error.match?(/PermissionDenied|permission denied/i)
                     'existing session lacks Generate permission'
                   elsif error.match?(/x509|certificate|tls handshake/i)
                     'TLS verification failed'
                   elsif error.match?(/connect|dial tcp|port.forward|refused/i)
                     'connection or port-forward unavailable'
                   else
                     'client or render rejected request'
                   end
        flunk "Argo Generate #{category} (response omitted)"
      end
      generated = JSON.parse(File.read(output_path))
      applications = generated.is_a?(Array) ? generated : generated.fetch('applications')
      assert_equal 1, applications.length
      app = applications.first
      refute app.fetch('spec').key?('source'), 'Actual Argo strategic merge must remove single source'
      assert_equal 2, app.dig('spec', 'sources').length
      assert_equal 'cutline-studio', app.dig('spec', 'sources', 0, 'chart')
      assert_equal 'values', app.dig('spec', 'sources', 1, 'ref')
      refute app.dig('spec', 'sources', 1).key?('path')
      assert_equal 'apps-cutline-studio', app.dig('metadata', 'name')
      assert_equal ['resources-finalizer.argocd.argoproj.io'], app.dig('metadata', 'finalizers')
      assert_equal APPSET.dig('spec', 'template', 'spec', 'syncPolicy'), app.dig('spec', 'syncPolicy')
    end
  end
end
