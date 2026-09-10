require 'minitest/autorun'
require 'open3'
require 'yaml'
require 'json'

class StudioSwapRenderTest < Minitest::Test
  CHART = File.expand_path('..', __dir__)

  def render(*args)
    output, error, status = Open3.capture3('rtk', 'proxy', 'helm', 'template', 'studio-swap', CHART,
                                          '--namespace', 'maintenance', *args)
    assert status.success?, error
    YAML.load_stream(output).compact
  end

  def test_suspended_plan_is_pinned_and_has_no_kubernetes_credentials
    docs = render('--set', 'suspended=true', '--set', 'phase=prepareDependencies')
    assert_equal %w[ConfigMap Job], docs.map { |doc| doc['kind'] }.sort
    job = docs.find { |doc| doc['kind'] == 'Job' }
    assert_equal true, job.dig('spec', 'suspend')
    assert_equal 0, job.dig('spec', 'backoffLimit')
    assert_equal 1200, job.dig('spec', 'activeDeadlineSeconds')
    pod = job.dig('spec', 'template', 'spec')
    assert_equal false, pod['automountServiceAccountToken']
    refute pod.key?('serviceAccountName')
    assert_equal({'kubernetes.io/hostname' => 'general-1-worker-1'}, pod['nodeSelector'])
    assert_equal [{'key' => 'node-role.kubernetes.io/control-plane', 'operator' => 'DoesNotExist'}],
                 pod.dig('affinity', 'nodeAffinity', 'requiredDuringSchedulingIgnoredDuringExecution', 'nodeSelectorTerms', 0, 'matchExpressions')
    container = pod.fetch('containers').fetch(0)
    assert_match(/@sha256:[a-f0-9]{64}$/, container['image'])
    assert_equal ['prepareDependencies'], container['args']
    refute container.key?('env')
    refute container.key?('envFrom')
    assert_equal true, container.dig('securityContext', 'readOnlyRootFilesystem')
    assert_equal ['host', 'plan'], pod.fetch('volumes').map { |volume| volume['name'] }
    plan = JSON.parse(docs.find { |doc| doc['kind'] == 'ConfigMap' }.dig('data', 'plan.json'))
    assert_equal 8_589_934_592, plan['size_bytes']
    assert_equal 'studio-swap-v1', plan['serial']
    assert_equal 'b233eaca510b4f35a4a3715cf11fa89d', plan['machine_id']
    assert_equal false, plan['baseline_fail_swap_on']
    assert_operator plan['root_reserve_bytes'], :>=, 1_073_741_824
    refute_empty plan['expected_kubelet_hashes']
  end

  def test_disabled_renders_no_resources
    assert_empty render('--set', 'enabled=false')
  end

  def test_prepare_and_activate_are_distinct_jobs
    prepare = render('--set', 'phase=prepareDependencies', '--set', 'suspended=false', '--set', 'noKubeletArgumentOverridesVerified=false').find { |doc| doc['kind'] == 'Job' }
    activate = render('--set', 'phase=activate', '--set', 'suspended=false', '--set', 'noKubeletArgumentOverridesVerified=true').find { |doc| doc['kind'] == 'Job' }
    refute_equal prepare.dig('metadata', 'name'), activate.dig('metadata', 'name')
    assert_equal ['activate'], activate.dig('spec', 'template', 'spec', 'containers', 0, 'args')
    assert_equal false, activate.dig('spec', 'suspend')
  end

  def test_unsafe_plans_fail_before_resource_creation
    overrides = ['target.hostname=general-1-master', 'target.machineId=wrong', 'disk.serial=existing-disk',
                 'disk.sizeBytes=7516192768', 'rootReserveBytes=1024', 'image=python:latest',
                 'operationId=../bad', 'phase=anything', 'baselineFailSwapOn=true',
                 'phase=activate,suspended=false,noKubeletArgumentOverridesVerified=false']
    overrides.each do |override|
      output, error, status = Open3.capture3('rtk', 'proxy', 'helm', 'template', 'studio-swap', CHART, '--set', override)
      refute status.success?, "unsafe override rendered: #{override}"
      assert_empty output
      refute_empty error
    end
  end
end
