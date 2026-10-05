"""Render the native ApplicationSet patch with Go templates before deployment."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ('order-service', 'pricing-service', 'inventory-service', 'event-hub',
            'analytics-service', 'shell', 'topology-mfe', 'traffic-mfe')
APPSET = yaml.safe_load((ROOT / 'cluster/applicationsets/playground-previews.yaml').read_text())


def sources_for(service):
    # Helm's tpl uses the same Go/Sprig functions as this ApplicationSet patch.
    # This temporary chart is a test harness, not a deployment wrapper.
    with tempfile.TemporaryDirectory(prefix='native-preview-test-') as directory:
        chart = Path(directory)
        (chart / 'templates').mkdir()
        (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: test\nversion: 0.1.0\n')
        (chart / 'templates/render.yaml').write_text('{{ tpl .Values.patch .Values.context }}\n')
        values = {'patch': APPSET['spec']['templatePatch'], 'context': {
            'values': {'service': service}, 'number': '42', 'head_sha': 'a' * 40,
            'Template': {'BasePath': 'test/templates', 'Name': 'test/templates/render.yaml'},
        }}
        (chart / 'values.yaml').write_text(yaml.safe_dump(values))
        output = subprocess.run(['helm', 'template', 'test', str(chart)],
                                check=True, text=True, capture_output=True).stdout
        return yaml.safe_load(output)['spec']['sources']


class NativePreviewTests(unittest.TestCase):
    def test_only_native_pr_generators_and_cascade(self):
        generators = APPSET['spec']['generators']
        self.assertEqual(len(generators), 8)
        self.assertEqual([g['pullRequest']['values']['service'] for g in generators], list(SERVICES))
        for generator in generators:
            self.assertEqual(set(generator), {'pullRequest'})
            pr = generator['pullRequest']
            self.assertEqual(pr['github']['labels'], ['preview'])
            self.assertEqual(pr['github']['repo'], 'playground-' + pr['values']['service'])
            self.assertEqual(pr['requeueAfterSeconds'], 60)
        self.assertFalse(APPSET['spec']['syncPolicy']['preserveResourcesOnDeletion'])
        template = APPSET['spec']['template']
        self.assertIn('resources-finalizer.argocd.argoproj.io', template['metadata']['finalizers'])
        self.assertEqual(template['spec']['project'], 'gitops-apps')

    def test_each_service_overrides_only_its_own_image(self):
        for service in SERVICES:
            with self.subTest(service=service):
                sources = sources_for(service)
                self.assertEqual(len(sources), 16)
                apps = [source for source in sources if 'chart' in source]
                changed = [s for s in apps if 'image' in s['helm']['valuesObject']]
                self.assertEqual(len(changed), 1)
                self.assertEqual(changed[0]['helm']['releaseName'], service)
                self.assertEqual(changed[0]['helm']['valuesObject']['image'], {
                    'repository': 'harbor.internal.api-api-api.com/playground-previews/' + service,
                    'tag': 'pr-' + 'a' * 40,
                })
                for app in apps:
                    name = app['helm']['releaseName']
                    self.assertEqual(app['helm']['valueFiles'], [f'$values/apps/playground-{name}/values/staging.yaml'])
                    if name in ('order-service', 'event-hub'):
                        self.assertEqual(app['helm']['valuesObject']['env']['CORS_ORIGINS'],
                                         f'https://preview-{service}-42.internal.api-api-api.com')
                utilities = [s for s in sources if 'path' in s]
                self.assertEqual(len(utilities), 7)
                for utility in utilities:
                    values = utility['helm']['valuesObject']
                    self.assertTrue(values['ephemeral'])
                    self.assertNotIn('playground-staging', str(values))
                    self.assertNotIn('playground-prod', str(values))

    def test_namespace_owned_and_disposable(self):
        output = subprocess.run(['helm', 'template', 'namespace',
            str(ROOT / 'utility-apps/playground-staging/namespace'),
            '--namespace', 'preview-shell-42', '--set', 'ephemeral=true'],
            check=True, text=True, capture_output=True).stdout
        resources = list(yaml.safe_load_all(output))
        namespace = next(r for r in resources if r['kind'] == 'Namespace')
        self.assertEqual(namespace['metadata']['name'], 'preview-shell-42')
        self.assertEqual(namespace['metadata']['labels']['gitops.api-api-api.com/environment'], 'preview')
        self.assertNotIn('Delete=false', output)
        self.assertNotIn('lifecycle', output)
        self.assertNotIn('RoleBinding', output)


if __name__ == '__main__':
    unittest.main()
